"""Sentinel web app: a live plant you can play forward, the planner's
dashboard, a human approval queue, and demo buttons that inject problems.

Run:  python -m uvicorn sentinel.server:app --port 8000
"""
from __future__ import annotations

import math
import random
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import agent, engine
from .catalog import PROD_DAYS
from .world import World

WEB = Path(__file__).resolve().parents[1] / "web"
APPROVER_ROLES = {"planner", "buyer"}


class Session:
    def __init__(self, seed: int = 7):
        self.seed = seed
        self.world = World(seed=seed, days=120)
        self.clock = 0.0
        self.rop_done = True
        self.proposals: dict[str, dict] = {}
        self.decisions: list[dict] = []
        self.first_seen: dict[str, float] = {}
        self.history: list[dict] = []
        self.injections: list[dict] = []
        self._snap = None
        self.advance(4.5)

    # -------------------------------------------------------------- time
    def advance(self, dt: float):
        w = self.world
        target = min(self.clock + dt, w.days - 0.01)
        while True:
            if not self.rop_done and w.day >= 0 and w.day + 0.9 <= target:
                w.rop_planner(w.day)
                self.rop_done = True
                continue
            nd = w.day + 1
            if nd <= target and nd < w.days and self.rop_done:
                w.simulate_day(nd)
                self.rop_done = False
                continue
            break
        w.deliver_until(target)
        self.clock = target
        self.refresh()

    def refresh(self):
        snap = engine.snapshot(self.world.store, self.clock)
        for a in snap["alerts"]:
            self.first_seen.setdefault(a["key"], self.clock)
            a["first_seen"] = self.first_seen[a["key"]]
        proj = {"orders": snap["orders"], "materials": snap["materials"], "suppliers": snap["suppliers"]}
        # Materials with a PO approved in the last day are left alone until it shows up.
        recent = {p["material"] for p in self.proposals.values()
                  if p["status"] == "approved" and self.clock - p["decided_at"] < 1.0}
        new = {p["material"]: p for p in agent.propose(snap["_state"], proj, self.world.cat, skip_materials=recent)}
        for old in self.proposals.values():
            if old["status"] != "pending":
                continue
            fresh = new.get(old["material"])
            if fresh is None:
                old["status"] = "expired"        # the problem went away on its own
                old["decided_at"] = self.clock
            elif fresh["qty"] != old["qty"] or fresh["expedite"] != old["expedite"]:
                old["status"] = "superseded"
                old["decided_at"] = self.clock
            else:
                old["effect"] = fresh["effect"]  # same proposal, refreshed what-if
                new.pop(old["material"])
        for p in new.values():
            if p["id"] in self.proposals:
                p["id"] = f"{p['id']}-{len(self.proposals)}"
            p["email"] = agent.supplier_email(p)
            self.proposals[p["id"]] = p
        self._snap = snap
        k = snap["kpis"]
        if not self.history or self.clock - self.history[-1]["t"] >= 0.24:
            self.history.append({"t": round(self.clock, 2), "late": k["late"], "at_risk": k["at_risk"],
                                 "open": k["open_orders"], "stockouts": k["stockouts"]})

    # -------------------------------------------------------------- human decisions
    def decide(self, pid: str, approve: bool, role: str, note: str = "") -> dict:
        p = self.proposals.get(pid)
        if not p:
            raise HTTPException(404, "no such proposal")
        if p["status"] != "pending":
            raise HTTPException(409, f"proposal already {p['status']}")
        p["status"] = "approved" if approve else "rejected"
        p["decided_at"] = self.clock
        p["decided_by"] = role
        rec = {"t": round(self.clock, 2), "proposal": pid, "decision": p["status"], "by": role, "note": note}
        if approve:
            po = self.world.create_po(p["material"], p["qty"], self.clock, expedite=p["expedite"], source="erp")
            p["po_id"] = po
            rec["po"] = po
            rec["email"] = "queued for the buyer to send (draft only; no email is sent by Sentinel)"
            self.world.deliver_until(self.clock + 0.025)
            self.clock += 0.025
        self.decisions.append(rec)
        self.refresh()
        return rec

    # -------------------------------------------------------------- demo injections
    def inject(self, kind: str) -> dict:
        w, r = self.world, random.Random(len(self.injections) + 99)
        d = math.floor(self.clock)
        info: dict[str, Any] = {"t": round(self.clock, 2), "kind": kind}
        if kind == "spike":
            prod = r.choice(list(w.cat.products))
            w.spike_days[d + 1] = prod
            info["detail"] = f"{w.cat.products[prod].name} gets a burst of orders tomorrow (day {d + 1})"
        elif kind == "supplier_delay":
            open_pos = [(pid, p) for pid, p in w.pos.items() if not p["received"] and p["actual"] > self.clock + 1]
            if not open_pos:
                return {**info, "detail": "no open PO to delay"}
            # Hit the PO that the most open orders are waiting on (that is what hurts in real life).
            waiting: dict[str, int] = {}
            for o in self._snap["orders"]:
                for r in o.get("reasons", []):
                    if r["kind"] == "waiting_po":
                        waiting[r["po"]] = waiting.get(r["po"], 0) + 1
            pid, p = max(open_pos, key=lambda x: (waiting.get(x[0], 0), x[1]["qty"]))
            p["actual"] += 7
            w.truth.pos[pid]["actual"] = p["actual"]
            w._emit("po_date_changed", self.clock + 0.01, "supplier", po_id=pid, expected=p["actual"])
            info["detail"] = f"{pid} ({w.cat.materials[p['material']].name}) slips 7 days to day {p['actual']}"
        elif kind == "outage":
            src = "wms"
            start, end = self.clock, self.clock + 1.25
            w.outage = (src, start, end)
            w.truth.outages.append({"source": src, "start": start, "end": end})
            keep = []
            for (arr, seq, ev) in w._pending:
                if ev.get("source") == src and isinstance(ev.get("ts"), (int, float)) and ev["ts"] >= start:
                    if ev["type"] == "heartbeat":
                        continue
                    arr = max(arr, end + 0.01)
                keep.append((arr, seq, ev))
            import heapq
            heapq.heapify(keep)
            w._pending = keep
            info["detail"] = f"Warehouse (WMS) feed goes silent for 30 hours from day {start:.2f}"
        elif kind == "shrink":
            m = max(w.cat.materials, key=lambda m: w.physical[m] * w.cat.materials[m].unit_cost)
            q = math.floor(w.physical[m] * 0.4)
            w.physical[m] -= q
            w.count_due.setdefault(d + 2, []).append(m)
            w.truth.shrinks.append({"day": d, "material": m, "qty": q, "count_day": d + 2})
            info["detail"] = f"{q} {w.cat.materials[m].unit} of {w.cat.materials[m].name} silently go missing"
        elif kind == "junk":
            for i in range(5):
                w._push(self.clock + 0.001 * i, {"event_id": f"JUNK-{len(self.injections)}-{i}", "type": "goods_received",
                                                 "ts": self.clock, "source": "wms", "po_id": "PO??",
                                                 "material": "M99", "qty": -10})
            info["detail"] = "5 malformed events sent into the feed"
        else:
            raise HTTPException(400, "unknown injection")
        self.injections.append(info)
        self.world.deliver_until(self.clock + 0.02)
        self.clock += 0.02
        self.refresh()
        return info

    # -------------------------------------------------------------- views
    def view(self, role: str) -> dict:
        s = self._snap
        props = sorted((p for p in self.proposals.values() if p["status"] == "pending"),
                       key=lambda p: -(p["effect"]["late_days_before"] - p["effect"]["late_days_after"]))
        brief = agent.briefing(role, s, props, self.world.cat)
        brief.pop("ignore", None)
        cat = self.world.cat
        # Copies: the snapshot's orders keep their material draws for /explain.
        orders = [{**{k: v for k, v in o.items() if k != "draws"}, "product_name": cat.products[o["product"]].name}
                  for o in s["orders"] if o["status"] in ("open", "in_production")]
        orders.sort(key=lambda o: (o["slack"], o["crd"]))
        return {
            "now": self.clock, "day": math.floor(self.clock), "seed": self.seed, "role": role,
            "kpis": s["kpis"], "alerts": s["alerts"], "orders": orders[:150],
            "materials": sorted(s["materials"], key=lambda m: (-m["uncovered"], m["days_of_cover"] or 0)),
            "suppliers": list(s["suppliers"].values()),
            "pos": [p for p in s["pos"] if p["status"] == "open"],
            "proposals": props,
            "decisions": self.decisions[-15:][::-1],
            "briefing": brief,
            "feeds": {src: self.world.store.last_seen.get(src) for src in ("erp", "wms", "supplier")},
            "quality": {**self.world.store.stats, "recent_quarantine": [
                {"reason": q["reason"], "arrival": q["arrival"]} for q in self.world.store.quarantine[-6:]][::-1]},
            "history": self.history[-160:],
            "injections": self.injections[-8:][::-1],
            "rule": ("Open orders are served by earliest Customer Request Date (ties: earliest created). "
                     "Each takes stock on hand first, then incoming POs by risk-adjusted arrival. Production "
                     f"starts when every material is covered and takes {PROD_DAYS} days."),
        }


