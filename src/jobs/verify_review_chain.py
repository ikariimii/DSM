"""QA-045d: re-verify every dam's reviewer hash chain in UC (audit.review_record) against the heads stored in the
append-only audit.chain_head (written by dam_lakebase_extract). No caller supplies a head: the latest stored head is
used and every stored head is checked as a checkpoint (dam_safety.audit_chain.verify_with_chain_heads). Any chain
that is not ok fails the task (so the job's failure notification fires), after printing every verdict.

Signer: none exists yet (FERC requires no in-app crypto signature; docs/VERIFICATION.md QA-045c). FAIL CLOSED: with
--catalog dam_prod the task refuses to start (before any read) unless a real, non-no-op signer is configured
(--chain_signer or env DAM_CHAIN_SIGNER; audit_chain.require_signer_for_catalog). Until a signer exists the prod task
therefore fails every run, by design. dam_dev / dam_test run with the no-op signer.
QA-052: on dam_prod the signer must also pass a load-time key probe (sign a fixed payload, verify it with the same
signer; audit_chain.signer_probe_problem). QA-053: verify_all() re-applies the same guard itself (catalog not given =
production), so skipping start() cannot bypass it. authority_failures() checks §7.13 authority only and vouches for no
record integrity; main() reports it alongside verify_all(). UNVERIFIED: never run on a
workspace.
"""
import json
import os

try:
    from . import _common  # noqa: F401
except ImportError:        # executed as a script by spark_python_task
    import _common         # noqa: F401
from dam_safety.audit_chain import (CHAIN_FIELDS_V2, PROD_CATALOGS, SignerConfigError, load_signer,
                                    require_signer_for_catalog, verify_with_chain_heads)
from dam_safety.catalog import normalize_catalog
from dam_safety.dssmp_authority import dssmp_authority_problems
from dam_safety.rule_flag_authority import (SECOND_APPROVER_SETTING, rule_flag_authority_problems,
                                            second_approver_required)

# job parameter carrying the bundle variable cdse_flag_require_second_approver (same value as the app's env)
SECOND_APPROVER_ARG = "cdse_flag_require_second_approver"


def review_record_select_cols() -> str:
    """Columns the task SELECTs from audit.review_record: every canon-2 hashed field (CHAIN_FIELDS + actor_app_role +
    canon_version, v1.7 v1_7_002) plus record_hash and the signature fields. Missing either canon-2 column would turn
    every canon-2 record into hash_mismatch."""
    return ", ".join(sorted(set(CHAIN_FIELDS_V2) | {"record_hash", "signature", "sig_alg", "key_id"}))


def verify_all(records, heads, signer=None, catalog=None):
    """records: audit.review_record rows with action_type set (chained). heads: all audit.chain_head rows.
    Returns one verdict per chain that has records OR a head (a chain whose records were all deleted still has its
    head rows, so it is checked and fails).

    QA-053: the prod fail-closed check is enforced HERE too, not only in start(), so calling this entry point directly
    cannot bypass it. catalog None (not given) is treated as production (fail closed). When the guard refuses, nothing
    is verified and every chain gets ok=False, reason 'signer_refused' (the task then fails); with no chains at all a
    single refused verdict (dam_id/chain_id None) is returned, so a refusal is never an empty "nothing failed" list.
    QA-055 follow-up: any other name is canonicalized by catalog.normalize_catalog first; an unknown, empty, garbled
    or non-string name RAISES ValueError (never passes, never falls back to dev)."""
    chains = sorted({(r["dam_id"], r["chain_id"]) for r in records} | {(h["dam_id"], h["chain_id"]) for h in heads})
    canonical = PROD_CATALOGS[0] if catalog is None else normalize_catalog(catalog)      # ValueError = refuse
    try:
        require_signer_for_catalog(canonical, signer)
    except SignerConfigError as e:
        return [{"dam_id": d, "chain_id": c, "records": sum(1 for r in records if r["dam_id"] == d
                                                            and r["chain_id"] == c),
                 "ok": False, "reason": "signer_refused", "first_broken_seq": None, "detail": str(e)}
                for d, c in (chains or [(None, None)])]
    out = []
    for dam_id, chain_id in chains:
        rows = [r for r in records if r["dam_id"] == dam_id and r["chain_id"] == chain_id]
        v = verify_with_chain_heads(rows, heads, dam_id, chain_id, signer)
        out.append({"dam_id": dam_id, **v})
    return out


