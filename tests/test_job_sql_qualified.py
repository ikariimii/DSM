"""Residual 4: every table name in a spark.sql / spark.table / read.table / saveAsTable / insertInto / toTable /
DeltaTable.forName string in src/jobs and src/dam_safety is fully qualified from the job's normalized catalog
parameter (catalog.schema.table), never schema-only or bare (which would depend on the session's current catalog).
Static: the call arguments are parsed with ast, f-strings / fq(...) / local names are resolved, and every table
reference in the SQL text (FROM / JOIN / INTO / USING / UPDATE / TABLE / MERGE INTO / DESCRIBE ...) is checked.
Temp views created in the same file are allowed bare."""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = [os.path.join(ROOT, "src", "jobs"), os.path.join(ROOT, "src", "dam_safety"), os.path.join(ROOT, "src", "pipelines")]
# a.catalog / C / catalog / cat = the --catalog job parameter after _common.args() normalize_catalog (QA-055);
# CATALOG = the pipeline's spark.conf dam.catalog (src/pipelines, qualified but not normalized: see VERIFICATION)
CAT_EXPRS = {"a.catalog", "C", "catalog", "cat", "args.catalog", "CATALOG"}
CAT = "\x00CAT\x00"
NAME_CALLS = {"table", "saveAsTable", "insertInto", "toTable", "forName"}
SQL_CALLS = {"sql"}
SQL_BUILDERS = {"merge_sql", "dam_sync_sql", "audit_insert_sql"}          # helpers whose args are table names / views
REF = re.compile(r"(?is)\b(?:FROM|JOIN|INTO|USING|UPDATE|TABLE|DESCRIBE(?:\s+HISTORY)?|OPTIMIZE|VACUUM)\s+"
                 r"(?!\(|SET\b|SELECT\b|TABLE\b|IF\b|VALUES\b|LATERAL\b|UNNEST\b|EXPLODE\b)([^\s,()]+)")
QUALIFIED = re.compile(r"^" + re.escape(CAT) + r"\.\w+\.\w+$")


def _files():
    for d in DIRS:
        for base, _, names in os.walk(d):
            for n in sorted(names):
                if n.endswith(".py"):
                    yield os.path.join(base, n)


class _Resolver:
    def __init__(self, tree, src):
        self.src = src
        self.assigns = {}
        self.lambdas = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                (self.lambdas if isinstance(node.value, ast.Lambda) else self.assigns).setdefault(
                    node.targets[0].id, []).append(node.value)

    def seg(self, node):
        return ast.get_source_segment(self.src, node) or ""

    def render(self, node, depth=0):
        if depth > 6:
            return "{?}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if self.seg(node) in CAT_EXPRS:
            return CAT
        if isinstance(node, ast.JoinedStr):
            return "".join(self.render(v.value if isinstance(v, ast.FormattedValue) else v, depth + 1)
                           for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.render(node.left, depth + 1) + self.render(node.right, depth + 1)
        if isinstance(node, ast.Call):
            f = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if f == "fq":
                args = [self.render(x, depth + 1) for x in node.args]
                if len(args) == 2 and "fq" in self.lambdas:                 # fq = lambda s, t: f"{a.catalog}.{s}.{t}"
                    lam = self.lambdas["fq"][-1]
                    if self.render(lam.body, depth + 1) == CAT + ".{s}.{t}" or \
                            self.render(lam.body, depth + 1).startswith(CAT + "."):
                        return CAT + "." + ".".join(args)
                return ".".join(args)
            if f == "format" and isinstance(node.func, ast.Attribute):
                return self.render(node.func.value, depth + 1)
        if isinstance(node, ast.Name):
            vals = self.assigns.get(node.id, [])
            rendered = {self.render(v, depth + 1) for v in vals}
            if len(rendered) == 1:
                return rendered.pop()
            return "{" + node.id + "}"
        return "{?" + self.seg(node) + "}"


def _views(tree, res):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                "createOrReplaceTempView", "createTempView", "createOrReplaceGlobalTempView") and node.args:
            out.add(res.render(node.args[0]))
    return out


