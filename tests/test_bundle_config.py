"""Static checks of the bundle config (no CLI on the build box, so `databricks bundle validate` was
NOT run - VERIFICATION.md BUNDLE-1). Source: Databricks DAB docs (resources reference, Lakebase
bundles page docs.databricks.com/aws/en/oltp/projects/manage-with-bundles) as cited in each resource file."""
import glob
import os
import re

import pytest

yaml = pytest.importorskip("yaml")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECURITY_MODES = ("table", "abac")      # databricks.yml var.security_mode (Databricks SME 2026-11-02)


def _load(p):
    return yaml.safe_load(open(p, encoding="utf-8"))


def _resource_files():
    return sorted(glob.glob(os.path.join(ROOT, "resources", "*.yml")))


def _walk(node, base, found):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("python_file", "path") and isinstance(v, str) and not v.startswith("$"):
                found.append((base, v))
            _walk(v, base, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, base, found)


def test_all_yaml_parses():
    for p in [os.path.join(ROOT, "databricks.yml")] + _resource_files():
        assert _load(p) is not None, p


def test_referenced_files_exist():
    found = []
    _walk(_load(os.path.join(ROOT, "databricks.yml")), ROOT, found)
    for p in _resource_files():
        _walk(_load(p), os.path.dirname(p), found)
    found = [(b, v.replace("${var.security_mode}", m)) for b, v in found for m in SECURITY_MODES]   # mode switch
    missing = [(b, v) for b, v in found if not os.path.exists(os.path.normpath(os.path.join(b, v)))]
    assert found and not missing, missing


def test_python_tasks_have_serverless_environment():
    for p in _resource_files():
        for name, job in (_load(p).get("resources", {}).get("jobs") or {}).items():
            envs = {e["environment_key"] for e in job.get("environments", [])}
            for t in job["tasks"]:
                if "spark_python_task" in t:
                    assert t.get("environment_key") in envs, (name, t["task_key"])


def test_ddl_tasks_in_numeric_order_and_complete():
    job = _load(os.path.join(ROOT, "resources", "uc_setup.yml"))["resources"]["jobs"]["dam_uc_setup"]
    paths = [os.path.basename(t["sql_task"]["file"]["path"]).replace("${var.security_mode}", m) for t in job["tasks"]
             if "sql_task" in t and "/sql/ddl/" in t["sql_task"]["file"]["path"]
             for m in (sorted(SECURITY_MODES) if "${var.security_mode}" in t["sql_task"]["file"]["path"] else ("",))]
    assert paths == sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "sql", "ddl", "*.sql")))


def test_alert_schedules_are_staggered_and_use_gold():
    alerts = _load(os.path.join(ROOT, "resources", "alerts.yml"))["resources"]["alerts"]
    crons = [a["schedule"]["quartz_cron_schedule"] for a in alerts.values()]
    offsets = {tuple(c.split()[:2]) for c in crons}
    assert len(offsets) == len(crons)
    for a in alerts.values():
        assert re.search(r"\.gold\.", a["query_text"])
        assert a["schedule"]["pause_status"] == "${var.alert_paused}"


def test_prod_runs_as_service_principal_and_dev_alerts_paused():
    cfg = _load(os.path.join(ROOT, "databricks.yml"))
    assert "service_principal_name" in cfg["targets"]["prod"]["run_as"]
    assert cfg["targets"]["dev"]["variables"]["alert_paused"] == "PAUSED"
    assert cfg["targets"]["prod"]["variables"]["catalog"] == "dam_prod"


def test_formula_seed_runs_after_formula_ddl_and_before_dependants():
    job = _load(os.path.join(ROOT, "resources", "uc_setup.yml"))["resources"]["jobs"]["dam_uc_setup"]
    by = {t["task_key"]: t for t in job["tasks"]}
    assert [d["task_key"] for d in by["seed_ref_formula"]["depends_on"]] == ["ddl_03_ref_formula"]
    assert by["seed_ref_formula"]["sql_task"]["file"]["path"] == "../sql/seed/ref_formula_seed.sql"


def test_app_is_part_of_bundle_and_synced():
    """Steering 6 (App Developer dd07fe27): app lives in this bundle."""
    cfg = _load(os.path.join(ROOT, "databricks.yml"))
    excl = cfg["sync"]["exclude"]
    assert "app/**" not in excl and "app/" not in excl
    for e in ("app/.venv/**", "app/frontend/node_modules/**"):
        assert e in excl
    assert not any(e.startswith("app/frontend/dist") or e.startswith("app/backend") for e in excl)
    assert not os.path.exists(os.path.join(ROOT, "app", "resources", "dam_safety_app.yml"))
    app = _load(os.path.join(ROOT, "resources", "app.yml"))["resources"]["apps"]["dam_safety_app"]
    assert os.path.isfile(os.path.join(ROOT, "resources", os.path.normpath(app["source_code_path"]), "app.yaml"))
    gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read().splitlines()
    assert "!app/frontend/dist/" in gi and gi.index("!app/frontend/dist/") > gi.index("dist/")


