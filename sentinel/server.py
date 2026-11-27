"""Sentinel web app: a live plant you can play forward, the planner's
dashboard, a human approval queue, and demo buttons that inject problems.

Run:  python -m uvicorn sentinel.server:app --port 8000
"""
from __future__ import annotations

import json
import math
import random
import secrets
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from . import agent, ambiguity, engine, supplier_text
from .catalog import PROD_DAYS
from .world import Faults, World

WEB = Path(__file__).resolve().parents[1] / "web"
APPROVER_ROLES = {"planner", "buyer"}


class Session:
    def __init__(self, seed: int = 7):
        self.seed = seed
        # Suppliers write most delay notices in plain language here (Problem 6 in the demo).
        self.world = World(seed=seed, days=120, faults=Faults(supplier_text=0.8, confirmations=0.15))
        self.questions: dict[str, dict] = {}
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
        self._sync_questions(snap)
        self._snap = snap
        k = snap["kpis"]
        if not self.history or self.clock - self.history[-1]["t"] >= 0.24:
            self.history.append({"t": round(self.clock, 2), "late": k["late"], "at_risk": k["at_risk"],
                                 "open": k["open_orders"], "stockouts": k["stockouts"]})

    # -------------------------------------------------------------- supplier questions
    def _sync_questions(self, snap):
        cat = self.world.cat
        live = {}
        for p in snap["_state"].pos.values():
            u = p.get("unclear")
            if p["status"] == "open" and u:
                live["Q-" + u["event_id"]] = (p, u)
        for qid, (p, u) in live.items():
            if qid not in self.questions:
                self.questions[qid] = {
                    "id": qid, "po": p["id"], "event_id": u["event_id"], "status": "pending",
                    "created": round(self.clock, 3), "supplier": cat.suppliers[p["supplier"]].name,
                    "material": p["material"], "material_name": cat.materials[p["material"]].name,
                    "message": u["text"], "sent_ts": u["ts"], "question": u["question"],
                    "read": {k: u[k] for k in ("type", "lo", "hi", "readings", "explanation", "cues")},
                    "lo_label": ambiguity.fmt(u["lo"]), "hi_label": ambiguity.fmt(u["hi"])}
        for q in self.questions.values():
            if q["status"] in ("pending", "sent") and q["id"] not in live:
                q["status"] = "answered" if q["status"] == "sent" else "resolved"
                q["closed_at"] = round(self.clock, 3)
                p = snap["_state"].pos.get(q["po"])
                if p:
                    q["final_day"] = p["expected"]
                    q["final_label"] = ambiguity.fmt(p["expected"])

    def decide_question(self, qid: str, send: bool, role: str) -> dict:
        q = self.questions.get(qid)
        if not q:
            raise HTTPException(404, "no such question")
        if q["status"] != "pending":
            raise HTTPException(409, f"question already {q['status']}")
        q["status"] = "sent" if send else "dismissed"
        q["decided_at"], q["decided_by"] = round(self.clock, 3), role
        rec = {"t": round(self.clock, 2), "question": qid, "po": q["po"], "decision": q["status"], "by": role}
        if send:
            when = self.world.answer_question(q["po"], self.clock)
            rec["note"] = ("question sent to the supplier after approval" +
                           (f"; in this demo the supplier replies around day {when:.2f}" if when else ""))
        self.decisions.append(rec)
        self.refresh()
        return rec

    def set_date(self, po: str, day: int, role: str) -> dict:
        p = self._snap["_state"].pos.get(po)
        if not p or p["status"] != "open":
            raise HTTPException(404, "no open PO with that id")
        if not math.floor(self.clock) <= day <= math.floor(self.clock) + 60:
            raise HTTPException(400, "day must be between today and 60 days ahead")
        self.world._emit("po_date_changed", self.clock + 0.001, "sentinel", prompt=True, po_id=po, expected=int(day))
        self.world.deliver_until(self.clock + 0.03)
        self.clock += 0.03
        rec = {"t": round(self.clock, 2), "po": po, "decision": "date set by hand", "by": role,
               "note": f"expected day set to {day} ({ambiguity.fmt(day)})"}
        self.decisions.append(rec)
        self.refresh()
        return rec

    def post_message(self, po: str, text: str) -> dict:
        p = self._snap["_state"].pos.get(po)
        if not p:
            raise HTTPException(404, "no PO with that id")
        ts = self.clock + 0.001
        ev = self.world._emit("supplier_message", ts, "supplier", prompt=True, po_id=po, text=text)
        self.world.deliver_until(self.clock + 0.03)
        self.clock += 0.03
        self.refresh()
        return {"event_id": ev["event_id"], "read": ambiguity.interpret(text, ts, p["orig_expected"], po)}

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
            w._emit("po_date_changed", self.clock + 0.01, "supplier", prompt=True, po_id=pid, expected=p["actual"])
            info["detail"] = f"{pid} ({w.cat.materials[p['material']].name}) slips 7 days to day {p['actual']}"
        elif kind == "vague":
            open_pos = [(pid, p) for pid, p in w.pos.items() if not p["received"] and p["actual"] > self.clock + 1]
            if not open_pos:
                return {**info, "detail": "no open PO to delay"}
            waiting: dict[str, int] = {}
            for o in self._snap["orders"]:
                for rr in o.get("reasons", []):
                    if rr["kind"] == "waiting_po":
                        waiting[rr["po"]] = waiting.get(rr["po"], 0) + 1
            pid, p = max(open_pos, key=lambda x: (waiting.get(x[0], 0), x[1]["qty"]))
            p["actual"] = max(p["actual"], p["expected"]) + r.randint(4, 8)
            w.truth.pos[pid]["actual"] = p["actual"]
            text, label = supplier_text.delay_message(w.trng, pid, self.clock + 0.01, p["expected"], p["actual"],
                                                      force="ambiguous")
            w.send_text(pid, self.clock + 0.01, text, label, "notice", prompt=True)
            info["detail"] = f"Supplier of {pid} ({w.cat.materials[p['material']].name}) writes: \u201c{text}\u201d"
            info["po"] = pid
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
        for p in props:   # a purchase based on a feed that has gone quiet may rest on stale numbers
            p["stale_feeds"] = list(s["kpis"]["feeds_down"])
        brief = agent.briefing(role, s, props, self.world.cat)
        brief.pop("ignore", None)
        brief.pop("names", None)
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
            "messages": [{**m, "sent_label": ambiguity.fmt(math.floor(m["ts"])),
                          "plan_label": ambiguity.fmt(m["read"]["plan_day"]) if m["read"]["plan_day"] is not None else None,
                          "lo_label": ambiguity.fmt(m["read"]["lo"]) if m["read"]["lo"] is not None else None}
                         for m in s["messages"]],
            "questions": sorted((q for q in self.questions.values()
                                 if q["status"] in ("pending", "sent") or self.clock - q.get("closed_at", 0) < 2),
                                key=lambda q: (q["status"] != "pending", -q["created"]))[:10],
            "today_label": ambiguity.fmt(math.floor(self.clock)),
            "rule": ("Open orders are served by earliest Customer Request Date (ties: earliest created). "
                     "Each takes stock on hand first, then incoming POs by risk-adjusted arrival. Production "
                     f"starts when every material is covered and takes {PROD_DAYS} days."),
        }