def _is_spark_table_call(node):
    """spark.table(...) / spark.read.table(...) / *.saveAsTable(...) etc. (not e.g. a pandas .table())."""
    f = node.func
    if f.attr in ("saveAsTable", "insertInto", "toTable", "forName"):
        return True
    recv = f.value
    return (isinstance(recv, ast.Name) and recv.id == "spark") or (
        isinstance(recv, ast.Attribute) and recv.attr == "read")


def findings():
    """[(file:line, kind, name or reference, problem)] for every reference that is not catalog-qualified."""
    bad, seen = [], 0
    for path in _files():
        src = open(path).read()
        tree = ast.parse(src)
        res = _Resolver(tree, src)
        views = _views(tree, res)
        rel = os.path.relpath(path, ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            f = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            where = f"{rel}:{node.lineno}"
            if isinstance(node.func, ast.Attribute) and f in NAME_CALLS and _is_spark_table_call(node):
                seen += 1
                name = res.render(node.args[0])
                if not QUALIFIED.match(name) and name not in views:
                    bad.append((where, f, name.replace(CAT, "{catalog}"), "not catalog.schema.table"))
            elif (isinstance(node.func, ast.Attribute) and f in SQL_CALLS and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "spark") or f in SQL_BUILDERS:
                seen += 1
                texts = [res.render(node.args[0])] if f in SQL_CALLS else [res.render(x) for x in node.args]
                for i, text in enumerate(texts):
                    refs = REF.findall(text) if f in SQL_CALLS else [text]
                    for r in refs:
                        r = r.rstrip(";")
                        if QUALIFIED.match(r) or r in views or (f in SQL_BUILDERS and r in views):
                            continue
                        if f in SQL_BUILDERS and not re.match(r"^[\w.{}\x00?]+$", r):
                            continue                                      # a non-name argument (e.g. columns)
                        bad.append((where, f, r.replace(CAT, "{catalog}"), "not catalog.schema.table"))
    return bad, seen


def test_every_job_and_engine_table_reference_is_catalog_qualified():
    bad, seen = findings()
    assert seen >= 60, seen                         # the scan really sees the calls
    assert bad == [], "\n".join(map(str, bad))


SCHEMAS = ("ref", "ops", "audit", "silver", "gold", "rpt", "raw", "ml", "bronze")
LITERAL = re.compile(r"(?i)(?<![\w.}])(?:FROM|JOIN|INTO|USING|UPDATE|TABLE)\s+((?:" + "|".join(SCHEMAS) + r")\.\w+)\b")


def test_no_schema_only_table_name_in_any_code_string():
    """Belt and braces for SQL assembled away from the call site: no string literal (docstrings excluded) in the scanned
    code has `FROM ref.x` / `INTO audit.x` ... without a catalog in front."""
    bad = []
    for path in _files():
        tree = ast.parse(open(path).read())
        docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(
            n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body and isinstance(
            n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                bad += [(os.path.relpath(path, ROOT), node.lineno, m) for m in LITERAL.findall(node.value)]
    assert bad == [], bad


@pytest.mark.parametrize("snippet, ok", [
    ('spark.table(f"{a.catalog}.ref.dam")', True),
    ('spark.table("ref.dam")', False),
    ('spark.table("dam")', False),
    ('spark.sql(f"SELECT * FROM {a.catalog}.ref.dam d JOIN ref.threshold t ON d.x = t.x")', False),
    ('spark.sql(f"SELECT * FROM {a.catalog}.ref.dam d JOIN {C}.ref.threshold t ON 1=1")', True),
    ('spark.sql(f"MERGE INTO ops.alarm_state t USING v s ON 1=1")', False),
    ('df.write.saveAsTable("audit.event_log")', False),
    ('spark.read.table(fq(a.catalog, "ref", "pfm"))', True),
    ('spark.sql(f"SELECT * FROM (SELECT 1) x")', True),
])
def test_the_scanner_catches_schema_only_and_bare_names(tmp_path, monkeypatch, snippet, ok):
    d = tmp_path / "jobs"
    d.mkdir()
    (d / "x.py").write_text("def fq(c, s, t):\n    return f'{c}.{s}.{t}'\n" + snippet + "\n")
    monkeypatch.setattr(__import__(__name__), "DIRS", [str(d)])
    bad, _ = findings()
    assert (bad == []) is ok, bad
