import random

from sentinel import agent, engine
from sentinel.catalog import Catalog, Material, Product, Supplier
from sentinel.events import EventStore
from sentinel.world import World


def tiny_catalog():
    c = Catalog()
    c.suppliers["S1"] = Supplier("S1", "Acme")
    c.materials["M01"] = Material("M01", "Steel", "kg", "S1", 5, 3, 10.0, 100, daily_forecast=10)
    c.materials["M02"] = Material("M02", "Bolt", "pcs", "S1", 5, 3, 1.0, 100, daily_forecast=10)
    c.products["P1"] = Product("P1", "Fan", {"M01": 2, "M02": 1}, 0.5, 10)
    return c


def ev(i, t, ts, src="erp", **kw):
    return {"event_id": f"e{i}", "type": t, "ts": ts, "source": src, **kw}


def test_net_availability_formula():
    c = tiny_catalog()
    s = EventStore(c)
    s.ingest(ev(1, "stock_count", 0, "wms", material="M01", counted=50))
    s.ingest(ev(2, "po_created", 0.5, po_id="PO1", material="M01", qty=30, expected=6))
    s.ingest(ev(3, "order_created", 1.0, order_id="SO1", product="P1", qty=20, crd=10))
    m = {x["id"]: x for x in engine.snapshot(s, 1.5)["materials"]}["M01"]
    assert (m["on_hand"], m["incoming"], m["allocated"]) == (50, 30, 40)
    assert m["net"] == 50 + 30 - 40


def test_duplicates_malformed_and_order_do_not_change_state():
    c = tiny_catalog()
    events = [ev(1, "stock_count", 0, "wms", material="M01", counted=50),
              ev(2, "stock_count", 0, "wms", material="M02", counted=50),
              ev(3, "order_created", 1.0, order_id="SO1", product="P1", qty=10, crd=10),
              ev(4, "production_started", 2.0, order_id="SO1"),
              ev(5, "po_created", 2.5, po_id="PO1", material="M01", qty=30, expected=6),
              ev(6, "goods_received", 6.3, "wms", po_id="PO1", material="M01", qty=30)]
    a = EventStore(c)
    for e in events:
        a.ingest(e)
    b = EventStore(c)
    mixed = events + [dict(e) for e in events[:3]] + [{"event_id": "x", "type": "goods_received", "qty": -1},
                                                      "garbage", {"type": "heartbeat"}]
    random.Random(3).shuffle(mixed)
    results = [b.ingest(e) for e in mixed]
    assert results.count("duplicate") == 3 and results.count("quarantined") == 3
    assert engine.snapshot(a, 7)["hash"] == engine.snapshot(b, 7)["hash"]
    assert {m["id"]: m["on_hand"] for m in engine.snapshot(b, 7)["materials"]} == {"M01": 60, "M02": 40}


def test_conflicting_duplicate_id_is_quarantined():
    c = tiny_catalog()
    s = EventStore(c)
    assert s.ingest(ev(1, "stock_count", 0, "wms", material="M01", counted=50)) == "accepted"
    assert s.ingest(ev(1, "stock_count", 0, "wms", material="M01", counted=999)) == "conflict"
    assert engine.rebuild(s, 1).on_hand["M01"] == 50


def test_allocation_rule_earliest_crd_first():
    c = tiny_catalog()
    s = EventStore(c)
    s.ingest(ev(1, "stock_count", 0, "wms", material="M01", counted=40))
    s.ingest(ev(2, "stock_count", 0, "wms", material="M02", counted=100))
    # SO2 is created first but has the later CRD: SO1 must get the stock.
    s.ingest(ev(3, "order_created", 0.1, order_id="SO2", product="P1", qty=20, crd=15))
    s.ingest(ev(4, "order_created", 0.2, order_id="SO1", product="P1", qty=20, crd=8))
    o = {x["id"]: x for x in engine.snapshot(s, 0.5)["orders"]}
    assert o["SO1"]["projected_ship"] == 1 + 2        # starts tomorrow, ships 2 days later
    assert o["SO2"]["uncovered"] == {"M01": 40}       # nothing left for SO2
    assert o["SO2"]["projected_ship"] == 0 + 5 + 2    # waits a full lead time
    assert o["SO1"]["risk"] == "ok"


def test_feed_down_alert():
    c = tiny_catalog()
    s = EventStore(c)
    s.ingest(ev(1, "heartbeat", 1.0, "wms"))
    s.ingest(ev(2, "heartbeat", 1.0, "erp"))
    s.ingest(ev(3, "heartbeat", 2.0, "erp"))
    kinds = {(a["kind"], a["data"].get("source")) for a in engine.snapshot(s, 2.0)["alerts"]}
    assert ("feed_down", "wms") in kinds and ("feed_down", "erp") not in kinds


def test_overdue_po_pushes_eta():
    c = tiny_catalog()
    s = EventStore(c)
    s.ingest(ev(1, "po_created", 0, po_id="PO1", material="M01", qty=30, expected=5))
    snap = engine.snapshot(s, 7.0)
    assert any(a["kind"] == "po_overdue" for a in snap["alerts"])
    p = snap["_state"].pos["PO1"]
    assert engine.po_eta(p, 7.0, snap["suppliers"], engine.EngineOptions()) >= 8.0


def test_grounding_check_catches_invented_numbers():
    facts = [{"id": "kpi", "late": 3, "at_risk": 1},
             {"id": "mat:M07", "net": -12.5, "on_hand": 4},
             {"id": "order:SO0001", "crd": 10, "projected_ship": 14, "days_late": 4}]
    g = agent.grounding_check
    assert g("3 orders projected late, 1 at risk.\nM07 net is -12.5.\nSO0001 ships day 14, 4 days late.", facts)["ok"]
    bad = g("3 orders projected late, 7 more next week", facts)
    assert not bad["ok"] and bad["ungrounded"] == [7.0]
    assert not g("SO0001 ships day 3.", facts)["ok"]              # 3 is a plant-wide number, not SO0001's
    assert not g("SO0099 ships day 14.", facts)["ok"]             # no such order
    assert not g("Nothing is late today.", facts)["ok"]          # 3 are
    assert not g("1 order projected late, 3 at risk.", facts)["ok"]   # swapped counts
    assert agent.grounding_check_loose("1 order projected late, 3 at risk.", facts)["ok"]   # the old check missed it


def test_proposals_need_approval_and_help():
    w = World(seed=4)
    seen = []
    for d in range(40):
        w.simulate_day(d)
        w.rop_planner(d)
        t = d + 0.95
        w.deliver_until(t)
        snap = engine.snapshot(w.store, t)
        proj = {k: snap[k] for k in ("orders", "materials", "suppliers")}
        before = len(w.pos)
        props = agent.propose(snap["_state"], proj, w.cat)
        assert len(w.pos) == before          # proposing never creates a PO
        for p in props:
            assert p["status"] == "pending"
            e = p["effect"]
            assert e["late_after"] < e["late_before"] or e["late_days_after"] < e["late_days_before"]
        seen += props
    assert seen, "expected at least one proposal in 40 days"


def test_briefings_are_grounded():
    w = World(seed=2)
    for d in range(30):
        w.simulate_day(d)
        w.rop_planner(d)
    w.deliver_until(29.95)
    snap = engine.snapshot(w.store, 29.95)
    for role in ("planner", "buyer", "manager"):
        assert agent.briefing(role, snap, [], w.cat)["grounding"]["ok"]
