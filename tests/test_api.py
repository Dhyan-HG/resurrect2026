from fastapi.testclient import TestClient

from sentinel import server

client = TestClient(server.app)
server.WRITE_LIMIT = 10 ** 6     # the rate limit has its own test below


def sess():
    return server._sessions[client.cookies.get("sid")][0]


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
    s = client.get("/api/state?role=manager").json()             # the demo jumps to the moment the alert fires
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
    pos_before = len(sess().world.pos)
    r = client.post(f"/api/proposals/{pid}/approve", headers={"X-Role": "planner"})
    assert r.status_code == 200 and r.json()["po"]
    assert len(sess().world.pos) == pos_before + 1
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


def test_hostile_input_never_breaks_the_demo():
    reset()
    po = client.get("/api/state").json()["pos"][0]["id"]
    for text in ("delayed by 9999999 days", "in 99999999999 days", "x" * 500, "\U0001F69A tomorrow", "31/02"):
        assert client.post("/api/messages", json={"po": po, "text": text}).status_code == 200
    assert client.post("/api/advance", json={"days": 1}).status_code == 200
    for body in ("[1,2]", '"text"', "{bad json", "null"):
        r = client.post("/api/advance", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 400, body
    assert client.post("/api/advance", json={"days": "abc"}).status_code == 400
    assert client.post("/api/reset", content='{"seed": 1e400}', headers={"Content-Type": "application/json"}).status_code == 400
    huge = [{"event_id": "h1", "type": "order_created", "ts": 5, "source": "erp", "order_id": "X",
             "product": "P1", "qty": 10 ** 400, "crd": 9}]
    assert client.post("/api/events", json=huge).json()["quarantined"] == 1
    future = [{"event_id": "f1", "type": "heartbeat", "ts": 99999, "source": "wms"}]
    assert client.post("/api/events", json=future).json()["quarantined"] == 1
    assert client.get("/api/state").status_code == 200


def test_each_visitor_has_their_own_plant():
    reset()
    other = TestClient(server.app)
    other.post("/api/advance", json={"days": 5})
    assert other.get("/api/state").json()["day"] == 9
    assert client.get("/api/state").json()["day"] == 4


def test_writes_are_rate_limited():
    c = TestClient(server.app)
    old, server.WRITE_LIMIT = server.WRITE_LIMIT, 5
    try:
        codes = [c.post("/api/advance", json={"days": 0.01}).status_code for _ in range(8)]
    finally:
        server.WRITE_LIMIT = old
    assert codes[:5] == [200] * 5 and 429 in codes[5:]