app = FastAPI(title="Sentinel")

# ------------------------------------------------------------------ sessions
# Every visitor gets their own plant (a cookie), so one person's clicks, resets or
# test messages never change what another visitor or a judge is looking at.
MAX_SESSIONS = 24
IDLE_SECONDS = 45 * 60
_sessions: "OrderedDict[str, tuple[Session, threading.Lock, float]]" = OrderedDict()
_reg_lock = threading.Lock()
_writes: dict[str, deque] = {}
WRITE_LIMIT, WRITE_WINDOW = 40, 10.0     # at most 40 changes per 10 s per visitor


def _session(request: Request, response: Response | None = None) -> tuple[Session, threading.Lock, str]:
    sid = request.cookies.get("sid") or ""
    now = time.time()
    with _reg_lock:
        for k in [k for k, (_, _, t) in _sessions.items() if now - t > IDLE_SECONDS]:
            del _sessions[k]
            _writes.pop(k, None)
        if sid not in _sessions:
            sid = secrets.token_urlsafe(16)
            while len(_sessions) >= MAX_SESSIONS:
                old, _ = _sessions.popitem(last=False)
                _writes.pop(old, None)
            _sessions[sid] = (Session(), threading.Lock(), now)
        sess, lock, _ = _sessions[sid]
        _sessions[sid] = (sess, lock, now)
        _sessions.move_to_end(sid)
    if response is not None:
        response.set_cookie("sid", sid, max_age=IDLE_SECONDS, httponly=True, samesite="lax")
    return sess, lock, sid


