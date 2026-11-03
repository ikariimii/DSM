"""Grounded engineering commentary (rpt.engineering_commentary).

Grounding contract:
  * The generator sees ONLY the facts passed in (findings, anomaly scores, PFM descriptions, DSSMP
    version) - no free retrieval.
  * Every sentence must carry ≥ 1 citation tag [F:<finding_id>] [P:<pfm_id>] [D:<dssmp_version_id>]
    [R:<reading_id>] [M:<model_id>] and every tag must exist in the supplied facts.
  * Every number in the text must appear in the supplied facts (no invented magnitudes).
  * If an LLM output fails any check it is discarded and the DETERMINISTIC_FALLBACK template is used.
  * requires_engineer_review is ALWAYS True (CHECK constraint chk_review in 09_rpt.sql). The text
    describes and cites; it never states that a dam is safe/unsafe or that an EAP level is reached -
    FERC-CH14 PDF p36: statistics are 'no substitute for judgment' of the engineer.
Confidence = min(confidence of cited findings), × 0.5 if any cited finding is INDETERMINATE; 0.1 if no
findings (UNVERIFIED heuristic, VERIFICATION.md INT-CONF)."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable

TAG = re.compile(r"\[([FPDRM]):([^\]]+)\]")
NUM = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?")
FORBIDDEN = re.compile(r"\b(is safe|is unsafe|no risk|guarantee|EAP (?:is |has been )?(?:activated|triggered)|"
                       r"evacuat\w*|failure is imminent)\b", re.I)
SYSTEM_PROMPT = (
    "You draft dam-safety monitoring commentary for review by a licensed engineer. Use ONLY the facts "
    "in FACTS. End every sentence with one or more citation tags exactly as given, e.g. [F:abc] [P:PFM-3]. "
    "Do not introduce numbers that are not in FACTS. Do not conclude whether the dam is safe, do not declare "
    "EAP levels, do not recommend evacuation. Distinguish probable sensor faults from probable real behaviour "
    "and state the classification confidence. Keep it under 200 words.")


def build_facts(dam_id, findings, anomalies=(), pfms=(), dssmp_version_id=None, models=()):
    return {"dam_id": dam_id, "dssmp_version_id": dssmp_version_id,
            "findings": [{k: f.get(k) for k in ("finding_id", "pfm_id", "severity", "classification", "confidence",
                                                 "window_start", "window_end", "evidence")} for f in findings],
            "anomalies": [{k: a.get(k) for k in ("reading_id", "sensor_id", "ts_utc", "residual", "z_score", "band",
                                                  "classification", "confidence", "model_id")} for a in anomalies],
            "pfms": [{k: p.get(k) for k in ("pfm_id", "title", "description", "ch17_disposition")} for p in pfms],
            "models": [{k: m.get(k) for k in ("model_id", "variant", "r2", "resid_sigma", "n_obs")} for m in models]}


def allowed_tags(facts) -> set:
    s = {("F", f["finding_id"]) for f in facts["findings"]}
    s |= {("P", f["pfm_id"]) for f in facts["findings"] if f.get("pfm_id")}
    s |= {("P", p["pfm_id"]) for p in facts["pfms"]}
    s |= {("R", a["reading_id"]) for a in facts["anomalies"] if a.get("reading_id")}
    s |= {("M", a["model_id"]) for a in facts["anomalies"] if a.get("model_id")}
    s |= {("M", m["model_id"]) for m in facts["models"]}
    if facts.get("dssmp_version_id"):
        s.add(("D", facts["dssmp_version_id"]))
    return s


def _sentences(text):
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+(?=[A-Z(])|(?<=\])\s*\n+", text.strip()) if x.strip()]


def validate_text(text: str, facts) -> list:
    """Returns list of violations (empty = grounded)."""
    errs = []
    ok_tags = allowed_tags(facts)
    fact_blob = json.dumps(facts, default=str)
    fact_nums = {float(n) for n in NUM.findall(fact_blob)}
    if FORBIDDEN.search(text):
        errs.append("forbidden conclusion: " + FORBIDDEN.search(text).group(0))
    for s in _sentences(text):
        tags = TAG.findall(s)
        if not tags:
            errs.append(f"uncited sentence: {s[:60]}")
        for t in tags:
            if t not in ok_tags:
                errs.append(f"unknown citation [{t[0]}:{t[1]}]")
        body = TAG.sub("", s)
        for n in NUM.findall(body):
            v = float(n)
            if v not in fact_nums and not any(abs(v - f) <= 0.005 * max(1.0, abs(f)) for f in fact_nums):
                errs.append(f"ungrounded number {n}")
    return errs


def deterministic_text(facts) -> str:
    lines = []
    dv = f" [D:{facts['dssmp_version_id']}]" if facts.get("dssmp_version_id") else ""
    if not facts["findings"] and not facts["anomalies"]:
        if dv:
            return f"No interpretation findings or residual anomalies were produced for {facts['dam_id']} in this period.{dv}"
        return ""
    for f in facts["findings"]:
        p = f" [P:{f['pfm_id']}]" if f.get("pfm_id") else ""
        lines.append(f"Finding {f['finding_id'][:8]} has severity {f['severity']} with classification "
                     f"{f['classification']} at confidence {f['confidence']}. [F:{f['finding_id']}]{p}{dv}")
    for a in facts["anomalies"][:10]:
        if a.get("band") in ("OUTSIDE_2S", "OUTSIDE_3S") and a.get("reading_id"):
            m = f" [M:{a['model_id']}]" if a.get("model_id") else ""
            lines.append(f"Sensor {a['sensor_id']} residual z-score {a['z_score']} ({a['band']}) is classified "
                         f"{a['classification']} at confidence {a['confidence']}. [R:{a['reading_id']}]{m}")
    lines.append("This draft is generated from monitoring data and requires review by the responsible engineer."
                 + (f" [F:{facts['findings'][0]['finding_id']}]" if facts["findings"] else
                    f" [R:{facts['anomalies'][0]['reading_id']}]" if facts["anomalies"] and facts["anomalies"][0].get("reading_id") else dv))
    return "\n".join(lines)


def confidence(facts) -> float:
    fs = facts["findings"]
    if not fs:
        return 0.1
    c = min(float(f["confidence"] or 0) for f in fs)
    if any(f["classification"] == "INDETERMINATE" for f in fs):
        c *= 0.5
    return round(c, 3)


def generate(facts, scope="dam", scope_id=None, period_start=None, period_end=None, created_ts=None,
             llm: Callable[[str, str], str] | None = None, llm_name: str | None = None) -> dict:
    """llm(system_prompt, user_prompt) -> text. Returns a rpt.engineering_commentary row dict."""
    user_prompt = "FACTS:\n" + json.dumps(facts, default=str, sort_keys=True, indent=1)
    prompt_hash = hashlib.sha256((SYSTEM_PROMPT + user_prompt).encode()).hexdigest()
    text, generator, rejected = None, "DETERMINISTIC_FALLBACK", []
    if llm is not None:
        try:
            cand = llm(SYSTEM_PROMPT, user_prompt) or ""
            rejected = validate_text(cand, facts)
            if not rejected and cand.strip():
                text, generator = cand.strip(), llm_name or "LLM"
        except Exception as e:  # endpoint down -> fallback, never block the job
            rejected = [f"llm error: {type(e).__name__}"]
    if text is None:
        text = deterministic_text(facts)
    tags = sorted({f"{k}:{v}" for k, v in TAG.findall(text)})
    citations = {"finding_ids": [t[2:] for t in tags if t[0] == "F"], "pfm_ids": [t[2:] for t in tags if t[0] == "P"],
                 "reading_ids": [t[2:] for t in tags if t[0] == "R"], "model_ids": [t[2:] for t in tags if t[0] == "M"],
                 "dssmp_version_id": facts.get("dssmp_version_id"), "llm_rejections": rejected}
    cid = hashlib.sha256(f"{facts['dam_id']}|{scope}|{scope_id}|{period_start}|{period_end}|{prompt_hash}".encode()).hexdigest()[:32]
    return {"commentary_id": cid, "dam_id": facts["dam_id"], "period_start": period_start, "period_end": period_end,
            "scope": scope, "scope_id": scope_id, "text": text, "citations": json.dumps(citations, default=str),
            "confidence": confidence(facts), "generator": generator, "prompt_hash": prompt_hash,
            "requires_engineer_review": True, "created_ts": created_ts}
