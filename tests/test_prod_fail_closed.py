"""Genie 2026-11-09: jobs that verify reviewer chains refuse to start on dam_prod without a real (non-no-op) signer.
The only such job is src/jobs/verify_review_chain.py (checked below by scanning src/jobs)."""
import hashlib
import hmac
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "jobs"))
from dam_safety import audit_chain as ac  # noqa: E402
import _common  # noqa: E402
import verify_review_chain as vj  # noqa: E402


class _Real:
    alg, key_id = "HMAC-SHA256", "prod-k1"
    sign_key = verify_key = b"k"

    def sign(self, record_hash):
        return hmac.new(self.sign_key, record_hash.encode(), hashlib.sha256).hexdigest()

    def verify(self, record_hash, signature, key_id):
        return key_id == self.key_id and hmac.compare_digest(
            str(signature), hmac.new(self.verify_key, record_hash.encode(), hashlib.sha256).hexdigest())


class _BrokenKey(_Real):
    """Real-looking at config time, but the key material failed to load."""
    def sign(self, record_hash):
        raise RuntimeError("key material failed to load")


class _BrokenVerifyKey(_Real):
    def verify(self, record_hash, signature, key_id):
        raise OSError("public key file unreadable")


class _WrongKey(_Real):
    verify_key = b"another-key"          # signs with one key, verifies with another


class _AcceptsAnything(_Real):
    def verify(self, record_hash, signature, key_id):
        return True


class _NoSign(_Real):
    sign = None


class _Test(_Real):
    alg = "HMAC-SHA256-test"


@pytest.fixture()
def no_spark(monkeypatch):
    def boom():
        raise AssertionError("spark (data access) must not start")
    monkeypatch.setattr(_common, "spark", boom)
    monkeypatch.delenv("DAM_CHAIN_SIGNER", raising=False)


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["verify_review_chain.py", *argv])
    vj.main()


@pytest.mark.parametrize("extra", [[], ["--chain_signer", "none"], ["--chain_signer", "noop"]])
def test_prod_without_real_signer_refuses_before_any_read(monkeypatch, no_spark, extra):
    with pytest.raises(SystemExit, match="refusing to verify reviewer chains in dam_prod"):
        _run(monkeypatch, "--catalog", "dam_prod", *extra)


def test_prod_with_env_noop_signer_also_refuses(monkeypatch, no_spark):
    monkeypatch.setenv("DAM_CHAIN_SIGNER", "none")
    with pytest.raises(SystemExit, match="dam_prod"):
        _run(monkeypatch, "--catalog", "dam_prod")


def test_unknown_signer_spec_is_refused_everywhere(monkeypatch, no_spark):
    for cat in ("dam_dev", "dam_prod"):
        with pytest.raises(SystemExit, match="not implemented"):
            _run(monkeypatch, "--catalog", cat, "--chain_signer", "hmac-sha256")


@pytest.mark.parametrize("cat", ["dam_dev", "dam_test"])
def test_non_prod_starts_with_the_noop_signer(monkeypatch, no_spark, cat):
    with pytest.raises(AssertionError, match="spark"):          # got past the guard to the first data access
        _run(monkeypatch, "--catalog", cat)


def test_guard_semantics():
    for s in (None, ac.NoopSigner(), _Test()):
        with pytest.raises(ac.SignerConfigError):
            ac.require_signer_for_catalog("dam_prod", s)
    ac.require_signer_for_catalog("dam_prod", _Real())         # a real signer passes
    ac.require_signer_for_catalog("dam_dev", None)
    assert issubclass(ac.SignerConfigError, SystemExit)


def test_every_job_that_verifies_chains_calls_the_guard_first():
    jobs = os.path.join(ROOT, "src", "jobs")
    callers = []
    for f in sorted(os.listdir(jobs)):
        if f.endswith(".py"):
            src = open(os.path.join(jobs, f)).read()
            if re.search(r"\b(verify_records|verify_with_chain_heads|verify_all)\(", src):
                callers.append(f)
                body = src[src.index("def main"):]
                assert body.index("start()") < body.index("_common.spark()"), f
                assert "require_signer_for_catalog(" in src, f
    assert callers == ["verify_review_chain.py"]