def test_app_yml_has_no_synced_table_wired():
    """SCHEMA_CONTRACT v1.1 §7.3 decision: latest values via SQL warehouse OBO + UC row filters."""
    app = _load(os.path.join(ROOT, "resources", "app.yml"))["resources"]
    assert "postgres_synced_tables" not in app
    assert "sql" in app["apps"]["dam_safety_app"]["user_api_scopes"]


# ---- Databricks SME go-live answers (2026-11-02; /workspace/databricks-kb 02, 03, 05, 07, summary) ----------------
PIPELINE_OWNED = ("raw.reading_raw", "raw.reading_quarantine", "silver.reading_clean", "silver.reading_quarantine",
                  "silver.incl_survey", "gold.instrument_latest", "gold.instrument_daily", "gold.data_freshness")


def _sql(name):
    return open(os.path.join(ROOT, "sql", "ddl", name), encoding="utf-8").read()


def test_no_alter_table_row_filter_on_pipeline_tables():
    # KB 03: LDP row_filter is managed only through the pipeline definition, never ALTER TABLE
    for f in glob.glob(os.path.join(ROOT, "sql", "**", "*.sql"), recursive=True):
        s = re.sub(r"--[^\n]*", "", open(f, encoding="utf-8").read())
        for t in PIPELINE_OWNED:
            assert not re.search(rf"ALTER\s+TABLE\s+{re.escape(t)}\b[^;]*ROW\s+FILTER", s, re.I), (f, t)


def test_pipeline_runs_as_sp_and_carries_security_mode():
    p = _load(os.path.join(ROOT, "resources", "pipeline.yml"))["resources"]["pipelines"]["dam_pipeline"]
    assert p["run_as"] == {"service_principal_name": "${var.runtime_sp_app_id}"}
    assert p["configuration"]["dam.security_mode"] == "${var.security_mode}"
    v = _load(os.path.join(ROOT, "databricks.yml"))["variables"]
    assert v["security_mode"]["default"] == "table" and "dam-portfolio-all" in v["runtime_sp_app_id"]["description"]


def test_security_modes_are_mutually_exclusive():
    # UC_ABAC_MULTIPLE_ROW_FILTERS (KB 02): abac mode has no SET ROW FILTER and the pipeline drops row_filter=
    table, abac = _sql("10a_row_filters_table.sql"), _sql("10a_row_filters_abac.sql")
    strip = lambda s: re.sub(r"--[^\n]*", "", s)                                      # noqa: E731
    assert "SET ROW FILTER" in strip(table) and "CREATE POLICY" not in strip(table)
    assert "SET ROW FILTER" not in strip(abac) and "CREATE POLICY" in strip(abac)
    set_tables = set(re.findall(r"ALTER TABLE (\S+)\s+SET ROW FILTER", strip(table)))
    assert set_tables == set(re.findall(r"ALTER TABLE (\S+)\s+DROP ROW FILTER", strip(abac)))
    assert "EXCEPT `dam-portfolio-all`" in abac and "UNVERIFIED" in abac
    assert "SET ROW FILTER" not in strip(_sql("10_security.sql"))
    for f in ("bronze.py", "silver.py", "gold.py"):
        s = open(os.path.join(ROOT, "src", "pipelines", f)).read()
        assert 'if SECURITY_MODE == "table" else None' in s


def test_alerts_run_as_sp_and_keep_empty_ok():
    for a in _load(os.path.join(ROOT, "resources", "alerts.yml"))["resources"]["alerts"].values():
        assert a["run_as"] == {"service_principal_name": "${var.runtime_sp_app_id}"}
        assert a["evaluation"]["empty_result_state"] == "OK"


def test_lakebase_ha_group_min_equals_max_and_prod_no_scale_to_zero():
    lb = _load(os.path.join(ROOT, "resources", "lakebase.yml"))["resources"]["postgres_endpoints"]["dam_lakebase_primary"]
    assert lb["group"] == {"min": "${var.lb_ha_nodes}", "max": "${var.lb_ha_nodes}"}
    prod = _load(os.path.join(ROOT, "databricks.yml"))["targets"]["prod"]["variables"]
    assert prod["lb_ha_nodes"] == 2 and prod["lb_no_suspension"] is True


def test_uc_setup_env_6():
    job = _load(os.path.join(ROOT, "resources", "uc_setup.yml"))["resources"]["jobs"]["dam_uc_setup"]
    assert [e["spec"]["environment_version"] for e in job["environments"]] == ["6"]