app = FastAPI(title="Sentinel")
_lock = threading.Lock()
S = Session()


def _role(r: str | None) -> str:
    r = (r or "planner").lower()
    if r not in ("planner", "buyer", "manager"):
        raise HTTPException(400, "role must be planner, buyer or manager")
    return r


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/state")
def state(role: str = "planner"):
    with _lock:
        return JSONResponse(S.view(_role(role)))


@app.get("/api/evidence")
def evidence():
    """Headline numbers from the last evaluation run (results/results.json)."""
    import json
    p = WEB.parent / "results" / "results.json"
    if not p.exists():
        raise HTTPException(404, "no evaluation results in this build")
    R = json.loads(p.read_text(encoding="utf-8"))
    d3 = R["E1_detection"]["ours@3"]
    st3 = R["E1_detection"]["static@3"]
    cmp = R["E5_vs_baseline"]["agent"]
    pm = R["E5_policy_means"]
    return {
        "seeds": R["seeds"], "days": R["days"], "orders": R["dataset"]["orders_scored"],
        "late_orders": R["dataset"]["late_orders"],
        "on_time": {"baseline": pm["baseline"]["on_time_pct"], "agent": pm["agent"]["on_time_pct"],
                    "half": pm["agent_half_approved"]["on_time_pct"],
                    "diff": cmp["on_time_pct"]["mean_diff"], "ci": cmp["on_time_pct"]["ci95"],
                    "p": cmp["on_time_pct"]["p_value"], "seeds_better": cmp["on_time_pct"]["seeds_better"]},
        "late_days": {"baseline": pm["baseline"]["late_days_total"], "agent": pm["agent"]["late_days_total"]},
        "stockout_days": {"baseline": pm["baseline"]["stockout_material_days"],
                          "agent": pm["agent"]["stockout_material_days"]},
        "spend": {"diff": cmp["po_spend"]["mean_diff"], "p": cmp["po_spend"]["p_value"],
                  "baseline": pm["baseline"]["po_spend"]},
        "detection": {"recall": d3["recall"], "precision": d3["precision"], "static_recall": st3["recall"],
                      "lead_median": R["E1_lead_time"]["ours"]["median_days"],
                      "lead_static": R["E1_lead_time"]["static"]["median_days"]},
        "numbers": {"checks": R["E3_numbers"]["materials_checked"] * 4, "mismatches": R["E3_numbers"]["mismatches"],
                    "faulty_identical": R["E3_faulty_vs_clean"]["identical"],
                    "faulty_total": R["E3_faulty_vs_clean"]["snapshots"],
                    "replay": R["E3_replay"]["identical"], "replay_total": R["E3_replay"]["replays"],
                    "dups": R["E3_feed_faults"]["dups"], "junk": R["E3_feed_faults"]["junk"]},
        "agent": {"briefings": R["E4_agent"]["briefings"], "grounded": R["E4_agent"]["grounded"],
                  "numbers": R["E4_agent"]["numbers"], "claims_ok": R["E4_agent"]["material_ok"],
                  "claims": R["E4_agent"]["material_claims"]},
        "alerts": {"outages": R["E2_feed"]["outages"], "outages_found": R["E2_feed"]["detected"],
                   "spikes": R["E2_spikes"]["spikes"], "spikes_found": R["E2_spikes"]["detected"],
                   "shrink": R["E2_discrepancies"]["planted"], "shrink_found": R["E2_discrepancies"]["detected"],
                   "delays": R["E2_supplier_delays"]["delayed_pos"],
                   "delays_warned": R["E2_supplier_delays"]["warned_before_arrival"]},
    }


