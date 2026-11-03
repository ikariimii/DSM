from conftest import ADMIN, DSO, ENG, NOBODY, VIEWER


def test_list_users_scoping(c):
    r = c.get("/api/admin/users").json()
    assert r["is_admin"] and len(r["dams"]) == 5 and "dam_admin" in r["roles"]
    r = c.get("/api/admin/users", user=DSO).json()
    assert r["dams"] == ["PHR"] and r["is_admin"] is False
    assert all(set(u["roles"]) <= {"PHR"} for u in r["users"])
    assert c.get("/api/admin/users", user=ENG).status_code == 403


def test_create_and_deactivate(c):
    assert c.post("/api/admin/users", user=DSO, json={"email": "a@b.org"}).status_code == 403
    assert c.post("/api/admin/users", json={"email": "bad"}).status_code == 422
    u = c.post("/api/admin/users", json={"email": "New.User@b.org", "display_name": "N"}).json()
    assert u["email"] == "new.user@b.org" and u["active"]
    assert c.put("/api/admin/roles", json={"email": "new.user@b.org", "dam_id": "CRD", "role": "viewer"}).status_code == 200
    assert c.get("/api/me", user="new.user@b.org").json()["dams"] == ["CRD"]
    assert c.delete("/api/admin/users/new.user@b.org", user=DSO).status_code == 403
    assert c.delete(f"/api/admin/users/{ADMIN}").status_code == 409
    assert c.delete("/api/admin/users/ghost@b.org").status_code == 404
    assert c.delete("/api/admin/users/new.user@b.org").status_code == 200
    me = c.get("/api/me", user="new.user@b.org").json()
    assert me["known"] is False and me["dams"] == []
    assert c.get("/api/dams/CRD", user="new.user@b.org").status_code == 403


def test_dam_admin_manages_own_dam_only(c):
    assert c.put("/api/admin/roles", user=DSO, json={"email": VIEWER, "dam_id": "PHR", "role": "viewer"}).status_code == 200
    assert c.get("/api/me", user=VIEWER).json()["roles"] == {"BFK": "viewer", "PHR": "viewer"}
    assert c.put("/api/admin/roles", user=DSO, json={"email": VIEWER, "dam_id": "CRD", "role": "viewer"}).status_code == 403
    assert c.put("/api/admin/roles", user=DSO, json={"email": DSO, "dam_id": "PHR", "role": "viewer"}).status_code == 409
    assert c.put("/api/admin/roles", user=DSO, json={"email": "ghost@x.org", "dam_id": "PHR", "role": "viewer"}).status_code == 404
    assert c.put("/api/admin/roles", user=DSO, json={"email": VIEWER, "dam_id": "PHR", "role": None}).status_code == 200
    assert c.get("/api/me", user=VIEWER).json()["roles"] == {"BFK": "viewer"}
    assert c.get("/api/admin/audit", user=DSO).status_code == 403
    assert c.get("/api/admin/audit?dam_id=CRD", user=DSO).status_code == 403
    es = c.get("/api/admin/audit?dam_id=PHR", user=DSO).json()["entries"]
    assert [e["action"] for e in es][:2] == ["role_set", "role_set"]
    assert c.get("/api/admin/audit", user=NOBODY).status_code == 403


def test_role_validation(c):
    assert c.put("/api/admin/roles", json={"email": VIEWER, "dam_id": "ZZZ", "role": "viewer"}).status_code == 404
    assert c.put("/api/admin/roles", json={"email": VIEWER, "dam_id": "CRD", "role": "boss"}).status_code == 422
    r = c.put("/api/admin/roles", json={"email": "fresh@x.org", "dam_id": "CRD", "role": "engineer"})
    assert r.status_code == 200 and r.json()["roles"] == {"CRD": "engineer"}  # admin may add a new user via role
    assert c.get("/api/admin/audit?limit=0").json()["entries"]