# ---------------------------------------------------------------- QA-052: load-time key probe
@pytest.mark.parametrize("signer,why", [(_BrokenKey(), "sign\\(\\) raised RuntimeError"),
                                        (_BrokenVerifyKey(), "verify\\(\\) raised OSError"),
                                        (_WrongKey(), "wrong or mismatched key"),
                                        (_AcceptsAnything(), "different payload"),
                                        (_NoSign(), "no callable sign")])
def test_prod_refuses_a_signer_whose_key_round_trip_fails(signer, why):
    assert ac.is_real_signer(signer) or isinstance(signer, _NoSign)      # the shape check alone would pass
    with pytest.raises(ac.SignerConfigError, match="signer key probe failed.*" + why):
        ac.require_signer_for_catalog("dam_prod", signer)
    ac.require_signer_for_catalog("dam_dev", signer)                   # the probe gates prod only
    assert ac.signer_probe_problem(_Real()) is None


def test_broken_key_refused_at_job_start_before_any_read(monkeypatch, no_spark):
    monkeypatch.setattr(vj, "load_signer", lambda spec: _BrokenKey())
    with pytest.raises(SystemExit, match="signer key probe failed"):
        _run(monkeypatch, "--catalog", "dam_prod", "--chain_signer", "hmac")
    monkeypatch.setattr(vj, "load_signer", lambda spec: _WrongKey())
    with pytest.raises(SystemExit, match="wrong or mismatched key"):
        _run(monkeypatch, "--catalog", "dam_prod", "--chain_signer", "hmac")
    monkeypatch.setattr(vj, "load_signer", lambda spec: _Real())
    with pytest.raises(AssertionError, match="spark"):                  # a working key gets past the guard
        _run(monkeypatch, "--catalog", "dam_prod", "--chain_signer", "hmac")


# ---------------------------------------------------------------- QA-053: verify_all enforces it too
def _one_chain():
    r = {"review_id": "RS-1", "dam_id": "SYN", "subject_type": "alarm_event", "subject_id": "E1",
         "review_level": "engineer", "decision": "acknowledge", "comment": "c", "reviewer_id": "u",
         "reviewer_pe_license": None, "signed_ts": "2026-11-09T00:00:00+00:00", "signature_hash": "c" * 64,
         "action_type": "alarm_ack", "reviewer_email": "a@x", "reviewer_sub": None, "original_value": "{}",
         "corrected_value": "{}", "chain_id": "SYN", "chain_seq": 1, "prev_hash": ac.GENESIS, "signature": None,
         "sig_alg": None, "key_id": None}
    r["record_hash"] = ac.record_hash(r)
    return [r], [{"dam_id": "SYN", "chain_id": "SYN", "chain_seq": 1, "head_hash": r["record_hash"],
                  "computed_utc": "2026-11-09T00:00:00", "job_run_id": "r1"}]


@pytest.mark.parametrize("catalog", ["dam_prod", None])
@pytest.mark.parametrize("signer", [None, ac.NoopSigner(), _Test(), _BrokenKey()])
def test_verify_all_called_directly_on_prod_without_a_working_signer_refuses(catalog, signer):
    rows, heads = _one_chain()
    out = vj.verify_all(rows, heads, signer, catalog=catalog) if catalog else vj.verify_all(rows, heads, signer)
    assert [(v["dam_id"], v["ok"], v["reason"]) for v in out] == [("SYN", False, "signer_refused")]
    assert "refusing to verify reviewer chains in dam_prod" in out[0]["detail"]


def test_verify_all_still_verifies_on_dev_and_main_passes_the_catalog():
    rows, heads = _one_chain()
    assert [v["ok"] for v in vj.verify_all(rows, heads, None, catalog="dam_dev")] == [True]
    src = open(os.path.join(ROOT, "src", "jobs", "verify_review_chain.py")).read()
    assert "verify_all(records, heads, signer, catalog=a.catalog)" in src
