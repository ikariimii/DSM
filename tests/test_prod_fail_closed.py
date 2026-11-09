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

    def verify(self, record_hash, signature, key_id):
        return hmac.compare_digest(signature, hmac.new(b"k", record_hash.encode(), hashlib.sha256).hexdigest())


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
