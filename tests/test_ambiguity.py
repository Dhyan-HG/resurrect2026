"""Supplier-message reading (Problem 6 inside Sentinel)."""
import json
from pathlib import Path

from sentinel import engine
from sentinel.ambiguity import interpret, naive_guess
from sentinel.world import Faults, World, agent_planner

WED_11_NOV = 9      # day 9 = Wednesday 11 Nov 2026
DUE_FRI = 11        # Friday 13 Nov


def read(text):
    return interpret(text, WED_11_NOV + 0.4, DUE_FRI, "PO0001")


def test_clear_messages_are_not_questioned():
    assert read("Delivery moved to Thursday 19 Nov")["day"] == 17
    assert read("PO0001 is running 3 days late")["day"] == DUE_FRI + 3
    assert read("Arriving tomorrow")["day"] == WED_11_NOV + 1
    r = read("On track, delivery as planned")
    assert r["status"] == "clear" and r["no_change"]
    for t in ("Delivery moved to Thursday 19 Nov", "in 4 days", "Friday"):
        assert read(t)["question"] is None


def test_ambiguous_messages_give_a_range_and_a_question():
    r = read("Sorry sir, material will reach next week")
    assert r["status"] == "ambiguous" and (r["lo"], r["hi"]) == (14, 20)        # Mon 16 - Sun 22 Nov
    assert r["plan_day"] == 20 and "Mon 16 Nov" in r["question"]
    r = read("Delivery next Friday")
    assert r["readings"] == [11, 18]
    assert read("Running 2-3 days late")["type"] == "range"
    assert read("Will dispatch on Monday")["type"] == "dispatch_vs_arrival"
    assert read("Delivery on Thursday 27 Nov")["type"] == "conflict"       # 27 Nov is a Friday
    assert read("Delayed, will update the new date")["type"] == "missing_date"


def test_no_date_messages_change_nothing():
    for t in ("Invoice attached", "Please release payment due on 15th", "Meeting on Wednesday at 11 is fine"):
        assert read(t)["status"] == "none"


def test_plain_parser_baseline_takes_the_earliest_reading():
    assert naive_guess("Delivery next week", WED_11_NOV + 0.4, DUE_FRI) == 14
    assert naive_guess("Delayed, will update", WED_11_NOV + 0.4, DUE_FRI) is None


def test_engine_plans_for_latest_and_a_reply_resolves_it():
    w = World(seed=3, days=30, faults=Faults(supplier_text=1.0, text_vague=1.0))
    for d in range(30):
        w.simulate_day(d)
        w.rop_planner(d)
        snap = engine.snapshot(w.store, d + 0.95)
        unclear = [p for p in snap["_state"].pos.values() if p["status"] == "open" and p["unclear"]]
        if unclear:
            p = unclear[0]
            assert p["expected"] == p["unclear"]["hi"]
            assert any(a["kind"] == "supplier_unclear" for a in snap["alerts"])
            w.answer_question(p["id"], d + 0.95)
            w.deliver_until(d + 1.5)
            after = engine.rebuild(w.store, d + 1.5)
            assert after.pos[p["id"]]["unclear"] is None
            assert after.pos[p["id"]]["expected"] == w.pos[p["id"]]["actual"]
            return
    raise AssertionError("no unclear supplier message in 30 days")


def test_replay_stays_identical_with_text_messages():
    w = World(seed=5, days=25, faults=Faults(supplier_text=1.0, confirmations=0.3))
    for d in range(25):
        w.simulate_day(d)
        w.rop_planner(d)
        agent_planner(w, d, ask=True)
    w.deliver_until(26)
    h1 = engine.snapshot(w.store, 25.5)["hash"]
    from sentinel.events import EventStore
    s2 = EventStore(w.cat)
    for ev in reversed(list(w.store.events.values())):
        s2.ingest(ev)
        s2.ingest(dict(ev))
    assert engine.snapshot(s2, 25.5)["hash"] == h1


def test_development_set_labels_are_consistent():
    items = json.loads((Path(__file__).resolve().parents[1] / "evaluation" / "ambiguity_dev.json")
                       .read_text(encoding="utf-8"))["items"]
    for it in items:
        if it["label"] == "ambiguous":
            assert it["range"][0] <= it["meant"] <= it["range"][1], it["text"]


def test_review_cases():
    """Cases raised by the independent reviewers (27 Nov)."""
    r = lambda t: interpret(t, 16.5, 18, "PO0001")          # sent Wed 18 Nov, due Fri 20 Nov
    assert r("Material will reach by 11.30 am tomorrow")["day"] == 17       # a time, not 30 Nov
    assert r("Balance 2.5 MT will come tomorrow")["day"] == 17              # a quantity, not 5 Feb
    assert r("The 12 Nov consignment is now rescheduled to 30 Nov")["day"] == 28   # past date ignored
    assert r("We cannot deliver on 20th, will deliver on 23rd")["day"] == 21
    assert r("Originally due 20 Nov, now revised to 27 Nov")["day"] == 25
    assert r("Delivery on 31 Nov")["type"] == "conflict"                    # no such date
    assert r("in a fortnight")["day"] == 30
    assert r("2 days earlier than planned")["day"] == 16
    assert r("Monday after next")["day"] == 28
    assert r("Material will not reach before 5 Dec")["type"] == "open_ended"
    assert r("Delivery on 2026-11-24")["day"] == 22 and r("ETA: 24-11-2026")["day"] == 22
    assert r("Will dispatch on Friday and reach early next week")["lo"] == 21
    assert r("Payment of last invoice pending, please clear by 25th")["status"] == "none"
    assert r("delayed by 9999999 days")["status"] in ("ambiguous", "none")   # never crashes
