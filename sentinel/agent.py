"""The planning agent.

Two layers, kept apart on purpose:

1. DECIDE (deterministic): read the engine's projection through read-only
   "tools", find shortages and slow purchase orders, and draft purchase-order
   proposals. Every proposal carries a what-if: the projection is re-run with
   the PO added, and the proposal is kept only if it measurably helps.
   Nothing is ordered here; a proposal waits for a human.

2. EXPLAIN (template, optionally Claude): turn the facts into a role-based
   briefing. Every briefing ships with the facts it used, and a grounding
   check rejects any text containing a number that is not in those facts.
   If Claude is enabled (ANTHROPIC_API_KEY set and SENTINEL_LLM=1) its text
   is used only when it passes the check; otherwise the template is used.
"""
from __future__ import annotations

import json
import math
import os
import re

from .catalog import EXPEDITE_PREMIUM, PROD_DAYS, Catalog
from . import engine


def _round_up(x: float, step: int = 5) -> int:
    return int(math.ceil(max(x, 1) / step) * step)


def _lateness(orders: list[dict]) -> tuple[int, int]:
    late = [o for o in orders if o["status"] == "open" and o["slack"] < 0]
    return len(late), sum(-o["slack"] for o in late)


# ---------------------------------------------------------------- decide
def propose(state: engine.BookState, proj: dict, cat: Catalog,
            opt: engine.EngineOptions | None = None, skip_materials: set | None = None,
            allow_expedite: bool = True) -> list[dict]:
    opt = opt or engine.EngineOptions()
    skip = skip_materials or set()
    now = state.now
    today = math.floor(now)
    sup = proj["suppliers"]
    orders = {o["id"]: o for o in proj["orders"] if o["status"] == "open"}
    base_late, base_days = _lateness(list(orders.values()))
    candidates: dict[str, dict] = {}

    # A) shortages with nothing on order
    for m in proj["materials"]:
        if m["uncovered"] <= 0 or m["id"] in skip:
            continue
        mat = cat.materials[m["id"]]
        aff = [orders[i] for i in m["affected_orders"] if i in orders]
        if not aff:
            continue
        need_by = min(o["crd"] for o in aff) - PROD_DAYS
        reg = today + mat.lead_days
        exp = today + mat.expedite_lead_days
        expedite = allow_expedite and reg > need_by and exp < reg
        qty = _round_up(m["uncovered"] * 1.1 + mat.daily_forecast * 3)
        candidates[m["id"]] = {"material": m["id"], "qty": qty, "expedite": expedite,
                               "kind": "shortage", "orders": [o["id"] for o in aff],
                               "short": m["uncovered"], "need_by": need_by}

    # B) late orders whose binding supply is a PO arriving later than an
    #    expedited replacement could (only if expedite is allowed)
    if allow_expedite:
        slow: dict[str, dict] = {}
        for o in orders.values():
            if o["slack"] >= 0:
                continue
            for r in o["reasons"]:
                if r["kind"] != "waiting_po" or r["material"] in skip or r["material"] in candidates:
                    continue
                mat = cat.materials[r["material"]]
                exp = today + mat.expedite_lead_days
                if r["eta"] <= exp:
                    continue
                q = sum(d["qty"] for d in o["draws"].get(r["material"], []) if d["lot"] != "stock")
                s = slow.setdefault(r["material"], {"qty": 0.0, "orders": [], "po": r["po"],
                                                    "po_eta": r["eta"]})
                s["qty"] += q
                s["orders"].append(o["id"])
        for mid, s in slow.items():
            candidates[mid] = {"material": mid, "qty": _round_up(s["qty"]), "expedite": True,
                               "kind": "slow_po", "orders": s["orders"], "short": round(s["qty"], 1),
                               "po": s["po"], "po_eta": s["po_eta"]}

    out = []
    for mid, c in candidates.items():
        mat = cat.materials[mid]
        lead = mat.expedite_lead_days if c["expedite"] else mat.lead_days
        expected = today + lead
        fake = {"id": f"WHATIF-{mid}", "material": mid, "supplier": mat.supplier, "qty": c["qty"],
                "received": 0, "expected": expected, "orig_expected": expected,
                "date_changes": [], "expedite": c["expedite"], "status": "open"}
        fake["eta"] = engine.po_eta(fake, now, sup, opt)
        after = engine.project(state, cat, opt, extra_pos=[fake])
        a_orders = [o for o in after["orders"] if o["status"] == "open"]
        a_late, a_days = _lateness(a_orders)
        saved = [o["id"] for o in a_orders
                 if o["id"] in orders and orders[o["id"]]["slack"] < 0 <= o["slack"]]
        if a_late >= base_late and a_days >= base_days:
            continue      # does not help: do not bother the planner
        unit = mat.unit_cost * (1 + EXPEDITE_PREMIUM if c["expedite"] else 1)
        out.append({
            "id": f"PR-{mid}-{today}-{'X' if c['expedite'] else 'R'}",
            "material": mid, "material_name": mat.name, "unit": mat.unit,
            "supplier": mat.supplier, "supplier_name": cat.suppliers[mat.supplier].name,
            "qty": c["qty"], "expedite": c["expedite"], "expected_day": expected,
            "eta_day": round(fake["eta"], 1), "cost": round(c["qty"] * unit, 2),
            "kind": c["kind"], "reason_orders": c["orders"], "short": c["short"],
            "slow_po": c.get("po"), "slow_po_eta": c.get("po_eta"),
            "effect": {"late_before": base_late, "late_after": a_late,
                       "late_days_before": base_days, "late_days_after": a_days,
                       "orders_saved": saved},
            "created": now, "status": "pending",
        })
    out.sort(key=lambda p: -(p["effect"]["late_days_before"] - p["effect"]["late_days_after"]))
    return out


