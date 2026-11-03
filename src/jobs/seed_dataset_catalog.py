"""Job dam_seed (task load_dataset_catalog): MERGE sql/seed/dataset_catalog.csv into ref.dataset_catalog.
Metadata only - no dataset content. In dam_prod only 'unrestricted' rows are loaded, so prod never
references an internal-test-only dataset (owner licensing decision 2026-11-02)."""
import csv
import datetime as dt
import os

try:
    from . import _common
except ImportError:
    import _common

ENUM = ("internal-test-only", "unrestricted")
SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sql", "seed", "dataset_catalog.csv")


def rows_for(catalog, path=SEED):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    for r in rows:
        if r["usage_restriction"] not in ENUM:      # mirrors CHECK ck_dataset_usage_restriction
            raise ValueError(f"{r['dataset_id']}: usage_restriction {r['usage_restriction']!r} not in {ENUM}")
    if catalog == "dam_prod":
        rows = [r for r in rows if r["usage_restriction"] == "unrestricted"]
    return rows


def main():
    a = _common.args()
    spark = _common.spark()
    now = dt.datetime.now(dt.timezone.utc)
    rows = [dict(r, loaded_ts=now) for r in rows_for(a.catalog)]
    spark.createDataFrame(rows).createOrReplaceTempView("seed_catalog")
    spark.sql(f"""MERGE INTO {a.catalog}.ref.dataset_catalog t USING seed_catalog s ON t.dataset_id = s.dataset_id
                  WHEN MATCHED THEN UPDATE SET family=s.family, title=s.title, license=s.license,
                       source_url=s.source_url, usage_restriction=s.usage_restriction,
                       restriction_basis=s.restriction_basis, manifest_path=s.manifest_path, loaded_ts=s.loaded_ts
                  WHEN NOT MATCHED THEN INSERT (dataset_id, family, title, license, source_url, usage_restriction,
                       restriction_basis, manifest_path, loaded_ts)
                  VALUES (s.dataset_id, s.family, s.title, s.license, s.source_url, s.usage_restriction,
                       s.restriction_basis, s.manifest_path, s.loaded_ts)""")
    if a.catalog == "dam_prod":   # remove anything restricted that an earlier load may have left
        spark.sql(f"DELETE FROM {a.catalog}.ref.dataset_catalog WHERE usage_restriction <> 'unrestricted'")


if __name__ == "__main__":
    main()