def _throttle(sid: str):
    q = _writes.setdefault(sid, deque())
    now = time.time()
    while q and now - q[0] > WRITE_WINDOW:
        q.popleft()
    if len(q) >= WRITE_LIMIT:
        raise HTTPException(429, "too many changes; slow down")
    q.append(now)


async def _body(req: Request, kind=dict):
    raw = await req.body()
    if not raw:
        return kind()
    if len(raw) > 2_000_000:
        raise HTTPException(413, "body too large")
    try:
        b = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "body is not valid JSON")
    if kind is dict and not isinstance(b, dict):
        raise HTTPException(400, "body must be a JSON object")
    return b


def _num(v, name, lo, hi, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, f"{name} must be a number")
    if (isinstance(x, float) and not math.isfinite(x)) or not (lo <= x <= hi):
        raise HTTPException(400, f"{name} must be between {lo} and {hi}")
    return x


def _role(r: str | None) -> str:
    r = (r or "planner").lower()
    if r not in ("planner", "buyer", "manager"):
        raise HTTPException(400, "role must be planner, buyer or manager")
    return r


def _approver(x_role: str | None, what: str) -> str:
    role = _role(x_role)
    if role not in APPROVER_ROLES:
        raise HTTPException(403, f"a {role} cannot {what}")
    return role


async def _run(req: Request, response: Response, fn, write: bool = True):
    sess, lock, sid = _session(req, response)
    if write:
        _throttle(sid)

    def work():
        with lock:
            return fn(sess)
    return await run_in_threadpool(work)


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/state")
async def state(req: Request, response: Response, role: str = "planner"):
    r = _role(role)
    return await _run(req, response, lambda s: s.view(r), write=False)


@app.get("/api/evidence")
def evidence():
    """Headline numbers from the last evaluation run (results/*.json)."""
    p = WEB.parent / "results" / "results.json"
    if not p.exists():
        raise HTTPException(404, "no evaluation results in this build")
    R = json.loads(p.read_text(encoding="utf-8"))
    pm, cmp = R["E5_policy_means"], R["E5_vs_baseline"]["agent"]
    d7, n7 = R["E1_detection"]["ours@7"], R["E1_detection"]["not_started@7"]
    mut = R.get("E4_mutations", {})
    e6 = None
    a = WEB.parent / "results" / "ambiguity.json"
    if a.exists():
        e6 = json.loads(a.read_text(encoding="utf-8")).get("headline")
    return {
        "ambiguity": e6,
        "seeds": R["seeds"], "days": R["days"], "orders": R["dataset"]["orders_scored"],
        "on_time": {"baseline": pm["baseline"]["on_time_pct"], "agent": pm["agent"]["on_time_pct"],
                    "diff": cmp["on_time_pct"]["mean_diff"], "ci": cmp["on_time_pct"]["ci95"],
                    "p": cmp["on_time_pct"]["p_value"], "seeds_better": cmp["on_time_pct"]["seeds_better"],
                    "spend_pct": 100 * (pm["agent"]["po_spend"] / pm["baseline"]["po_spend"] - 1)},
        "rules": {k: {"on_time": pm[k]["on_time_pct"],
                      "spend_pct": 100 * (pm[k]["po_spend"] / pm["agent"]["po_spend"] - 1),
                      "stock_pct": 100 * (pm[k]["avg_stock_value"] / pm["agent"]["avg_stock_value"] - 1)}
                  for k in ("rule_expedite", "baseline_safety4") if k in pm},
        "detection": {"precision7": d7["precision"], "recall7": d7["recall"], "naive_precision7": n7["precision"],
                      "lead_median": R["E1_lead_time"]["ours"]["median_days"]},
        "numbers": {"checks": R["E3_numbers"]["materials_checked"] * 3, "mismatches": R["E3_numbers"]["mismatches"],
                    "replay": R["E3_replay"]["identical"], "replay_total": R["E3_replay"]["replays"],
                    "dups": R["E3_feed_faults"]["dups"], "junk": R["E3_feed_faults"]["junk"]},
        "agent": {k: round(100 * v["strict"] / max(v["n"], 1)) for k, v in mut.items()},
    }


