"""QA-045d: re-verify every dam's reviewer hash chain in UC (audit.review_record) against the heads stored in the
append-only audit.chain_head (written by dam_lakebase_extract). No caller supplies a head: the latest stored head is
used and every stored head is checked as a checkpoint (dam_safety.audit_chain.verify_with_chain_heads). Any chain
that is not ok fails the task (so the job's failure notification fires), after printing every verdict.

Signer: none exists yet (FERC requires no in-app crypto signature; docs/VERIFICATION.md QA-045c). FAIL CLOSED: with
--catalog dam_prod the task refuses to start (before any read) unless a real, non-no-op signer is configured
(--chain_signer or env DAM_CHAIN_SIGNER; audit_chain.require_signer_for_catalog). Until a signer exists the prod task
therefore fails every run, by design. dam_dev / dam_test run with the no-op signer. UNVERIFIED: never run on a
workspace.
"""
import json
import os

try:
    from . import _common  # noqa: F401
except ImportError:        # executed as a script by spark_python_task
    import _common         # noqa: F401
from dam_safety.audit_chain import (CHAIN_FIELDS, load_signer, require_signer_for_catalog,
                                    verify_with_chain_heads)
from dam_safety.rule_flag_authority import rule_flag_authority_problems


def verify_all(records, heads, signer=None):
    """records: audit.review_record rows with action_type set (chained). heads: all audit.chain_head rows.
    Returns one verdict per chain that has records OR a head (a chain whose records were all deleted still has its
    head rows, so it is checked and fails)."""
    chains = sorted({(r["dam_id"], r["chain_id"]) for r in records} | {(h["dam_id"], h["chain_id"]) for h in heads})
    out = []
    for dam_id, chain_id in chains:
        rows = [r for r in records if r["dam_id"] == dam_id and r["chain_id"] == chain_id]
        v = verify_with_chain_heads(rows, heads, dam_id, chain_id, signer)
        out.append({"dam_id": dam_id, **v})
    return out


def authority_failures(records, authority, app_roles=None):
    """§7.13: alarm_rule records re-checked against ref.v_dam_reviewer_authority at signed_ts. Returns (errors, warns)."""
    probs = rule_flag_authority_problems(records, authority, app_roles)
    return [p for p in probs if p["severity"] == "error"], [p for p in probs if p["severity"] != "error"]


def start():
    """Job start: parse args, build the signer, fail closed for prod. Returns (args, signer). No data is read."""
    a = _common.args(("chain_signer", os.environ.get("DAM_CHAIN_SIGNER", "none")))
    signer = load_signer(a.chain_signer)
    require_signer_for_catalog(a.catalog, signer)
    return a, signer


def main():
    a, signer = start()
    spark = _common.spark()
    cols = ", ".join(sorted(set(CHAIN_FIELDS) | {"record_hash", "signature", "sig_alg", "key_id"}))
    records = [r.asDict() for r in spark.sql(
        f"SELECT {cols} FROM {a.catalog}.audit.review_record WHERE action_type IS NOT NULL").collect()]
    heads = [r.asDict() for r in spark.sql(
        f"SELECT dam_id, chain_id, chain_seq, head_hash, CAST(computed_utc AS STRING) AS computed_utc, job_run_id "
        f"FROM {a.catalog}.audit.chain_head").collect()]
    res = verify_all(records, heads, signer)
    for v in res:
        print(json.dumps({k: v.get(k) for k in ("dam_id", "chain_id", "records", "heads_checked", "ok", "reason",
                                                 "first_broken_seq", "detail")}, default=str))
    authority = [r.asDict() for r in spark.sql(
        f"SELECT dam_id, principal, CAST(granted_ts AS STRING) AS granted_ts, CAST(revoked_ts AS STRING) AS revoked_ts "
        f"FROM {a.catalog}.ref.v_dam_reviewer_authority").collect()]
    errors, warns = authority_failures(records, authority)     # no app-role source in UC yet (module doc)
    for p in errors + warns:
        print(json.dumps(p, default=str))
    bad = [v for v in res if not v["ok"]]
    if bad or errors:
        raise SystemExit(f"{len(bad)} reviewer chain(s) failed verification, {len(errors)} alarm_rule authority "
                         "error(s): " + ", ".join([f"{v['dam_id']}:{v['reason']}@{v['first_broken_seq']}" for v in bad]
                                                  + [f"{p['dam_id']}:{p['code']}@{p['chain_seq']}" for p in errors]))


if __name__ == "__main__":
    main()
