"""Shared bootstrap for spark_python_task entry points (serverless jobs).
The bundle syncs src/ to the workspace; we add it to sys.path so `dam_safety` imports
without building a wheel (KB: keep serverless env minimal)."""
import argparse
import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def args(*extra):
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    for name, default in extra:
        p.add_argument(f"--{name}", default=default)
    a, _ = p.parse_known_args()
    from dam_safety.catalog import normalize_catalog  # QA-055: one shared check (src/ is on sys.path above)
    try:
        a.catalog = normalize_catalog(a.catalog)     # canonical name from here on (== "dam_prod" checks downstream)
    except ValueError as e:
        raise SystemExit(str(e)) from None
    return a


def spark():
    from pyspark.sql import SparkSession
    return SparkSession.builder.getOrCreate()
