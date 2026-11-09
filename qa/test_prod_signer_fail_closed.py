"""QA-052+: prod fail-closed for verify_review_chain (independent of tests/test_prod_fail_closed.py).

Derived from SCHEMA / job docstring + dam_safety.audit_chain.require_signer_for_catalog /
load_signer: dam_prod must not verify under Noop or an unknown/unloadable signer; check runs before any
Spark read; dam_dev/dam_test may use Noop; no env bypass.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "jobs"))
sys.path.insert(0, str(ROOT / "app"))

from dam_safety import audit_chain as ac  # noqa: E402
import _common  # noqa: E402
import verify_review_chain as vj  # noqa: E402


class _Real:
    """Working HMAC pair (sign + verify) with a test key — not a stub that always passes."""
    alg, key_id = "HMAC-SHA256", "prod-k1"
    _key = b"qa-prod-test-key"

    def sign(self, record_hash):
        return hmac.new(self._key, record_hash.encode(), hashlib.sha256).hexdigest()

    def verify(self, record_hash, signature, key_id):
        if key_id != self.key_id or signature is None:
            return False
        return hmac.compare_digest(signature, self.sign(record_hash))


class _BrokenKey(_Real):
    """Shape looks real; sign() raises (unloadable key material)."""
    def sign(self, record_hash):
        raise RuntimeError("key material failed to load")


class _AlwaysVerifyTrue(_Real):
    """Signs correctly but verify() vouches for everything — must fail the probe."""
    def verify(self, record_hash, signature, key_id):
        return True


class _TestOnly(_Real):
    alg = "HMAC-SHA256-test"


@pytest.fixture()
def no_spark(monkeypatch):
    def boom():
        raise AssertionError("spark (data access) must not start")
    monkeypatch.setattr(_common, "spark", boom)
    monkeypatch.delenv("DAM_CHAIN_SIGNER", raising=False)
    monkeypatch.delenv("DEMO_MODE", raising=False)


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["verify_review_chain.py", *argv])
    return vj.main()


def _start(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["verify_review_chain.py", *argv])
    return vj.start()


# --------------------------------------------------------------------------- load_signer: empty / garbage / case / whitespace
@pytest.mark.parametrize("spec", [None, "", "  ", "none", "NONE", " NoOp ", "noop", "NoOp"])
def test_load_signer_noop_variants(spec):
    s = ac.load_signer(spec)
    assert isinstance(s, ac.NoopSigner)
    assert not ac.is_real_signer(s)
    assert not ac.is_enforcing(s)


@pytest.mark.parametrize("spec", ["hmac", "hmac-sha256", "garbage", "kms:alias/foo", "1", "true", "real"])
def test_unknown_signer_spec_refused(spec):
    with pytest.raises(ac.SignerConfigError, match="not implemented"):
        ac.load_signer(spec)


def test_signer_spec_present_but_unimplemented_is_startup_failure_not_silent_noop(no_spark, monkeypatch):
    """Until a real loader exists, a 'present' spec must refuse — never fall through to Noop."""
    with pytest.raises(SystemExit, match="not implemented"):
        _run(monkeypatch, "--catalog", "dam_dev", "--chain_signer", "hmac-sha256")
    with pytest.raises(SystemExit, match="not implemented"):
        _run(monkeypatch, "--catalog", "dam_prod", "--chain_signer", "kms:prod")


# --------------------------------------------------------------------------- dam_prod refuses Noop / test-only / broken
@pytest.mark.parametrize("extra", [
    [],
    ["--chain_signer", "none"],
    ["--chain_signer", "noop"],
    ["--chain_signer", "NONE"],
    ["--chain_signer", "  noop  "],
])
def test_dam_prod_refuses_noop_and_variants_before_spark(no_spark, monkeypatch, extra):
    with pytest.raises(SystemExit, match="refusing to verify reviewer chains in dam_prod"):
        _run(monkeypatch, "--catalog", "dam_prod", *extra)


def test_dam_prod_refuses_test_only_signer_alg(no_spark, monkeypatch):
    # inject a loader that returns a *-test signer (would be a future misconfig)
    monkeypatch.setattr(ac, "load_signer", lambda spec: _TestOnly())
    monkeypatch.setattr(vj, "load_signer", lambda spec: _TestOnly())
    with pytest.raises(SystemExit, match="dam_prod"):
        _run(monkeypatch, "--catalog", "dam_prod", "--chain_signer", "hmac-sha256-test")


def test_require_signer_rejects_broken_looking_signers():
    for s in (None, ac.NoopSigner(), _TestOnly(), object()):
        with pytest.raises(ac.SignerConfigError):
            ac.require_signer_for_catalog("dam_prod", s)
    ac.require_signer_for_catalog("dam_prod", _Real())
    with pytest.raises(ac.SignerConfigError, match="signer key probe failed"):
        ac.require_signer_for_catalog("dam_prod", _BrokenKey())


def test_signer_whose_key_fails_to_load_is_rejected_by_require_signer():
    """QA-052 closed: load-time probe rejects unloadable keys on dam_prod."""
    broken = _BrokenKey()
    assert ac.is_real_signer(broken)  # shape alone would have passed the old check
    with pytest.raises(ac.SignerConfigError, match="signer key probe failed"):
        ac.require_signer_for_catalog("dam_prod", broken)
    # probe gates prod only
    ac.require_signer_for_catalog("dam_dev", broken)
    assert ac.signer_probe_problem(_Real()) is None


def test_signer_that_verifies_everything_fails_probe():
    """Probe must not be satisfied by a signer that signs but verifies any payload."""
    bad = _AlwaysVerifyTrue()
    assert ac.is_real_signer(bad)
    why = ac.signer_probe_problem(bad)
    assert why and "different payload" in why
    with pytest.raises(ac.SignerConfigError, match="different payload"):
        ac.require_signer_for_catalog("dam_prod", bad)



def test_fail_closed_also_enforced_at_verify_time_not_only_startup():
    """QA-053 closed: verify_all re-applies the guard; catalog None = prod."""
    rows = [{
        "review_id": "RS-1", "dam_id": "CRD", "subject_type": "alarm_event", "subject_id": "E1",
        "review_level": "engineer", "decision": "acknowledge", "comment": "c", "reviewer_id": "u",
        "reviewer_pe_license": None, "signed_ts": "2026-11-09T00:00:00.000000+00:00",
        "signature_hash": "c" * 64, "action_type": "alarm_ack", "reviewer_email": "a@x",
        "reviewer_sub": None, "original_value": "{}", "corrected_value": "{}",
        "chain_id": "CRD", "chain_seq": 1, "prev_hash": ac.GENESIS,
        "record_hash": None, "signature": None, "sig_alg": None, "key_id": None,
    }]
    rows[0]["record_hash"] = ac.record_hash(rows[0])
    heads = [{"dam_id": "CRD", "chain_id": "CRD", "chain_seq": 1, "head_hash": rows[0]["record_hash"],
              "computed_utc": "2026-11-09T00:00:00", "job_run_id": "r1"}]
    for cat in ("dam_prod", None):
        out = vj.verify_all(rows, heads, ac.NoopSigner(), catalog=cat) if cat else vj.verify_all(rows, heads, ac.NoopSigner())
        assert out and out[0]["ok"] is False and out[0]["reason"] == "signer_refused", (cat, out)


def test_verify_all_no_catalog_does_not_break_dev_when_catalog_passed():
    """catalog=None is fail-closed (prod); legitimate dev/test callers must pass catalog explicitly."""
    rows = [{
        "review_id": "RS-1", "dam_id": "CRD", "subject_type": "alarm_event", "subject_id": "E1",
        "review_level": "engineer", "decision": "acknowledge", "comment": "c", "reviewer_id": "u",
        "reviewer_pe_license": None, "signed_ts": "2026-11-09T00:00:00.000000+00:00",
        "signature_hash": "c" * 64, "action_type": "alarm_ack", "reviewer_email": "a@x",
        "reviewer_sub": None, "original_value": "{}", "corrected_value": "{}",
        "chain_id": "CRD", "chain_seq": 1, "prev_hash": ac.GENESIS,
        "record_hash": None, "signature": None, "sig_alg": None, "key_id": None,
    }]
    rows[0]["record_hash"] = ac.record_hash(rows[0])
    heads = [{"dam_id": "CRD", "chain_id": "CRD", "chain_seq": 1, "head_hash": rows[0]["record_hash"],
              "computed_utc": "2026-11-09T00:00:00", "job_run_id": "r1"}]
    assert vj.verify_all(rows, heads, ac.NoopSigner(), catalog="dam_dev")[0]["ok"] is True
    assert vj.verify_all(rows, heads, ac.NoopSigner(), catalog="dam_test")[0]["ok"] is True
    # every production caller in src/jobs passes catalog=
    src_job = (ROOT / "src/jobs/verify_review_chain.py").read_text()
    assert "verify_all(records, heads, signer, catalog=a.catalog)" in src_job
    # every src/jobs + tests *call* of verify_all must pass catalog= (docstrings ignored)
    import re as _re
    call_re = _re.compile(r"""(?m)^(?P<indent>\s*)(?:[\w\.]+\s*=\s*|return\s+)?verify_all\(""")
    for f in list((ROOT / "tests").rglob("*.py")) + [ROOT / "src/jobs/verify_review_chain.py"]:
        text = f.read_text()
        for m in call_re.finditer(text):
            i = text[:m.start()].count("\n")
            block = "\n".join(text.splitlines()[i:i + 8])
            if "catalog=" in block:
                continue
            if f.name in ("test_prod_fail_closed.py", "test_prod_signer_fail_closed.py"):
                continue  # intentional omit / None = prod refusal cases live here
            raise AssertionError(f"{f.name}:{i+1} verify_all call without catalog=: {block.splitlines()[0].strip()}")


# --------------------------------------------------------------------------- env / DEMO_MODE / bypasses
@pytest.mark.parametrize("env_val", ["none", "NONE", " noop ", "noop", ""])
def test_env_dam_chain_signer_noop_variants_still_refuse_prod(no_spark, monkeypatch, env_val):
    monkeypatch.setenv("DAM_CHAIN_SIGNER", env_val)
    with pytest.raises(SystemExit, match="dam_prod"):
        _run(monkeypatch, "--catalog", "dam_prod")


def test_no_env_override_bypasses_prod_guard(no_spark, monkeypatch):
    for k, v in [
        ("DAM_ALLOW_NOOP_PROD", "1"),
        ("DAM_CHAIN_SIGNER_BYPASS", "1"),
        ("DAM_SKIP_SIGNER_CHECK", "true"),
        ("ALLOW_INSECURE", "1"),
        ("DEMO_MODE", "true"),
    ]:
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DAM_CHAIN_SIGNER", "none")
    with pytest.raises(SystemExit, match="dam_prod"):
        _run(monkeypatch, "--catalog", "dam_prod")


def test_demo_mode_true_with_dam_prod_job_still_fail_closed(no_spark, monkeypatch):
    """QA-041-adjacent: DEMO_MODE must not weaken the verify job's prod guard."""
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DAM_CATALOG", "dam_prod")
    with pytest.raises(SystemExit, match="dam_prod"):
        _run(monkeypatch, "--catalog", "dam_prod", "--chain_signer", "none")


def test_app_settings_refuse_demo_mode_with_prod_catalog():
    """QA-041 closed on 7c71ff3: Settings.check() refuses DEMO_MODE + dam_prod."""
    from backend.config import Settings
    s = Settings(demo_mode=True, catalog="dam_prod", warehouse_id="w", sql_on_behalf_of_user=True)
    with pytest.raises(RuntimeError, match="QA-041"):
        s.check()


# --------------------------------------------------------------------------- catalog naming
def test_unknown_catalog_refused_including_bare_prod_alias(no_spark, monkeypatch):
    """QA-055: unknown/garbled names fail closed via normalize_catalog (message may say QA-055, not
    the older 'unknown catalog'). DAM_PROD / trailing-space normalize to dam_prod (then prod guard)."""
    with pytest.raises(SystemExit, match=r"QA-055|unknown catalog|refusing catalog"):
        _run(monkeypatch, "--catalog", "prod")
    # case / trailing space now normalize to dam_prod → prod signer guard (not 'unknown')
    with pytest.raises(SystemExit, match=r"dam_prod|QA-055|unknown catalog|refusing catalog"):
        _run(monkeypatch, "--catalog", "DAM_PROD")
    with pytest.raises(SystemExit, match=r"dam_prod|QA-055|unknown catalog|refusing catalog"):
        _run(monkeypatch, "--catalog", "dam_prod ")


def test_only_dam_prod_is_in_prod_catalogs_constant():
    assert ac.PROD_CATALOGS == ("dam_prod",)


# --------------------------------------------------------------------------- dev/test still work with Noop
@pytest.mark.parametrize("cat", ["dam_dev", "dam_test"])
def test_dev_and_test_reach_spark_with_noop(no_spark, monkeypatch, cat):
    with pytest.raises(AssertionError, match="spark"):
        _run(monkeypatch, "--catalog", cat)
    with pytest.raises(AssertionError, match="spark"):
        _run(monkeypatch, "--catalog", cat, "--chain_signer", "none")


def test_start_returns_noop_for_dev_and_real_passes_prod(no_spark, monkeypatch):
    a, signer = _start(monkeypatch, "--catalog", "dam_dev")
    assert a.catalog == "dam_dev" and isinstance(signer, ac.NoopSigner)
    monkeypatch.setattr(vj, "load_signer", lambda spec: _Real())
    a2, s2 = _start(monkeypatch, "--catalog", "dam_prod", "--chain_signer", "hmac")
    assert a2.catalog == "dam_prod" and ac.is_real_signer(s2)


def test_startup_guard_runs_before_any_data_access(no_spark, monkeypatch):
    """Order: start()/require_signer before spark() — spark boom must not fire on dam_prod+noop."""
    with pytest.raises(SystemExit, match="dam_prod") as ei:
        _run(monkeypatch, "--catalog", "dam_prod")
    assert "spark" not in str(ei.value).lower()

def test_cli_none_overrides_env_real_looking_spec(no_spark, monkeypatch):
    """CLI --chain_signer none must win over a weird env value for the default; prod still refuses."""
    monkeypatch.setenv("DAM_CHAIN_SIGNER", "hmac-sha256")
    # argparse default is env at parse definition time in start(); re-import path uses env as default for the flag
    # When CLI passes none explicitly, that value is used:
    with pytest.raises(SystemExit, match="dam_prod"):
        _run(monkeypatch, "--catalog", "dam_prod", "--chain_signer", "none")


def test_signerconfigerror_is_systemexit():
    assert issubclass(ac.SignerConfigError, SystemExit)


def test_only_verify_review_chain_job_calls_guard():
    import re
    jobs = ROOT / "src" / "jobs"
    callers = []
    for f in sorted(jobs.glob("*.py")):
        src = f.read_text()
        if re.search(r"\b(verify_records|verify_with_chain_heads|verify_all)\(", src):
            callers.append(f.name)
            body = src[src.index("def main"):]
            assert body.index("start()") < body.index("_common.spark()"), f.name
            assert "require_signer_for_catalog(" in src
    assert callers == ["verify_review_chain.py"]
