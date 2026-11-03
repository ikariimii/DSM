"""Every published LDP dataset that carries dam_id has the UC dam row filter (SCHEMA_CONTRACT v1.1 §6, Q12:
the app reads gold via SQL warehouse on behalf of the user, so the filter must sit on the table itself).
Syntax per docs.databricks.com/aws/en/ldp/developer/ldp-python-ref-materialized-view (row_filter, Public Preview)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_DAM_ID = {'name="incl_survey_raw"', 'name="incl_quarantine"'}   # raw schema: dam-portfolio-all only


def _decorators():
    for f in ("bronze.py", "silver.py", "gold.py"):
        s = open(os.path.join(ROOT, "src", "pipelines", f)).read()
        for m in re.finditer(r"@dp\.(table|materialized_view)\((.*?)\)\s*\n(?:@dp\.[^\n]*\n)*def ", s, re.S):
            yield f, m.group(2)


def test_published_datasets_have_dam_row_filter():
    seen = 0
    for f, args in _decorators():
        if "private=True" in args or any(n in args for n in NO_DAM_ID):
            continue
        seen += 1
        assert "row_filter=DAM_ROW_FILTER" in args, (f, args[:80])
    assert seen == 8


def test_filter_expression_targets_ref_udf():
    for f in ("bronze.py", "silver.py", "gold.py"):
        s = open(os.path.join(ROOT, "src", "pipelines", f)).read()
        assert 'DAM_ROW_FILTER = f"ROW FILTER {CATALOG}.ref.dam_row_filter ON (dam_id)"' in s