def authority_failures(records, authority, app_roles=None, require_second_approver=None, threshold_dssmp=None,
                       dssmp_versions=None):
    """§7.13: alarm_rule records re-checked against ref.v_dam_reviewer_authority at signed_ts. Returns (errors, warns).
    require_second_approver None = read CDSE_FLAG_REQUIRE_SECOND_APPROVER from the environment (app parsing).
    threshold_dssmp / dssmp_versions: DSSMP version of each applied rule must be approved and in effect (v1.7)."""
    probs = rule_flag_authority_problems(records, authority, app_roles, require_second_approver,
                                         threshold_dssmp=threshold_dssmp, dssmp_versions=dssmp_versions)
    probs += dssmp_authority_problems(records, authority, app_roles)      # v1.7 dssmp_version propose/approve/reject
    return [p for p in probs if p["severity"] == "error"], [p for p in probs if p["severity"] != "error"]


def start():
    """Job start: parse args, build the signer, fail closed for prod. Returns (args, signer). No data is read."""
    a = _common.args(("chain_signer", os.environ.get("DAM_CHAIN_SIGNER", "none")),
                     (SECOND_APPROVER_ARG, os.environ.get(SECOND_APPROVER_SETTING)))
    signer = load_signer(a.chain_signer)
    require_signer_for_catalog(a.catalog, signer)
    return a, signer


def main():
    a, signer = start()
    spark = _common.spark()
    cols = review_record_select_cols()
    records = [r.asDict() for r in spark.sql(
        f"SELECT {cols} FROM {a.catalog}.audit.review_record WHERE action_type IS NOT NULL").collect()]
    heads = [r.asDict() for r in spark.sql(
        f"SELECT dam_id, chain_id, chain_seq, head_hash, CAST(computed_utc AS STRING) AS computed_utc, job_run_id "
        f"FROM {a.catalog}.audit.chain_head").collect()]
    res = verify_all(records, heads, signer, catalog=a.catalog)
    for v in res:
        print(json.dumps({k: v.get(k) for k in ("dam_id", "chain_id", "records", "heads_checked", "ok", "reason",
                                                 "first_broken_seq", "detail")}, default=str))
    authority = [r.asDict() for r in spark.sql(
        f"SELECT dam_id, principal, CAST(granted_ts AS STRING) AS granted_ts, CAST(revoked_ts AS STRING) AS revoked_ts "
        f"FROM {a.catalog}.ref.v_dam_reviewer_authority").collect()]
    second = second_approver_required({SECOND_APPROVER_SETTING: getattr(a, SECOND_APPROVER_ARG)}
                                      if getattr(a, SECOND_APPROVER_ARG) is not None else {})
    print(json.dumps({SECOND_APPROVER_SETTING: second}))
    threshold_dssmp = {(r.threshold_id, r.threshold_version): r.dssmp_version_id for r in spark.sql(
        f"SELECT threshold_id, threshold_version, dssmp_version_id FROM {a.catalog}.ref.threshold").collect()}
    dssmp_versions = {r.dssmp_version_id: r.asDict() for r in spark.sql(
        f"SELECT dssmp_version_id, dam_id, status, effective_from, effective_to FROM {a.catalog}.ref.dssmp_version"
    ).collect()}
    errors, warns = authority_failures(records, authority, require_second_approver=second,
                                       threshold_dssmp=threshold_dssmp, dssmp_versions=dssmp_versions)
    for p in errors + warns:
        print(json.dumps(p, default=str))
    bad = [v for v in res if not v["ok"]]
    if bad or errors:
        raise SystemExit(f"{len(bad)} reviewer chain(s) failed verification, {len(errors)} alarm_rule authority "
                         "error(s): " + ", ".join([f"{v['dam_id']}:{v['reason']}@{v['first_broken_seq']}" for v in bad]
                                                  + [f"{p['dam_id']}:{p['code']}@{p['chain_seq']}" for p in errors]))


if __name__ == "__main__":
    main()