@app.post("/api/advance")
async def advance(req: Request):
    body = await req.json() if (await req.body()) else {}
    days = float(body.get("days", 0.25))
    if not 0 < days <= 10:
        raise HTTPException(400, "days must be in (0, 10]")
    with _lock:
        S.advance(days)
        return {"now": S.clock}


@app.post("/api/reset")
async def reset(req: Request):
    global S
    body = await req.json() if (await req.body()) else {}
    with _lock:
        S = Session(int(body.get("seed", 7)))
        return {"now": S.clock, "seed": S.seed}


@app.post("/api/proposals/{pid}/{action}")
async def decide(pid: str, action: str, req: Request, x_role: str | None = Header(default=None)):
    role = _role(x_role)
    if role not in APPROVER_ROLES:
        raise HTTPException(403, f"a {role} cannot approve or reject purchase orders")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    body = await req.json() if (await req.body()) else {}
    with _lock:
        return S.decide(pid, action == "approve", role, str(body.get("note", ""))[:200])


@app.post("/api/inject/{kind}")
def inject(kind: str):
    with _lock:
        return S.inject(kind)


@app.get("/api/orders/{oid}/explain")
def explain(oid: str):
    with _lock:
        return agent.explain_order(S._snap, oid, S.world.cat)


@app.post("/api/events")
async def ingest(req: Request):
    """Ingest external events (list of JSON objects). Idempotent: re-sending
    the same batch changes nothing. Malformed events are quarantined."""
    batch = await req.json()
    if not isinstance(batch, list):
        batch = [batch]
    with _lock:
        res = [S.world.store.ingest(ev, S.clock) for ev in batch[:5000]]
        S.refresh()
        return {k: res.count(k) for k in ("accepted", "duplicate", "quarantined", "conflict")}