def supplier_email(p: dict) -> str:
    """Draft only. Sentinel never sends it; a human approves first."""
    speed = "EXPEDITED delivery" if p["expedite"] else "standard delivery"
    return (f"To: {p['supplier_name']}\nSubject: Purchase order request - {p['material_name']} "
            f"({p['material']})\n\nPlease confirm {p['qty']} {p['unit']} of {p['material_name']} "
            f"with {speed}, required by day {p['expected_day']}.\n\n"
            f"[Draft generated by Sentinel. Not sent until a planner approves.]")


# ---------------------------------------------------------------- explain
NUM_RE = re.compile(r"(?<![A-Za-z0-9-])[-+]?\d+(?:\.\d+)?")


def _flatten_numbers(obj, out: set):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(round(float(obj), 1))
        out.add(round(abs(float(obj)), 1))
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flatten_numbers(v, out)


def catalog_names(cat: Catalog) -> list[str]:
    names = [m.name for m in cat.materials.values()] + [p.name for p in cat.products.values()]         + [s.name for s in cat.suppliers.values()]
    return sorted(set(names), key=len, reverse=True)


def grounding_check(text: str, facts: list[dict], ignore: list[str] | None = None) -> dict:
    """Every number written in the text must appear in the facts.
    IDs such as M07, SO0042 or PO0012 are not counted as numbers, and names
    in `ignore` (catalog names like "Bearing 6202") are removed first."""
    for name in ignore or []:
        text = text.replace(name, " ")
    allowed: set = set()
    _flatten_numbers(facts, allowed)
    found = [float(x) for x in NUM_RE.findall(text)]
    bad = [x for x in found if round(x, 1) not in allowed and round(abs(x), 1) not in allowed]
    return {"numbers": len(found), "ungrounded": bad, "ok": not bad}


