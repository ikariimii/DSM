from conftest import DSO, ENG, VIEWER


def test_dssmp(rc):
    r = rc.get("/api/dams/CRD/dssmp", user=ENG).json()
    assert r["pfms"] and r["can_review"] is False
    for p in r["pfms"]:
        for x in p["linked"]:
            if x["latest"] and x["latest"]["display_status"] != "VERIFIED":
                assert x["latest"]["corrected_value"] is None
    assert isinstance(r["untied_instruments"], list)
    assert rc.get("/api/dams/CRD/dssmp", user=VIEWER).status_code == 403


def test_pfm_review(c):
    pfm = c.get("/api/dams/PHR/dssmp", user=DSO).json()["pfms"][0]["pfm_id"]
    assert c.post(f"/api/dams/PHR/pfms/{pfm}/review", user=DSO, json={"status": "bogus"}).status_code == 422
    assert c.post("/api/dams/PHR/pfms/NOPE/review", user=DSO, json={"status": "no_change"}).status_code == 404
    assert c.post(f"/api/dams/CRD/pfms/{pfm}/review", user=ENG, json={"status": "no_change"}).status_code == 403
    r = c.post(f"/api/dams/PHR/pfms/{pfm}/review", user=DSO, json={"status": "no_change", "notes": "annual"})
    assert r.status_code == 200 and r.json()["reviews"][0]["reviewed_by"] == DSO
    d = c.get("/api/dams/PHR/dssmp", user=DSO).json()
    assert next(p for p in d["pfms"] if p["pfm_id"] == pfm)["reviews"]


def test_dssmp_traceability_gap(c):
    an = c.tc.app.state.analytics_factory(None)
    an.d["pfms"].append({"pfm_id": "CRD-PFM-X", "dam_id": "CRD", "title": "t", "category": "II", "description": "",
                         "expected_behavior": "", "instruments": ["CRD-GHOST"], "monitoring_frequency": "monthly"})
    p = next(p for p in c.get("/api/dams/CRD/dssmp").json()["pfms"] if p["pfm_id"] == "CRD-PFM-X")
    assert p["traceability_gaps"] == ["CRD-GHOST"] and p["linked"][0]["missing"]


def test_report_generation(c):
    body = {"period_start": "2026-01-01", "period_end": "2026-11-02"}
    assert c.post("/api/dams/CRD/reports", user=VIEWER, json=body).status_code == 403
    r = c.post("/api/dams/HLD/reports", user=ENG, json=body)
    assert r.status_code == 200
    rid = r.json()["report_id"]
    md = c.get(f"/api/dams/HLD/reports/{rid}", user=ENG).json()["content_md"]
    assert md.startswith("# DRAFT") and "Sign-off" in md
    assert "QC_BAD: 1" in md and "BAD: 1" in md  # HLD-PZ-1 spike counted, not plotted
    assert "| HLD-PZ-1 | piezometer | m | v1 |" in md
    assert "HLD-IN-1" in md and "Bias-shift corrections are not applied" in md
    assert [x["report_id"] for x in c.get("/api/dams/HLD/reports", user=ENG).json()["reports"]] == [rid]
    assert c.get(f"/api/dams/CRD/reports/{rid}", user=ENG).status_code == 404


def test_report_withheld_values_not_printed(c):
    r = c.post("/api/dams/STC/reports", user=DSO, json={"period_start": "2026-01-01", "period_end": "2026-12-31"})
    md = r.json()["content_md"]
    line = next(x for x in md.splitlines() if x.startswith("| STC-PZ-3 "))
    assert "UNAPPROVED_CALIBRATION" in line and line.rstrip().endswith("| — | — | — |")


def test_report_unverified_settlement(c):
    md = c.post("/api/dams/CRD/reports", json={"period_start": "2026-01-01", "period_end": "2026-12-31"}).json()["content_md"]
    line = next(x for x in md.splitlines() if x.startswith("| CRD-SM-1 "))
    assert "UNVERIFIED_FORMULA" in line and line.rstrip().endswith("| — | — | — |")
    assert "| EV-" in md and "L3 Act" in md


def test_report_bad_dates(c):
    assert c.post("/api/dams/CRD/reports", json={"period_start": "2026-13-01", "period_end": "2026-11-02"}).status_code == 422
    assert c.post("/api/dams/CRD/reports", json={"period_start": "nope", "period_end": "2026-11-02"}).status_code == 422
    assert c.post("/api/dams/CRD/reports", json={"period_start": "2026-11-02", "period_end": "2026-01-01"}).status_code == 422
    r = c.post("/api/dams/CRD/reports", json={"period_start": "2026-10-01T00:00:00Z", "period_end": "2026-10-02T00:00:00Z"})
    assert r.status_code == 200 and "no survey in period" in r.json()["content_md"]


def test_report_no_events(c):
    md = c.post("/api/dams/BFK/reports", json={"period_start": "2020-01-01", "period_end": "2020-01-02"}).json()["content_md"]
    assert "No alarm events in the period." in md
