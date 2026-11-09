"""Lakebase migration helpers for the upgrade-path tests: the up migrations after a given one, in order, so an
upgrade test that checks the CURRENT validator brings the database fully up to date first."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LB_MIG = os.path.join(ROOT, "lakebase", "migrations")


def ups():
    return sorted(f for f in os.listdir(LB_MIG) if re.match(r"v\d+_\d+_\d+_\w+\.sql$", f) and not f.endswith("_down.sql"))


def later_than(version: str):
    """Paths of the up migrations whose name sorts after `version` (e.g. 'v1_7_006')."""
    return [os.path.join(LB_MIG, f) for f in ups() if f[:8] > version]