@app.post("/api/advance")
async def advance(req: Request, response: Response):
    b = await _body(req)
    days = _num(b.get("days", 0.25), "days", 0.001, 10)

    def fn(s):
        s.advance(days)
        return {"now": s.clock}
    return await _run(req, response, fn)


@app.post("/api/reset")
async def reset(req: Request, response: Response):
    b = await _body(req)
    seed = _num(b.get("seed", 7), "seed", 0, 10_000, int)
    sess, lock, sid = _session(req, response)
    _throttle(sid)
    fresh = await run_in_threadpool(lambda: Session(seed))
    with _reg_lock:
        _sessions[sid] = (fresh, lock, time.time())
    return {"now": fresh.clock, "seed": fresh.seed}


@app.post("/api/proposals/{pid}/{action}")
async def decide(pid: str, action: str, req: Request, response: Response, x_role: str | None = Header(default=None)):
    role = _approver(x_role, "approve or reject purchase orders")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    b = await _body(req)
    note = str(b.get("note", ""))[:200]
    return await _run(req, response, lambda s: s.decide(pid, action == "approve", role, note))


@app.post("/api/questions/{qid}/{action}")
async def question(qid: str, action: str, req: Request, response: Response, x_role: str | None = Header(default=None)):
    role = _approver(x_role, "send questions to suppliers")
    if action not in ("send", "dismiss"):
        raise HTTPException(400, "action must be send or dismiss")
    return await _run(req, response, lambda s: s.decide_question(qid, action == "send", role))


@app.post("/api/pos/{po}/date")
async def po_date(po: str, req: Request, response: Response, x_role: str | None = Header(default=None)):
    """A planner sets the date by hand (e.g. after phoning the supplier)."""
    role = _approver(x_role, "change PO dates")
    b = await _body(req)
    if "day" not in b:
        raise HTTPException(400, "body must be an object with an integer 'day'")
    day = _num(b["day"], "day", 0, 100_000, int)
    return await _run(req, response, lambda s: s.set_date(po, day, role))


@app.post("/api/messages")
async def message(req: Request, response: Response):
    """Type a supplier message yourself (demo). Body: {"po": "PO0012", "text": "..."}"""
    b = await _body(req)
    po, text = str(b.get("po", ""))[:40], str(b.get("text", "")).strip()
    if not text or len(text) > 500:
        raise HTTPException(400, "text must be 1-500 characters")
    return await _run(req, response, lambda s: s.post_message(po, text))


@app.post("/api/read")
async def read(req: Request, response: Response):
    """Read a message without changing anything (live preview while typing)."""
    b = await _body(req)
    text = str(b.get("text", ""))[:500]
    po = str(b.get("po", ""))[:40]

    def fn(s):
        p = s._snap["_state"].pos.get(po)
        due = p["orig_expected"] if p else math.floor(s.clock) + 3
        r = ambiguity.interpret(text, s.clock, due, p["id"] if p else "this order")
        for k in ("lo", "hi", "plan_day", "day"):
            if r.get(k) is not None:
                r[k + "_label"] = ambiguity.fmt(r[k])
        r["due"], r["due_label"] = due, ambiguity.fmt(due)
        r["today_label"] = ambiguity.fmt(math.floor(s.clock))
        return r
    return await _run(req, response, fn, write=False)


@app.post("/api/inject/{kind}")
async def inject(kind: str, req: Request, response: Response):
    return await _run(req, response, lambda s: s.inject(kind))


@app.get("/api/orders/{oid}/explain")
async def explain(oid: str, req: Request, response: Response):
    return await _run(req, response, lambda s: agent.explain_order(s._snap, oid, s.world.cat), write=False)


@app.post("/api/events")
async def ingest(req: Request, response: Response):
    """Ingest external events (a JSON object or a list of up to 5,000) into your session.
    Idempotent: re-sending the same batch changes nothing. Malformed events are quarantined."""
    batch = await _body(req, kind=object)
    if not isinstance(batch, list):
        batch = [batch]

    def fn(s):
        res = [s.world.store.ingest(ev, s.clock) for ev in batch[:5000]]
        s.refresh()
        return {k: res.count(k) for k in ("accepted", "duplicate", "quarantined", "conflict")}
    return await _run(req, response, fn)