def briefing(role: str, snap: dict, proposals: list[dict], cat: Catalog) -> dict:
    k = snap["kpis"]
    now = math.floor(snap["now"])
    facts: list[dict] = []
    claims: list[dict] = []
    lines: list[str] = []

    def fact(fid, **vals):
        facts.append({"id": fid, **vals})

    fact("kpi", day=now, open_orders=k["open_orders"], late=k["late"], at_risk=k["at_risk"],
         on_track_pct=k["on_track_pct"], stockouts=k["stockouts"], overdue_pos=k["overdue_pos"],
         pending_proposals=len(proposals))
    late_orders = sorted((o for o in snap["orders"] if o["risk"] == "late" and o["status"] == "open"),
                         key=lambda o: o["slack"])
    stockouts = [m for m in snap["materials"] if m["uncovered"] > 0]
    overdue = [a for a in snap["alerts"] if a["kind"] == "po_overdue"]
    feeds = [a for a in snap["alerts"] if a["kind"] == "feed_down"]

    if feeds:
        for a in feeds:
            fact(f"feed:{a['data']['source']}", silent_days=a["data"]["silent_days"])
            lines.append(f"WARNING: the {a['data']['source'].upper()} feed has been silent for "
                         f"{a['data']['silent_days']} days, so figures below may be stale.")

    if role == "manager":
        lines.append(f"Day {now}: {k['open_orders']} open orders, {k['on_track_pct']}% on track, "
                     f"{k['late']} projected late and {k['at_risk']} at risk.")
        lines.append(f"{k['stockouts']} material(s) heading for a stock-out; {k['overdue_pos']} purchase "
                     f"order(s) overdue; {len(proposals)} purchase proposal(s) waiting for approval.")
        if proposals:
            spend = round(sum(p["cost"] for p in proposals), 2)
            saved = sum(len(p["effect"]["orders_saved"]) for p in proposals)
            fact("proposals", spend=spend, orders_saved=saved)
            lines.append(f"Approving them would cost {spend} and bring {saved} late order(s) back on time.")
    elif role == "buyer":
        lines.append(f"Day {now}: {len(proposals)} purchase proposal(s) to review, {k['overdue_pos']} overdue PO(s).")
        for a in overdue[:5]:
            d = a["data"]
            fact(f"po:{d['po']}", expected=d["expected"])
            lines.append(f"- PO {d['po']} ({d['material']}) was due day {d['expected']:g} and is overdue.")
        for p in proposals[:5]:
            fact(f"prop:{p['id']}", qty=p["qty"], cost=p["cost"], expected_day=p["expected_day"])
            lines.append(f"- Proposal {p['id']}: {p['qty']} {p['unit']} of {p['material_name']} from "
                         f"{p['supplier_name']}{' (expedited)' if p['expedite'] else ''}, arriving day "
                         f"{p['expected_day']}, cost {p['cost']}.")
        worst = sorted(snap["suppliers"].values(), key=lambda s: -(s["avg_delay_days"] or 0))[:2]
        for s in worst:
            if s["avg_delay_days"]:
                fact(f"sup:{s['id']}", avg_delay_days=s["avg_delay_days"], delivered=s["delivered"])
                lines.append(f"- {s['name']} averages {s['avg_delay_days']} days late over "
                             f"{s['delivered']} deliveries.")
    else:  # planner
        lines.append(f"Day {now}: {k['late']} order(s) projected late, {k['at_risk']} at risk, "
                     f"{k['stockouts']} stock-out(s) ahead.")
        for o in late_orders[:5]:
            fact(f"order:{o['id']}", crd=o["crd"], projected_ship=o["projected_ship"], days_late=-o["slack"],
                 causes=[{k: v for k, v in r.items() if k in ("material", "kind", "po", "eta", "short")}
                         for r in o["reasons"]])
            claims.append({"kind": "order", "id": o["id"], "field": "crd", "value": o["crd"]})
            why = _why(o, cat)
            lines.append(f"- {o['id']} ({cat.products[o['product']].name}): CRD day {o['crd']}, projected "
                         f"ship day {o['projected_ship']} ({-o['slack']} day(s) late). {why}")
        for m in stockouts[:4]:
            fact(f"mat:{m['id']}", on_hand=m["on_hand"], incoming=m["incoming"], allocated=m["allocated"],
                 net=m["net"], short=m["uncovered"], stockout_day=m["stockout_day"])
            for f in ("on_hand", "incoming", "allocated", "net"):
                claims.append({"kind": "material", "id": m["id"], "field": f, "value": m[f]})
            lines.append(f"- {m['name']} ({m['id']}): on hand {m['on_hand']:g} + incoming {m['incoming']:g} "
                         f"- allocated {m['allocated']:g} = net {m['net']:g}; short {m['uncovered']:g} "
                         f"from day {m['stockout_day']}.")
        tight = sorted((m for m in snap["materials"] if m["uncovered"] <= 0 and m["days_of_cover"] is not None),
                       key=lambda m: m["days_of_cover"])[:3]
        if tight:
            lines.append("Tightest materials:")
        for m in tight:
            fact(f"mat:{m['id']}", on_hand=m["on_hand"], incoming=m["incoming"], allocated=m["allocated"],
                 net=m["net"], days_of_cover=m["days_of_cover"])
            for f in ("on_hand", "incoming", "allocated", "net"):
                claims.append({"kind": "material", "id": m["id"], "field": f, "value": m[f]})
            lines.append(f"- {m['name']} ({m['id']}): {m['on_hand']:g} on hand ({m['days_of_cover']:g} days of cover), "
                         f"{m['incoming']:g} incoming, {m['allocated']:g} allocated, net {m['net']:g}.")
        if proposals:
            p = proposals[0]
            e = p["effect"]
            fact(f"prop:{p['id']}", qty=p["qty"], late_before=e["late_before"], late_after=e["late_after"],
                 late_days_before=e["late_days_before"], late_days_after=e["late_days_after"])
            lines.append(f"Top action: approve {p['id']} ({p['qty']} {p['unit']} of {p['material_name']}"
                         f"{', expedited' if p['expedite'] else ''}): late orders {e['late_before']} -> "
                         f"{e['late_after']}, total days late {e['late_days_before']} -> {e['late_days_after']}.")
    text = "\n".join(lines)
    ignore = catalog_names(cat)
    out = {"role": role, "text": text, "facts": facts, "claims": claims, "mode": "template",
           "grounding": grounding_check(text, facts, ignore), "ignore": ignore}
    if os.environ.get("SENTINEL_LLM") == "1":
        out = _llm_rewrite(out)
    return out


