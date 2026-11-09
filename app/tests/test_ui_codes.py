"""Every refusal code the steering asked the UI to explain (DA DSSMP / CDSE load / reversion publisher / readiness)
has a message in frontend/src/codes.ts, and the app's own coded refusals do too."""
import re
from pathlib import Path

CODES_TS = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "codes.ts").read_text()
KEYS = set(re.findall(r"^\s+([a-z][a-z0-9_]*): \"", CODES_TS, re.M))

REQUIRED = {
    # DSSMP (mirrored from the DA dssmp check)
    "dssmp_not_yet_effective", "dssmp_status_unknown", "dssmp_missing", "dssmp_not_found", "dssmp_superseded",
    "dssmp_draft", "dssmp_other_dam", "dssmp_unverifiable", "dssmp_document_hash_mismatch",
    # CDSE load job refusals (src/dam_safety/cdse_load.py)
    "cdse_exists", "cdse_stale", "approval_record_missing", "approval_record_wrong_action", "approval_record_mismatch",
    "approval_not_authorized", "invalid_value", "missing_required_column",
    # rule re-version publisher refusals
    "reversion_approval_missing", "reversion_approval_wrong_action", "reversion_approval_mismatch",
    "reversion_not_authorized", "reversion_fields_changed", "payload_invalid",
    # go-live readiness (ref.v_dam_go_live_readiness + the app's fail-closed codes)
    "no_cdse_in_reviewer_authority", "go_live_readiness_missing", "go_live_readiness_unverifiable", "go_live_not_ready",
}


def test_every_required_code_has_a_ui_message():
    assert REQUIRED - KEYS == set()


def test_the_backend_readiness_codes_are_the_ones_the_ui_explains():
    from backend import onboarding as ob
    assert {ob.READINESS_MISSING, ob.READINESS_UNVERIFIABLE} <= KEYS


def _da_tuple(module: str, name: str) -> set[str]:
    """The DA module's declared code tuple, read as text (the app never imports src/)."""
    src = Path(__file__).resolve().parents[2] / "src" / "dam_safety" / f"{module}.py"
    if not src.exists():
        return set()
    m = re.search(rf"^{name} = [({{](.*?)[)}}]", src.read_text(), re.S | re.M)
    return set(re.findall(r'"([a-z][a-z0-9_]*)"', m.group(1))) if m else set()


def test_the_da_declared_codes_all_have_ui_messages():
    da = _da_tuple("cdse_load", "CODES") | _da_tuple("rule_reversion_authority", "PUB_CODES") \
        | _da_tuple("dssmp_check", "CODES")
    assert da - KEYS == set(), da - KEYS
