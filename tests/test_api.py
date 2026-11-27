from fastapi.testclient import TestClient

from sentinel import server

client = TestClient(server.app)


def reset():
    assert client.post("/api/reset", json={"seed": 7}).status_code == 200


def test_state_and_advance():
    reset()
    s = client.get("/api/state?role=planner").json()
    assert s["day"] == 4 and "kpis" in s and s["briefing"]["grounding"]["ok"]
    client.post("/api/advance", json={"days": 2})
    assert client.get("/api/state").json()["day"] == 6
    assert client.post("/api/advance", json={"days": 50}).status_code == 400


def test_every_injection_and_roles():
    reset()
    for kind in ("spike", "supplier_delay", "outage", "shrink", "junk"):
        r = client.post(f"/api/inject/{kind}")
        assert r.status_code == 200, (kind, r.text)
    s = client.get("/api/state").json()
    assert s["quality"]["quarantined"] >= 5                      # the 5 bad events
    assert any(a["kind"] == "po_delayed" for a in s["alerts"])   # the supplier's slip notice
    client.post("/api/advance", json={"days": 1.0})              # outage lasts 30 h
    s = client.get("/api/state?role=manager").json()
    assert "wms" in s["kpis"]["feeds_down"]
    client.post("/api/advance", json={"days": 1.0})              # feed recovers, buffered events flush
    assert client.get("/api/state").json()["kpis"]["feeds_down"] == []
    for role in ("planner", "buyer", "manager"):
        assert client.get(f"/api/state?role={role}").json()["briefing"]["grounding"]["ok"]


def test_approval_requires_planner_or_buyer():
    reset()
    props = []
    for _ in range(40):
        client.post("/api/advance", json={"days": 1})
        props = client.get("/api/state").json()["proposals"]
        if props:
            break
    assert props, "no proposal appeared in 40 days"
    pid = props[0]["id"]
    r = client.post(f"/api/proposals/{pid}/approve", headers={"X-Role": "manager"})
    assert r.status_code == 403
    pos_before = len(server.S.world.pos)
    r = client.post(f"/api/proposals/{pid}/approve", headers={"X-Role": "planner"})
    assert r.status_code == 200 and r.json()["po"]
    assert len(server.S.world.pos) == pos_before + 1
    assert client.post(f"/api/proposals/{pid}/approve", headers={"X-Role": "planner"}).status_code == 409


def test_event_ingest_is_idempotent():
    reset()
    batch = [{"event_id": "ext-1", "type": "stock_count", "ts": 4.6, "source": "wms",
              "material": "M01", "counted": 123},
             {"event_id": "ext-bad", "type": "stock_count", "ts": 4.6, "source": "wms", "material": "M01"}]
    client.post("/api/advance", json={"days": 0.2})
    first = client.post("/api/events", json=batch).json()
    again = client.post("/api/events", json=batch).json()
    assert first["accepted"] == 1 and first["quarantined"] == 1
    assert again["accepted"] == 0 and again["duplicate"] == 1


def test_explain_and_index():
    reset()
    o = client.get("/api/state").json()["orders"][0]["id"]
    r = client.get(f"/api/orders/{o}/explain").json()
    assert o in r["text"] and r["grounding"]["ok"]
    assert "Sentinel" in client.get("/").text