def _why(o: dict, cat: Catalog) -> str:
    parts = []
    for r in o["reasons"]:
        name = cat.materials[r["material"]].name
        if r["kind"] == "no_supply":
            parts.append(f"{name} is short {r['short']:g} with nothing on order")
        elif r["kind"] == "waiting_po":
            parts.append(f"waiting for {name} on {r['po']} (expected day {r['eta']:g})")
    return ("Cause: " + "; ".join(parts) + ".") if parts else ""


def explain_order(snap: dict, oid: str, cat: Catalog) -> dict:
    o = next((x for x in snap["orders"] if x["id"] == oid), None)
    if not o:
        return {"text": f"No open order {oid}.", "facts": []}
    facts = [{"id": f"order:{oid}", "qty": o["qty"], "crd": o["crd"], "projected_ship": o["projected_ship"],
              "slack": o["slack"]}]
    lines = [f"Order {oid}: {o['qty']} x {cat.products[o['product']].name}, CRD day {o['crd']}, "
             f"projected ship day {o['projected_ship']} (slack {o['slack']} day(s), {o['risk']})."]
    for m, draws in o.get("draws", {}).items():
        mat = cat.materials[m]
        need = cat.products[o["product"]].bom[m] * o["qty"]
        facts.append({"id": f"need:{m}", "need": need, "draws": draws,
                      "short": o["uncovered"].get(m, 0)})
        src = ", ".join(f"{d['qty']:g} from {'stock' if d['lot'] == 'stock' else d['lot']} (day {d['eta']:g})"
                        for d in draws) or "nothing available"
        short = f"; short {o['uncovered'][m]:g}" if m in o["uncovered"] else ""
        lines.append(f"- {mat.name}: needs {need:g}; gets {src}{short}.")
    lines.append(f"Rule: orders are served by earliest CRD; production starts when every material is "
                 f"covered and takes {PROD_DAYS} days.")
    facts.append({"id": "rule", "prod_days": PROD_DAYS})
    text = "\n".join(lines)
    return {"text": text, "facts": facts, "grounding": grounding_check(text, facts, catalog_names(cat))}


def _llm_rewrite(b: dict) -> dict:
    """Optional: Claude rewrites the briefing in plain language. Its output is
    used only if it passes the grounding check; otherwise the template stays."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-opus-5-5",
            max_tokens=2000,
            output_config={"effort": "low"},
            system=("You write short operational briefings for a factory's "
                    f"{b['role']}. Use ONLY the facts provided. Never invent or compute a number; "
                    "copy numbers exactly as given. At most 120 words, plain sentences."),
            messages=[{"role": "user", "content": "Facts:\n" + json.dumps(b["facts"]) +
                       "\n\nDraft to improve:\n" + b["text"]}],
        )
        if resp.stop_reason == "refusal":
            return {**b, "llm": "refused"}
        text = "".join(blk.text for blk in resp.content if blk.type == "text").strip()
        g = grounding_check(text, b["facts"], b.get("ignore"))
        if g["ok"] and text:
            return {**b, "text": text, "mode": "claude", "grounding": g}
        return {**b, "llm": "rejected: ungrounded numbers " + str(g["ungrounded"][:5])}
    except Exception as e:   # no key, network down, etc. -> keep the template
        return {**b, "llm": f"unavailable ({type(e).__name__})"}
