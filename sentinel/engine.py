"""Inventory engine: rebuild book state from events, compute net availability,
allocate by the documented rule, project ship dates, raise alerts.

THE ALLOCATION RULE (documented, and used identically by the plant model):
  Open orders are served in order of Customer Request Date (earliest first;
  ties broken by order creation time, then order id). Each order takes each
  material from the earliest supply that is still free: stock on hand first,
  then open purchase orders by their expected (risk-adjusted) arrival.
  An order starts production only when every material is fully covered, and
  ships PROD_DAYS later.

NET AVAILABILITY = on-hand + incoming (open PO remainder) - allocated
  (requirements of open orders not yet in production).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from . import ambiguity
from .catalog import PROD_DAYS, Catalog
from .events import FEED_SOURCES, EventStore

AT_RISK_SLACK = 2       # slack (days) below which an order is "at risk"
FEED_TIMEOUT = 0.75     # days without any event from a source -> feed down
OVERDUE_GRACE = 0.5     # a PO is overdue this long after its expected day
DISCREPANCY_WINDOW = 3.0
SPIKE_MIN_ORDERS = 4
SPIKE_FACTOR = 3.0


@dataclass
class EngineOptions:
    learn_supplier_delay: bool = True   # add each supplier's learned average delay to ETAs
    handle_overdue: bool = True         # push ETA of overdue POs past "now"
    time_phased: bool = True            # project per order by date (False = static net check)
    # How plain-language supplier messages move a PO's expected date:
    #   "worst"  - clear: that date; ambiguous: the latest reading, and ask (default)
    #   "guess"  - take the earliest reading silently (a plain date parser)
    #   "mid"    - take the middle of the range silently
    #   "ignore" - do not read free text at all (structured events only)
    text_policy: str = "worst"


@dataclass
class BookState:
    now: float
    on_hand: dict[str, float]
    orders: dict[str, dict]
    pos: dict[str, dict]
    discrepancies: list[dict] = field(default_factory=list)
    anomalies: list[dict] = field(default_factory=list)


def rebuild(store: EventStore, now: float, text_policy: str = "worst") -> BookState:
    cat = store.catalog
    on_hand = {m: 0.0 for m in cat.materials}
    orders: dict[str, dict] = {}
    pos: dict[str, dict] = {}
    disc: list[dict] = []
    anom: list[dict] = []

    def consume(o: dict) -> None:
        for m, per in cat.products[o["product"]].bom.items():
            on_hand[m] -= per * o["qty"]

    for ev in store.sorted_events():
        ts = ev["ts"]
        if ts > now:
            break
        t = ev["type"]
        if t == "order_created":
            oid = ev["order_id"]
            if oid in orders:
                anom.append({"ts": ts, "what": f"order {oid} created twice (event {ev['event_id']})"})
                continue
            orders[oid] = {"id": oid, "product": ev["product"], "qty": ev["qty"],
                           "crd": ev["crd"], "created": ts, "status": "open",
                           "started": None, "shipped": None}
        elif t == "order_cancelled":
            o = orders.get(ev["order_id"])
            if o and o["status"] == "open":
                o["status"] = "cancelled"
        elif t == "production_started":
            o = orders.get(ev["order_id"])
            if not o:
                anom.append({"ts": ts, "what": f"production started for unknown order {ev['order_id']}"})
            elif o["status"] == "open":
                o["status"] = "in_production"
                o["started"] = ts
                consume(o)
        elif t == "order_shipped":
            o = orders.get(ev["order_id"])
            if not o:
                anom.append({"ts": ts, "what": f"shipment of unknown order {ev['order_id']}"})
            else:
                if o["status"] == "open":       # start event missing: consume now
                    o["started"] = ts
                    consume(o)
                o["status"] = "shipped"
                o["shipped"] = ts
        elif t == "po_created":
            pid = ev["po_id"]
            if pid in pos:
                anom.append({"ts": ts, "what": f"PO {pid} created twice"})
                continue
            m = cat.materials[ev["material"]]
            pos[pid] = {"id": pid, "material": ev["material"], "supplier": m.supplier,
                        "qty": ev["qty"], "expected": ev["expected"],
                        "orig_expected": ev["expected"], "created": ts, "received": 0,
                        "status": "open", "closed": None, "date_changes": [], "messages": [], "unclear": None,
                        "expedite": bool(ev.get("expedite", False)), "source": ev["source"]}
        elif t == "po_date_changed":
            p = pos.get(ev["po_id"])
            if p and p["status"] == "open":
                p["date_changes"].append({"ts": ts, "old": p["expected"], "new": ev["expected"]})
                p["expected"] = ev["expected"]
                p["unclear"] = None
        elif t == "supplier_message":
            p = pos.get(ev["po_id"])
            if not p:
                anom.append({"ts": ts, "what": f"supplier message about unknown PO {ev['po_id']}"})
                continue
            read = ambiguity.interpret(ev["text"], ts, p["orig_expected"], p["id"])
            p["messages"].append({"ts": ts, "event_id": ev["event_id"], "text": ev["text"], "read": read})
            if p["status"] != "open" or text_policy == "ignore":
                continue
            if text_policy == "guess":
                new = ambiguity.naive_guess(ev["text"], ts, p["orig_expected"])
            elif text_policy == "mid":       # baseline: middle of the range, never asks
                if read["status"] == "ambiguous":
                    new = (read["lo"] + read["hi"] + 1) // 2
                elif read["status"] == "clear":
                    new = None if read["no_change"] else read["day"]
                else:
                    new = None
            elif read["status"] == "ambiguous":
                new = read["plan_day"]
                p["unclear"] = {"event_id": ev["event_id"], "ts": ts, "text": ev["text"], **read}
            elif read["status"] == "clear":
                new = None if read["no_change"] else read["day"]
                p["unclear"] = None
            else:
                new = None
            if new is not None and new != p["expected"]:
                p["date_changes"].append({"ts": ts, "old": p["expected"], "new": new, "from_text": ev["event_id"]})
                p["expected"] = new
        elif t == "goods_received":
            on_hand[ev["material"]] += ev["qty"]
            p = pos.get(ev["po_id"])
            if not p:
                anom.append({"ts": ts, "what": f"receipt for unknown PO {ev['po_id']}"})
            elif p["material"] != ev["material"]:
                anom.append({"ts": ts, "what": f"receipt of {ev['material']} booked against {p['id']}, "
                                               f"which is for {p['material']}; PO left open"})
            else:
                p["received"] += ev["qty"]
                if p["received"] >= p["qty"] and p["status"] == "open":
                    p["status"] = "closed"
                    p["closed"] = ts
        elif t == "stock_count":
            m = ev["material"]
            book = on_hand[m]
            if ts > 0 and abs(ev["counted"] - book) > 1e-9:
                disc.append({"ts": ts, "material": m, "book": book,
                             "counted": ev["counted"], "delta": ev["counted"] - book})
            on_hand[m] = float(ev["counted"])
    return BookState(now, on_hand, orders, pos, disc, anom)


# ---------------------------------------------------------------- suppliers
def supplier_stats(state: BookState, cat: Catalog) -> dict[str, dict]:
    """Learned delivery performance per supplier (from closed POs).
    est_delay is a shrunk mean of positive lateness (prior 1 day, weight 2)."""
    out = {}
    for sid, s in cat.suppliers.items():
        lates = [p["closed"] - p["orig_expected"] for p in state.pos.values()
                 if p["supplier"] == sid and p["status"] == "closed"]
        pos_late = [max(0.0, round(x)) for x in lates]
        n = len(lates)
        est = (sum(pos_late) + 1.0 * 2) / (n + 2)
        out[sid] = {"id": sid, "name": s.name, "delivered": n,
                    "late": sum(1 for x in pos_late if x >= 1),
                    "on_time_rate": round(1 - sum(1 for x in pos_late if x >= 1) / n, 3) if n else None,
                    "avg_delay_days": round(sum(pos_late) / n, 2) if n else None,
                    "est_delay": round(est, 2)}
    return out


def po_eta(p: dict, now: float, sup: dict, opt: EngineOptions) -> float:
    """Risk-adjusted arrival day of an open PO."""
    eta = p["expected"]
    if opt.learn_supplier_delay and not p["date_changes"]:
        eta += sup[p["supplier"]]["est_delay"] * (0.5 if p["expedite"] else 1.0)
    if opt.handle_overdue and now > p["expected"] + OVERDUE_GRACE:
        extra = max(1.0, sup[p["supplier"]]["est_delay"]) if opt.learn_supplier_delay else 1.0
        eta = max(eta, now + extra)
    return max(eta, math.floor(now))


# ---------------------------------------------------------------- projection
def _order_key(o: dict):
    return (o["crd"], o["created"], o["id"])


def project(state: BookState, cat: Catalog, opt: EngineOptions | None = None,
            extra_pos: list[dict] | None = None) -> dict:
    """Time-phased allocation. Returns per-order projections and per-material
    summaries. `extra_pos` lets the agent ask 'what if we placed this PO?'."""
    opt = opt or EngineOptions()
    now = state.now
    today = math.floor(now)
    sup = supplier_stats(state, cat)

    open_pos = [p for p in state.pos.values() if p["status"] == "open"]
    if extra_pos:
        open_pos = open_pos + extra_pos
    lots: dict[str, list[list]] = {m: [] for m in cat.materials}
    for m in cat.materials:
        oh = max(0.0, state.on_hand[m])
        if oh > 0:
            lots[m].append([now, oh, "stock"])
    for p in open_pos:
        rem = p["qty"] - p.get("received", 0)
        if rem > 0:
            eta = p["eta"] if "eta" in p else po_eta(p, now, sup, opt)
            lots[p["material"]].append([eta, float(rem), p["id"]])
    for m in lots:
        lots[m].sort(key=lambda x: (x[0], str(x[2])))

    open_orders = sorted((o for o in state.orders.values() if o["status"] == "open"), key=_order_key)
    allocated = {m: 0.0 for m in cat.materials}
    uncovered_total = {m: 0.0 for m in cat.materials}
    stockout_day: dict[str, float] = {}
    affected: dict[str, list[str]] = {m: [] for m in cat.materials}
    ptr = {m: 0 for m in cat.materials}
    left = {m: [lot[1] for lot in lots[m]] for m in cat.materials}

    proj_orders = []
    for o in open_orders:
        bom = cat.products[o["product"]].bom
        mat_ready = {}
        draws = {}
        uncovered = {}
        for m, per in bom.items():
            need = per * o["qty"]
            allocated[m] += need
            ready = now
            took = []
            while need > 1e-9 and ptr[m] < len(lots[m]):
                i = ptr[m]
                take = min(need, left[m][i])
                if take > 0:
                    left[m][i] -= take
                    need -= take
                    ready = max(ready, lots[m][i][0])
                    took.append({"lot": lots[m][i][2], "qty": take, "eta": round(lots[m][i][0], 2)})
                if left[m][i] <= 1e-9:
                    ptr[m] += 1
            if need > 1e-9:
                uncovered[m] = need
                uncovered_total[m] += need
                affected[m].append(o["id"])
                stockout_day.setdefault(m, max(today, o["crd"] - PROD_DAYS))
                ready = max(ready, today + cat.materials[m].lead_days)
            mat_ready[m] = ready
            draws[m] = took
        start = max(math.ceil(max(mat_ready.values())), today + 1) if mat_ready else today + 1
        if not opt.time_phased:
            start = today + 1
        ship = start + PROD_DAYS
        slack = o["crd"] - ship
        binding = max(mat_ready.values()) if mat_ready else now
        reasons = []
        for m, r in mat_ready.items():
            if r < binding - 1e-9:
                continue
            if m in uncovered:
                reasons.append({"material": m, "kind": "no_supply", "short": round(uncovered[m], 1),
                                "eta": round(r, 2)})
            else:
                po_ids = [d["lot"] for d in draws[m] if d["lot"] != "stock"]
                if po_ids:
                    reasons.append({"material": m, "kind": "waiting_po", "po": po_ids[-1], "eta": round(r, 2)})
                else:
                    reasons.append({"material": m, "kind": "stock", "eta": round(r, 2)})
        proj_orders.append({**o, "projected_ship": ship, "slack": slack,
                            "risk": _risk(slack), "uncovered": {k: round(v, 1) for k, v in uncovered.items()},
                            "reasons": reasons, "draws": draws})

    for o in state.orders.values():
        if o["status"] == "in_production":
            ship = math.floor(o["started"]) + PROD_DAYS
            proj_orders.append({**o, "projected_ship": ship, "slack": o["crd"] - ship,
                                "risk": _risk(o["crd"] - ship), "uncovered": {}, "reasons": [], "draws": {}})

    incoming = {m: 0.0 for m in cat.materials}
    for p in open_pos:
        incoming[p["material"]] += max(0, p["qty"] - p.get("received", 0))
    materials = []
    for m, mat in cat.materials.items():
        oh = state.on_hand[m]
        materials.append({
            "id": m, "name": mat.name, "unit": mat.unit, "supplier": mat.supplier,
            "on_hand": round(oh, 1), "incoming": round(incoming[m], 1),
            "allocated": round(allocated[m], 1),
            "net": round(oh + incoming[m] - allocated[m], 1),
            "uncovered": round(uncovered_total[m], 1),
            "stockout_day": stockout_day.get(m),
            "affected_orders": affected[m],
            "lead_days": mat.lead_days,
            "days_of_cover": round(max(0.0, oh) / mat.daily_forecast, 1) if mat.daily_forecast else None,
        })
    return {"orders": proj_orders, "materials": materials, "suppliers": sup}


def _risk(slack: float) -> str:
    if slack < 0:
        return "late"
    if slack < AT_RISK_SLACK:
        return "at_risk"
    return "ok"


def static_flags(state: BookState, cat: Catalog) -> dict[str, bool]:
    """Baseline detector (what a typical ERP screen shows): an order is flagged
    if any of its materials has negative net availability right now."""
    proj = project(state, cat, EngineOptions(learn_supplier_delay=False, handle_overdue=False))
    neg = {m["id"] for m in proj["materials"] if m["net"] < 0}
    return {o["id"]: any(m in neg for m in cat.products[o["product"]].bom)
            for o in state.orders.values() if o["status"] == "open"}


# ---------------------------------------------------------------- alerts
def feeds_down(store: EventStore, now: float) -> list[tuple[str, float]]:
    """Sources with no event at all (heartbeats included) for longer than FEED_TIMEOUT."""
    out = []
    for src in FEED_SOURCES:
        last = store.last_seen.get(src)
        if last is not None and now - last > FEED_TIMEOUT:
            out.append((src, last))
    return out


def alerts(state: BookState, proj: dict, store: EventStore, cat: Catalog) -> list[dict]:
    now = state.now
    out: list[dict] = []

    def add(key, kind, severity, msg, **data):
        out.append({"key": key, "kind": kind, "severity": severity, "message": msg, "data": data})

    # Feed health: no events at all (heartbeats included) from a source.
    for src, last in feeds_down(store, now):
        add(f"feed_down:{src}", "feed_down", "high",
            f"{src.upper()} feed silent for {now - last:.1f} days (last event day {last:.2f}). "
            f"Numbers that depend on it may be stale.", source=src, silent_days=round(now - last, 2))

    for o in sorted(proj["orders"], key=lambda o: o["slack"]):
        if o["status"] not in ("open", "in_production"):
            continue
        if o["risk"] == "late":
            add(f"order_late:{o['id']}", "order_late", "high",
                f"Order {o['id']} projected to ship day {o['projected_ship']}, "
                f"{-o['slack']} day(s) after its CRD (day {o['crd']}).",
                order=o["id"], projected_ship=o["projected_ship"], crd=o["crd"], slack=o["slack"])
        elif o["risk"] == "at_risk":
            add(f"order_at_risk:{o['id']}", "order_at_risk", "medium",
                f"Order {o['id']} has only {o['slack']} day(s) of slack before CRD day {o['crd']}.",
                order=o["id"], projected_ship=o["projected_ship"], crd=o["crd"], slack=o["slack"])

    for m in proj["materials"]:
        if m["uncovered"] > 0:
            add(f"stockout:{m['id']}", "stockout", "high",
                f"{m['name']} ({m['id']}) runs out by day {m['stockout_day']}: short "
                f"{m['uncovered']:g} {m['unit']} with nothing on order to cover it "
                f"({len(m['affected_orders'])} order(s) affected).",
                material=m["id"], short=m["uncovered"], day=m["stockout_day"],
                orders=m["affected_orders"])
        if m["on_hand"] < 0:
            add(f"negative_stock:{m['id']}", "data_quality", "medium",
                f"Book stock of {m['id']} is negative ({m['on_hand']:g}); a receipt or count is missing.",
                material=m["id"])

    for p in state.pos.values():
        if p["status"] != "open":
            continue
        if now > p["expected"] + OVERDUE_GRACE:
            add(f"po_overdue:{p['id']}", "po_overdue", "high",
                f"PO {p['id']} ({p['material']}, {p['qty']} units, {cat.suppliers[p['supplier']].name}) "
                f"was due day {p['expected']:g} and has not arrived.",
                po=p["id"], material=p["material"], expected=p["expected"])
        elif p.get("unclear"):
            u = p["unclear"]
            add(f"supplier_unclear:{p['id']}:{u['event_id']}", "supplier_unclear", "medium",
                f"Supplier message on PO {p['id']} ({p['material']}) is unclear: \"{u['text']}\". "
                f"It could mean day {u['lo']} to day {u['hi']}. Planning for day {u['hi']} until they confirm.",
                po=p["id"], material=p["material"], lo=u["lo"], hi=u["hi"], text=u["text"])
        elif p["expected"] > p["orig_expected"]:
            add(f"po_delayed:{p['id']}", "po_delayed", "medium",
                f"Supplier moved PO {p['id']} ({p['material']}) from day {p['orig_expected']:g} "
                f"to day {p['expected']:g}.",
                po=p["id"], material=p["material"], expected=p["expected"], was=p["orig_expected"])

    for d in state.discrepancies:
        if now - d["ts"] <= DISCREPANCY_WINDOW:
            add(f"discrepancy:{d['material']}:{d['ts']:.2f}", "stock_discrepancy", "medium",
                f"Stock count of {d['material']} found {d['counted']:g} against {d['book']:g} on the books "
                f"({d['delta']:+g}). Book corrected.", **d)

    # Demand spike per product: last 24 h vs the previous 14 days.
    for pid, p in cat.products.items():
        recent = [o for o in state.orders.values() if o["product"] == pid and now - 1 < o["created"] <= now]
        hist = [o for o in state.orders.values() if o["product"] == pid and now - 15 < o["created"] <= now - 1]
        span = min(14.0, max(1.0, now - 1))
        base_qty = sum(o["qty"] for o in hist) / span
        rq = sum(o["qty"] for o in recent)
        if now >= 4 and len(recent) >= SPIKE_MIN_ORDERS and rq >= SPIKE_FACTOR * max(base_qty, 1):
            add(f"demand_spike:{pid}:{math.floor(now)}", "demand_spike", "medium",
                f"Demand spike on {p.name} ({pid}): {len(recent)} orders / {rq} units in 24 h "
                f"vs {base_qty:.1f} units/day normally.", product=pid, orders=len(recent),
                qty=rq, baseline=round(base_qty, 1))

    for an in state.anomalies:
        if now - an["ts"] <= 1:
            add(f"anomaly:{an['ts']:.3f}", "data_quality", "medium", f"Inconsistent data: {an['what']}.", **an)

    recent_q = [q for q in store.quarantine if q.get("arrival") is not None and now - q["arrival"] <= 1]
    if recent_q:
        add(f"quarantine:{math.floor(now)}", "data_quality", "low",
            f"{len(recent_q)} malformed event(s) quarantined in the last 24 h "
            f"(e.g. '{recent_q[-1]['reason']}').", count=len(recent_q))
    sev = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda a: sev[a["severity"]])
    return out


# ---------------------------------------------------------------- snapshot
def snapshot(store: EventStore, now: float, opt: EngineOptions | None = None) -> dict:
    cat = store.catalog
    opt = opt or EngineOptions()
    state = rebuild(store, now, opt.text_policy)
    proj = project(state, cat, opt)
    al = alerts(state, proj, store, cat)
    open_orders = [o for o in proj["orders"] if o["status"] in ("open", "in_production")]
    late = [o for o in open_orders if o["risk"] == "late"]
    at_risk = [o for o in open_orders if o["risk"] == "at_risk"]
    kpis = {
        "open_orders": len(open_orders),
        "late": len(late),
        "at_risk": len(at_risk),
        "on_track_pct": round(100 * (1 - (len(late) + len(at_risk)) / len(open_orders)), 1) if open_orders else 100.0,
        "stockouts": sum(1 for m in proj["materials"] if m["uncovered"] > 0),
        "open_pos": sum(1 for p in state.pos.values() if p["status"] == "open"),
        "overdue_pos": sum(1 for a in al if a["kind"] == "po_overdue"),
        "feeds_down": [a["data"]["source"] for a in al if a["kind"] == "feed_down"],
        "quarantined": store.stats["quarantined"],
        "duplicates_ignored": store.stats["duplicates"],
        "events_accepted": store.stats["accepted"],
        "unclear_messages": sum(1 for p in state.pos.values() if p["status"] == "open" and p.get("unclear")),
    }
    snap = {"now": now, "kpis": kpis, "orders": proj["orders"], "materials": proj["materials"],
            "suppliers": proj["suppliers"], "alerts": al,
            "pos": sorted(state.pos.values(), key=lambda p: p["created"]),
            "anomalies": state.anomalies[-20:], "discrepancies": state.discrepancies[-20:],
            "messages": recent_messages(state, cat)}
    snap["hash"] = state_hash(state, proj)
    snap["_state"] = state
    return snap


def recent_messages(state: BookState, cat: Catalog, n: int = 12) -> list[dict]:
    """Latest supplier messages with how Sentinel read them (for the dashboard)."""
    out = []
    for p in state.pos.values():
        for m in p.get("messages", []):
            out.append({"po": p["id"], "material": p["material"], "material_name": cat.materials[p["material"]].name,
                        "supplier": cat.suppliers[p["supplier"]].name, "ts": m["ts"], "event_id": m["event_id"],
                        "text": m["text"], "read": m["read"], "po_status": p["status"],
                        "open_question": bool(p.get("unclear") and p["unclear"]["event_id"] == m["event_id"]),
                        "expected_now": p["expected"], "orig_expected": p["orig_expected"]})
    out.sort(key=lambda x: -x["ts"])
    return out[:n]


def state_hash(state: BookState, proj: dict) -> str:
    """Fingerprint of everything derived from the events (for replay tests)."""
    core = {"on_hand": {k: round(v, 6) for k, v in sorted(state.on_hand.items())},
            "orders": sorted((o["id"], o["status"], o["projected_ship"]) for o in proj["orders"]),
            "pos": sorted((p["id"], p["status"], p["received"], p["expected"]) for p in state.pos.values()),
            "materials": [(m["id"], m["net"], m["uncovered"]) for m in proj["materials"]]}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()[:16]
