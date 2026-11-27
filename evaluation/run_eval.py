"""Sentinel evaluation. One command reproduces every number:

    python -m evaluation.run_eval --seeds 30

Experiments (all seeded; the plant simulator is the ground truth):
  E1 Detection     - does Sentinel flag orders that will really miss their CRD, early?
  E2 Alerts        - feed outages, demand spikes, supplier delays, stock discrepancies
  E3 Numbers       - are on-hand / incoming / allocated / net exactly right, under
                     duplicates, junk and shuffled delivery? is a replay identical?
  E4 Agent trust   - are the agent's briefings grounded and its claims correct?
  E5 Outcomes      - do planners do better with the agent's proposals than without?
Writes results/results.json, results/RESULTS.md and charts.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sentinel import agent, engine  # noqa: E402
from sentinel.catalog import PROD_DAYS  # noqa: E402
from sentinel.events import EventStore  # noqa: E402
from sentinel.world import Faults, World, agent_planner  # noqa: E402
from evaluation.reference import reference_numbers  # noqa: E402

DAYS = 60
HORIZONS = (3, 5, 7, 10)
DETECTORS = ("ours", "ours_any", "fair_ablation", "ablation", "static", "stock_only", "not_started")
OUT = Path(__file__).resolve().parents[1] / "results"


# ====================================================================== helpers
def boot_ci(values, fn=np.mean, n=5000, seed=0):
    v = np.asarray(values, dtype=float)
    if len(v) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    stats = [fn(v[rng.integers(0, len(v), len(v))]) for _ in range(n)]
    return (float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)))


def sign_flip_p(diffs, n=20000, seed=0):
    d = np.asarray(diffs, dtype=float)
    if len(d) == 0 or np.allclose(d, 0):
        return 1.0
    rng = np.random.default_rng(seed)
    obs = abs(d.mean())
    flips = rng.choice([-1, 1], size=(n, len(d)))
    return float((np.abs((flips * d).mean(axis=1)) >= obs - 1e-12).mean())


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f = 2 * p * r / (p + r) if p + r and not math.isnan(p) and not math.isnan(r) else float("nan")
    return p, r, f


def cluster_prf(per_seed, n=3000):
    """Bootstrap over seeds (orders inside one run are not independent)."""
    rng = np.random.default_rng(1)
    arr = np.array(per_seed, dtype=float)  # rows: tp, fp, fn
    fs, ps, rs = [], [], []
    for _ in range(n):
        s = arr[rng.integers(0, len(arr), len(arr))].sum(axis=0)
        p, r, f = prf(*s)
        ps.append(p); rs.append(r); fs.append(f)
    q = lambda x: (float(np.nanpercentile(x, 2.5)), float(np.nanpercentile(x, 97.5)))
    return q(ps), q(rs), q(fs)


# ====================================================================== E1-E4: one baseline run
def run_observed(seed: int) -> dict:
    """Plant runs with the baseline planner only (the agent observes, never acts),
    so detection is scored against outcomes the agent did not change."""
    w = World(seed=seed, days=DAYS)
    ablate = engine.EngineOptions(learn_supplier_delay=False, handle_overdue=False)
    fair = engine.EngineOptions(learn_supplier_delay=False, handle_overdue=True)
    feed_samples = []     # (t, [sources Sentinel reports down]) four times a day
    mut = {k: {"n": 0, "strict": 0, "loose": 0} for k in ("number_changed", "numbers_swapped",
                                                          "false_count", "unknown_id")}
    mrng = random.Random(seed * 7 + 1)
    flags = {}            # day -> {oid: {...}}
    alerts_by_day = {}
    eta_err = {"ours": [], "promised": [], "surprise_ours": [], "surprise_promised": []}
    claim_checks = {"material_claims": 0, "material_ok": 0, "order_claims": 0, "order_ok": 0}
    grounding = {"briefings": 0, "grounded": 0, "numbers": 0, "ungrounded_numbers": 0}
    numbers_check = {"snapshots": 0, "materials_checked": 0, "mismatches": 0, "examples": []}
    faulty_vs_clean = {"snapshots": 0, "identical": 0}
    clean_store = EventStore(w.cat)
    clean_ptr = 0

    for d in range(DAYS):
        w.simulate_day(d)
        for tt in (d + 0.2, d + 0.45, d + 0.7):          # feed alerts read from Sentinel itself
            w.deliver_until(tt)
            feed_samples.append((tt, [src for src, _ in engine.feeds_down(w.store, tt)]))
        w.rop_planner(d)
        t = d + 0.95
        w.deliver_until(t)
        snap = engine.snapshot(w.store, t)
        feed_samples.append((t, [a["data"]["source"] for a in snap["alerts"] if a["kind"] == "feed_down"]))
        st = snap["_state"]
        abl = engine.project(st, w.cat, ablate)
        fab = engine.project(st, w.cat, fair)
        static = engine.static_flags(st, w.cat)
        nopo = engine.project(engine.BookState(st.now, st.on_hand, st.orders, {}), w.cat, ablate)
        abl_by = {o["id"]: o for o in abl["orders"]}
        fab_by = {o["id"]: o for o in fab["orders"]}
        nopo_by = {o["id"]: o for o in nopo["orders"]}
        flags[d] = {o["id"]: {"ours": o["risk"] == "late", "ours_any": o["risk"] != "ok",
                              "fair_ablation": fab_by[o["id"]]["risk"] == "late",
                              "ablation": abl_by[o["id"]]["risk"] == "late",
                              "static": static.get(o["id"], False),
                              "stock_only": nopo_by[o["id"]]["risk"] == "late",
                              "not_started": True}
                    for o in snap["orders"] if o["status"] == "open"}
        alerts_by_day[d] = [(a["kind"], a["key"], a["data"]) for a in snap["alerts"]]

        # E3: numbers vs the independent reference, on the same delivered events
        ref = reference_numbers(w.truth.clean_events, w.cat, t)
        numbers_check["snapshots"] += 1
        for m in snap["materials"]:
            numbers_check["materials_checked"] += 1
            r = ref[m["id"]]
            for f in ("on_hand", "incoming", "allocated", "net"):
                if abs(m[f] - r[f]) > 0.05:
                    numbers_check["mismatches"] += 1
                    if len(numbers_check["examples"]) < 5:
                        numbers_check["examples"].append({"day": d, "material": m["id"], "field": f,
                                                          "sentinel": m[f], "reference": r[f]})
        # E3: faulty feed (dups, junk, shuffle) vs the same events delivered cleanly
        clean = sorted(w.truth.clean_events, key=lambda x: x[0])
        while clean_ptr < len(clean) and clean[clean_ptr][0] <= t:
            clean_store.ingest(clean[clean_ptr][1], clean[clean_ptr][0])
            clean_ptr += 1
        faulty_vs_clean["snapshots"] += 1
        if engine.snapshot(clean_store, t)["hash"] == snap["hash"]:
            faulty_vs_clean["identical"] += 1

        # E4: agent briefings, grounding and claims
        proj = {"orders": snap["orders"], "materials": snap["materials"], "suppliers": snap["suppliers"]}
        props = agent.propose(st, proj, w.cat)
        for role in ("planner", "buyer", "manager"):
            b = agent.briefing(role, snap, props, w.cat)
            if d % 3 == 0:
                _mutations(b, mrng, mut)
            grounding["briefings"] += 1
            grounding["grounded"] += int(b["grounding"]["ok"])
            grounding["numbers"] += b["grounding"]["numbers"]
            grounding["ungrounded_numbers"] += len(b["grounding"]["ungrounded"])
            for c in b["claims"]:
                if c["kind"] == "material":
                    claim_checks["material_claims"] += 1
                    claim_checks["material_ok"] += int(abs(ref[c["id"]][c["field"]] - c["value"]) <= 0.05)
                elif c["kind"] == "order":
                    claim_checks["order_claims"] += 1
                    claim_checks["order_ok"] += int(w.truth.orders[c["id"]]["crd"] == c["value"])

        # ETA accuracy: open POs one day after creation, projected vs promised
        sup = snap["suppliers"]
        for p in st.pos.values():
            if p["status"] == "open" and d - 1 < p["created"] <= d and not p["date_changes"]:
                actual = w.truth.pos[p["id"]]["actual"]
                eta_ours = engine.po_eta(p, t, sup, engine.EngineOptions())
                eta_err["ours"].append(abs(eta_ours - actual))
                eta_err["promised"].append(abs(p["expected"] - actual))
                eta_err["surprise_ours"].append(actual > math.ceil(eta_ours))
                eta_err["surprise_promised"].append(actual > p["expected"])

    # replay determinism: same events, 3 random arrival orders -> same final hash
    w.deliver_until(DAYS + 5)
    final = engine.snapshot(w.store, DAYS - 0.05)["hash"]
    raw = [(a, e) for (a, e) in w.truth.clean_events]
    replay_ok = 0
    for k in range(3):
        rr = random.Random(seed * 100 + k)
        evs = raw[:] + [(a, dict(e)) for (a, e) in rr.sample(raw, len(raw) // 20)]   # + 5 % dups
        rr.shuffle(evs)
        s2 = EventStore(w.cat)
        for a, e in evs:
            s2.ingest(e, a)
        replay_ok += int(engine.snapshot(s2, DAYS - 0.05)["hash"] == final)

    # ------------------------------------------------ E1 scoring
    truth_orders = w.truth.orders
    eval_orders = {oid: o for oid, o in truth_orders.items() if o["crd"] <= DAYS - 1}
    is_late = {oid: (o["ship_day"] is None or o["ship_day"] > o["crd"]) for oid, o in eval_orders.items()}
    det = {}
    for H in HORIZONS:
        for det_name in DETECTORS:
            tp = fp = fn = tn = 0
            for oid, o in eval_orders.items():
                day = o["crd"] - H
                f = flags.get(day, {}).get(oid)
                if f is None:        # not open at that point (already started) -> predicted on time
                    pred = False
                else:
                    pred = f[det_name]
                if pred and is_late[oid]: tp += 1
                elif pred: fp += 1
                elif is_late[oid]: fn += 1
                else: tn += 1
            det[f"{det_name}@{H}"] = [tp, fp, fn, tn]
    lead = {"ours": [], "fair_ablation": [], "ablation": [], "static": []}
    for oid, o in eval_orders.items():
        if not is_late[oid]:
            continue
        for name in lead:
            first = next((dd for dd in range(DAYS) if dd < o["crd"] and flags.get(dd, {}).get(oid, {}).get(name)), None)
            lead[name].append(o["crd"] - first if first is not None else 0)
    ever_false = {name: sum(1 for oid, o in eval_orders.items() if not is_late[oid] and any(
        flags.get(dd, {}).get(oid, {}).get(name) for dd in range(DAYS))) for name in ("ours", "fair_ablation", "ablation", "static")}

    # ------------------------------------------------ E2 scoring
    # feed outage: Sentinel's own feed alerts, sampled four times a day
    feed = {}
    for o in w.truth.outages:
        src = o["source"]
        first_alert, false_eps, active_prev = None, 0, False
        for t, down in feed_samples:
            active = src in down
            inside = o["start"] <= t <= o["end"] + 0.3      # +0.3: catching up after the burst
            if active and inside and first_alert is None:
                first_alert = t
            if active and not active_prev and not inside:
                false_eps += 1
            active_prev = active
        feed = {"source": src, "outage_days": round(o["end"] - o["start"], 2),
                "detected": first_alert is not None,
                "latency_days": round(first_alert - o["start"], 3) if first_alert else None,
                "false_alert_episodes": false_eps}
    # demand spikes
    spike_truth = {(s["day"], s["product"]) for s in w.truth.spikes}
    spike_alerts = {(dd, a[2]["product"]) for dd, al in alerts_by_day.items() for a in al if a[0] == "demand_spike"}
    spike_tp = sum(1 for s in spike_truth if s in spike_alerts)
    spike_fp = sum(1 for s in spike_alerts if s not in spike_truth)
    # supplier delays: first alert (delayed or overdue) before the goods actually arrived
    delayed = {pid: p for pid, p in w.truth.pos.items() if p["delay"] > 0 and p["actual"] <= DAYS - 1}
    first_po_alert = {}
    for dd in range(DAYS):
        for kind, key, data in alerts_by_day[dd]:
            if kind in ("po_delayed", "po_overdue"):
                first_po_alert.setdefault(data["po"], dd + 0.95)
    po_warn = [p["actual"] - first_po_alert[pid] for pid, p in delayed.items() if pid in first_po_alert]
    po_warn_notice = [p["actual"] - first_po_alert[pid] for pid, p in delayed.items()
                      if pid in first_po_alert and p["notified_at"] is not None]
    po_warn_silent = [p["actual"] - first_po_alert[pid] for pid, p in delayed.items()
                      if pid in first_po_alert and p["notified_at"] is None]
    po_false = sum(1 for pid in first_po_alert if w.truth.pos.get(pid, {}).get("delay", 0) == 0)
    # stock discrepancies
    shrink_found = []
    for s in w.truth.shrinks:
        hit = next((dd for dd in range(s["day"], DAYS) for kind, key, data in alerts_by_day[dd]
                    if kind == "stock_discrepancy" and data["material"] == s["material"]), None)
        shrink_found.append(None if hit is None else hit - s["day"])

    return {
        "seed": seed, "orders": len(eval_orders), "late": sum(is_late.values()),
        "det": det, "lead": lead, "ever_false": ever_false,
        "on_time_orders": sum(1 for v in is_late.values() if not v),
        "feed": feed, "spike": {"truth": len(spike_truth), "tp": spike_tp, "fp": spike_fp},
        "po": {"delayed": len(delayed), "warned": len(po_warn), "warn_lead": po_warn,
               "warn_notice": po_warn_notice, "warn_silent": po_warn_silent,
               "with_notice": sum(1 for p in delayed.values() if p["notified_at"] is not None),
               "false": po_false, "eta_err_ours": eta_err["ours"], "eta_err_promised": eta_err["promised"],
               "surprise_ours": eta_err["surprise_ours"], "surprise_promised": eta_err["surprise_promised"]},
        "shrink": shrink_found,
        "numbers": numbers_check, "faulty_vs_clean": faulty_vs_clean, "replay_ok": replay_ok,
        "grounding": grounding, "claims": claim_checks, "mutations": mut,
        "feed_stats": {"junk": w.truth.junk, "dups": w.truth.dups,
                       "quarantined": w.store.stats["quarantined"], "dup_ignored": w.store.stats["duplicates"]},
    }


# ====================================================================== E5: outcomes
def run_policy(args) -> dict:
    seed, policy = args
    w = World(seed=seed, days=DAYS, safety_days=4.0 if policy == "baseline_safety4" else 1.0)
    n_props = n_appr = 0
    for d in range(DAYS):
        w.simulate_day(d)
        w.rop_planner(d)
        if policy == "agent":
            ps = agent_planner(w, d)
        elif policy == "rule_expedite":
            ps = rule_expedite(w, d)
        elif policy == "agent_no_expedite":
            ps = agent_planner(w, d, allow_expedite=False)
        elif policy == "agent_no_learning":
            ps = agent_planner(w, d, opt=engine.EngineOptions(learn_supplier_delay=False, handle_overdue=False))
        elif policy == "agent_half_approved":
            rr = random.Random(seed * 13 + d)
            ps = agent_planner(w, d, approve=lambda p: rr.random() < 0.5)
        else:
            ps = []
        n_props += len(ps)
        n_appr += sum(1 for p in ps if p["status"] == "approved")
    ev = [o for o in w.truth.orders.values() if o["crd"] <= DAYS - 1]
    late = [o for o in ev if o["ship_day"] is None or o["ship_day"] > o["crd"]]
    days_late = [(o["ship_day"] if o["ship_day"] is not None else DAYS) - o["crd"] for o in late]
    return {"seed": seed, "policy": policy, "orders": len(ev),
            "on_time_pct": 100 * (1 - len(late) / len(ev)), "late_orders": len(late),
            "late_days_total": sum(days_late),
            "stockout_material_days": w.truth.stockout_material_days,
            "po_spend": w.truth.po_spend, "avg_stock_value": w.truth.stock_value_days / DAYS,
            "proposals": n_props, "approved": n_appr}


def rule_expedite(w: World, d: int) -> list[dict]:
    """A simple rule a planner could follow without Sentinel: whenever a PO is overdue or its
    supplier moves its date later, rush-order the open quantity again (once per PO)."""
    t = d + 0.95
    w.deliver_until(t)
    st = engine.rebuild(w.store, t)
    done = getattr(w, "_rushed", set())
    out = []
    for p in st.pos.values():
        if p["status"] != "open" or p["id"] in done or p["expedite"]:
            continue
        if t > p["expected"] + engine.OVERDUE_GRACE or p["expected"] > p["orig_expected"]:
            qty = p["qty"] - p["received"]
            if qty > 0:
                done.add(p["id"])
                w.create_po(p["material"], int(qty), t + 0.001, expedite=True, source="erp")
                out.append({"status": "approved"})
    w._rushed = done
    return out


def run_mismatch(seed: int) -> dict:
    """E1b: the plant builds orders oldest-first, while Sentinel assumes earliest-CRD-first.
    How much does detection degrade when the documented rule is not what the shop floor does?"""
    w = World(seed=seed, days=DAYS, plant_rule="fifo")
    flags = {}
    for d in range(DAYS):
        w.simulate_day(d)
        w.rop_planner(d)
        t = d + 0.95
        w.deliver_until(t)
        snap = engine.snapshot(w.store, t)
        flags[d] = {o["id"]: o["risk"] == "late" for o in snap["orders"] if o["status"] == "open"}
    ev = {oid: o for oid, o in w.truth.orders.items() if o["crd"] <= DAYS - 1}
    late = {oid: (o["ship_day"] is None or o["ship_day"] > o["crd"]) for oid, o in ev.items()}
    det = {}
    for H in HORIZONS:
        tp = fp = fn = 0
        for oid, o in ev.items():
            pred = flags.get(o["crd"] - H, {}).get(oid, False)
            tp += pred and late[oid]
            fp += pred and not late[oid]
            fn += (not pred) and late[oid]
        det[H] = [tp, fp, fn]
    return {"seed": seed, "orders": len(ev), "late": sum(late.values()), "det": det}


NUM_TOKEN = None


def _mutations(b: dict, r: random.Random, mut: dict):
    """E4: plant one error in a briefing and see whether the grounding check (strict, as used,
    and the earlier loose one) rejects it."""
    import re
    text, facts, ign, names = b["text"], b["facts"], b.get("ignore"), b.get("names")
    lines = text.split("\n")
    nums = [(i, m) for i, l in enumerate(lines) for m in re.finditer(r"(?<![A-Za-z])\d+(?:\.\d+)?", l)
            if not any(n in l[max(0, m.start() - 20):m.end() + 20] for n in (ign or []) if any(ch.isdigit() for ch in n))]

    def judge(kind, new_text):
        mut[kind]["n"] += 1
        mut[kind]["strict"] += int(not agent.grounding_check(new_text, facts, ign, names)["ok"])
        mut[kind]["loose"] += int(not agent.grounding_check_loose(new_text, facts, ign)["ok"])

    if nums:
        i, m = r.choice(nums)
        v = float(m.group(0))
        nv = v + r.choice([1, 2, 3, -1]) if v >= 2 else v + 2
        nv_s = str(int(nv)) if nv == int(nv) else f"{nv:g}"
        ln = lines[i][:m.start()] + nv_s + lines[i][m.end():]
        judge("number_changed", "\n".join(lines[:i] + [ln] + lines[i + 1:]))
    by_line = {}
    for i, m in nums:
        by_line.setdefault(i, []).append(m)
    cand = [(i, ms) for i, ms in by_line.items() if len({x.group(0) for x in ms}) >= 2]
    if cand:
        i, ms = r.choice(cand)
        a, c = r.sample([x for x in ms], 2)
        if a.group(0) != c.group(0):
            if a.start() > c.start():
                a, c = c, a
            l = lines[i]
            ln = l[:a.start()] + c.group(0) + l[a.end():c.start()] + a.group(0) + l[c.end():]
            judge("numbers_swapped", "\n".join(lines[:i] + [ln] + lines[i + 1:]))
    kpi = next(f for f in facts if f["id"] == "kpi")
    claim = "Nothing is late today." if kpi["late"] else f"{kpi['late'] + 2} orders are projected late."
    judge("false_count", text + "\n" + claim)
    ids = [m.group(0) for m in re.finditer(r"\b(SO|PO)\d{4}\b", text)]
    if ids:
        old = r.choice(ids)
        judge("unknown_id", text.replace(old, old[:2] + "9" + old[3:], 1))


# ====================================================================== report
def summarize(obs: list[dict], pol: list[dict], seeds: list[int], secs: float, mism: list[dict] | None = None) -> dict:
    R: dict = {"seeds": len(seeds), "days": DAYS, "runtime_s": round(secs, 1)}
    R["dataset"] = {"orders_scored": sum(o["orders"] for o in obs), "late_orders": sum(o["late"] for o in obs),
                    "base_late_rate_pct": round(100 * sum(o["late"] for o in obs) / sum(o["orders"] for o in obs), 2)}
    # E1
    det = {}
    for key in obs[0]["det"]:
        per = [o["det"][key][:3] for o in obs]
        tp, fp, fn = np.array(per).sum(axis=0)
        p, r, f = prf(tp, fp, fn)
        pci, rci, fci = cluster_prf(per)
        det[key] = {"tp": int(tp), "fp": int(fp), "fn": int(fn), "precision": p, "recall": r, "f1": f,
                    "precision_ci": pci, "recall_ci": rci, "f1_ci": fci}
    R["E1_detection"] = det
    leads = {}
    for name in ("ours", "fair_ablation", "ablation", "static"):
        allv = [x for o in obs for x in o["lead"][name]]
        leads[name] = {"median_days": float(np.median(allv)) if allv else None,
                       "mean_days": float(np.mean(allv)) if allv else None,
                       "pct_ge_3_days": 100 * float(np.mean([x >= 3 for x in allv])) if allv else None,
                       "mean_ci": boot_ci(allv)}
    leads["no_system"] = {"median_days": 0.0, "mean_days": 0.0, "pct_ge_3_days": 0.0,
                          "note": "without a projection the planner learns on the CRD itself"}
    R["E1_lead_time"] = leads
    on_time_total = sum(o["on_time_orders"] for o in obs)
    R["E1_false_alarm_ever_pct"] = {n: 100 * sum(o["ever_false"][n] for o in obs) / on_time_total
                                    for n in ("ours", "fair_ablation", "ablation", "static")}
    # E2
    feeds = [o["feed"] for o in obs if o["feed"]]
    lat = [f["latency_days"] for f in feeds if f["detected"]]
    R["E2_feed"] = {"outages": len(feeds), "detected": sum(f["detected"] for f in feeds),
                    "median_latency_hours": 24 * float(np.median(lat)) if lat else None,
                    "max_latency_hours": 24 * float(np.max(lat)) if lat else None,
                    "false_alert_episodes": sum(f["false_alert_episodes"] for f in feeds),
                    "timeout_hours": 24 * engine.FEED_TIMEOUT}
    st = sum(o["spike"]["truth"] for o in obs); stp = sum(o["spike"]["tp"] for o in obs)
    sfp = sum(o["spike"]["fp"] for o in obs)
    R["E2_spikes"] = {"spikes": st, "detected": stp, "false_alerts": sfp,
                      "recall": stp / st if st else None, "precision": stp / (stp + sfp) if stp + sfp else None}
    dl = sum(o["po"]["delayed"] for o in obs); wn = [x for o in obs for x in o["po"]["warn_lead"]]
    eo = [x for o in obs for x in o["po"]["eta_err_ours"]]; ep = [x for o in obs for x in o["po"]["eta_err_promised"]]
    sur_o = 100 * float(np.mean([x for o in obs for x in o["po"]["surprise_ours"]]))
    sur_p = 100 * float(np.mean([x for o in obs for x in o["po"]["surprise_promised"]]))
    wnot = [x for o in obs for x in o["po"]["warn_notice"]]
    wsil = [x for o in obs for x in o["po"]["warn_silent"]]
    R["E2_supplier_delays"] = {"delayed_pos": dl, "with_notice": sum(o["po"]["with_notice"] for o in obs),
                               "median_warning_notice": float(np.median(wnot)) if wnot else None,
                               "median_warning_silent": float(np.median(wsil)) if wsil else None,
                               "warned_1day_ahead": sum(1 for x in wn if x >= 1), "late_surprise_pct_ours": sur_o, "late_surprise_pct_promised": sur_p,
                               "eta_rmse_ours": float(np.sqrt(np.mean(np.square(eo)))),
                               "eta_rmse_promised": float(np.sqrt(np.mean(np.square(ep)))),"warned_before_arrival": sum(1 for x in wn if x > 0),
                               "median_warning_days": float(np.median(wn)) if wn else None,
                               "false_po_alerts": sum(o["po"]["false"] for o in obs),
                               "eta_mae_ours": float(np.mean(eo)), "eta_mae_promised": float(np.mean(ep)),
                               "eta_mae_ours_ci": boot_ci(eo), "eta_mae_promised_ci": boot_ci(ep), "eta_n": len(eo)}
    sh = [x for o in obs for x in o["shrink"]]
    R["E2_discrepancies"] = {"planted": len(sh), "detected": sum(1 for x in sh if x is not None),
                             "median_days_to_detect": float(np.median([x for x in sh if x is not None])) if sh else None,
                             "note": "detection is bounded by when the next cycle count happens"}
    # E3
    nc = {k: sum(o["numbers"][k] for o in obs) for k in ("snapshots", "materials_checked", "mismatches")}
    nc["examples"] = [e for o in obs for e in o["numbers"]["examples"]][:5]
    R["E3_numbers"] = nc
    R["E3_faulty_vs_clean"] = {"snapshots": sum(o["faulty_vs_clean"]["snapshots"] for o in obs),
                               "identical": sum(o["faulty_vs_clean"]["identical"] for o in obs)}
    R["E3_replay"] = {"replays": 3 * len(obs), "identical": sum(o["replay_ok"] for o in obs)}
    R["E3_feed_faults"] = {k: sum(o["feed_stats"][k] for o in obs) for k in ("junk", "dups", "quarantined", "dup_ignored")}
    # E4
    g = {k: sum(o["grounding"][k] for o in obs) for k in obs[0]["grounding"]}
    c = {k: sum(o["claims"][k] for o in obs) for k in obs[0]["claims"]}
    R["E4_agent"] = {**g, **c}
    R["E4_mutations"] = {k: {f: sum(o["mutations"][k][f] for o in obs) for f in ("n", "strict", "loose")}
                         for k in obs[0]["mutations"]}
    # E5
    pols = sorted({p["policy"] for p in pol})
    by = {pp: {p["seed"]: p for p in pol if p["policy"] == pp} for pp in pols}
    out = {}
    metrics = ("on_time_pct", "late_orders", "late_days_total", "stockout_material_days", "po_spend",
               "avg_stock_value", "proposals", "approved")
    for pp in pols:
        out[pp] = {m: float(np.mean([by[pp][s][m] for s in seeds])) for m in metrics}
    comp = {}
    for pp in pols:
        if pp == "baseline":
            continue
        comp[pp] = {}
        for m in metrics[:6]:
            diffs = [by[pp][s][m] - by["baseline"][s][m] for s in seeds]
            comp[pp][m] = {"mean_diff": float(np.mean(diffs)), "ci95": boot_ci(diffs),
                           "p_value": sign_flip_p(diffs),
                           "seeds_better": int(sum(1 for x in diffs if (x > 0 if m == "on_time_pct" else x < 0))),
                           "seeds_worse": int(sum(1 for x in diffs if (x < 0 if m == "on_time_pct" else x > 0)))}
    R["E5_policy_means"] = out
    R["E5_vs_baseline"] = comp
    R["E5_per_seed"] = {pp: [by[pp][s] for s in seeds] for pp in pols}
    R["E5_agent_vs"] = {}
    for ref in ("rule_expedite", "baseline_safety4"):
        if ref in by:
            R["E5_agent_vs"][ref] = {}
            for m in metrics[:6]:
                diffs = [by["agent"][s][m] - by[ref][s][m] for s in seeds]
                R["E5_agent_vs"][ref][m] = {"mean_diff": float(np.mean(diffs)), "ci95": boot_ci(diffs),
                                            "p_value": sign_flip_p(diffs),
                                            "seeds_better": int(sum(1 for x in diffs if (x > 0 if m == "on_time_pct" else x < 0))),
                                            "seeds_worse": int(sum(1 for x in diffs if (x < 0 if m == "on_time_pct" else x > 0)))}
    if mism:
        md = {}
        for H in HORIZONS:
            per = [m["det"][H] for m in mism]
            tp, fp, fn = np.array(per).sum(axis=0)
            p, r_, f = prf(tp, fp, fn)
            md[H] = {"precision": p, "recall": r_, "f1": f, "tp": int(tp), "fp": int(fp), "fn": int(fn)}
        R["E1b_rule_mismatch"] = {"late_orders": sum(m["late"] for m in mism),
                                  "orders": sum(m["orders"] for m in mism), "detection": md}
    return R


def fmt(x, d=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{d}f}"


def write_markdown(R: dict, path: Path):
    L = []
    a = L.append
    a("# Sentinel – evaluation results\n")
    a(f"{R['seeds']} seeded runs × {R['days']} days. Ground truth = the plant simulator. "
      f"Reproduce: `python -m evaluation.run_eval --seeds {R['seeds']}` (runtime {R['runtime_s']} s).\n")
    ds = R["dataset"]
    a(f"Orders scored: **{ds['orders_scored']}**, of which **{ds['late_orders']}** really missed their CRD "
      f"({ds['base_late_rate_pct']}%) under the company's existing reorder-point planner.\n")
    a("**How to read this report.** Some checks below are *evidence* (they could have failed and would tell you "
      "something about a real plant); others are *sanity checks* that pass largely because of how the simulator "
      "is built. Each section says which.\n")

    # ---------------------------------------------------------------- E1
    a("## E1 – Does Sentinel flag the orders that will miss their CRD, early enough to act?\n")
    a("Prediction made at the end of the day **H days before the CRD**; positive = projected late. "
      "95% CIs from a bootstrap over seeds. Production takes 2 days, so at H = 3 an order that has not started "
      "is already close to late: **H = 5, 7 and 10 are the informative horizons.**\n")
    names = {"ours": "**Sentinel** (time-phased + learned supplier delay)",
             "ours_any": "Sentinel, 'late' or 'at risk'",
             "fair_ablation": "Sentinel without delay learning (still handles overdue POs)",
             "ablation": "Trusts every promised date (no learning, no overdue handling)",
             "stock_only": "Naive: stock on hand only, ignores incoming POs",
             "not_started": "Naive: every order not yet in production",
             "static": "Static net availability < 0 (ERP-style total)"}
    a("| Detector | H | Precision | Recall | F1 | TP / FP / FN |")
    a("|---|---|---|---|---|---|")
    for H in HORIZONS:
        for n in ("ours", "fair_ablation", "ablation", "stock_only", "not_started", "ours_any"):
            d = R["E1_detection"][f"{n}@{H}"]
            a(f"| {names[n]} | {H} | {fmt(d['precision'],2)} [{fmt(d['precision_ci'][0],2)}–{fmt(d['precision_ci'][1],2)}] "
              f"| {fmt(d['recall'],2)} [{fmt(d['recall_ci'][0],2)}–{fmt(d['recall_ci'][1],2)}] "
              f"| {fmt(d['f1'],2)} | {d['tp']} / {d['fp']} / {d['fn']} |")
    st = R["E1_detection"]["static@5"]
    a(f"\nThe static ERP-style check (net availability < 0) flags {st['tp'] + st['fp']} order(s) in total: "
      "the existing planner keeps that total positive by design, so it is not a meaningful baseline here and is "
      "left out of the table.\n")
    a("**What this shows.** At 3 days, naive rules reach recall 1.0 too, so a high recall there proves little. "
      "Further ahead the difference is real: at 7 days 'every unstarted order' has precision "
      f"{fmt(R['E1_detection']['not_started@7']['precision'],2)}, Sentinel {fmt(R['E1_detection']['ours@7']['precision'],2)} "
      f"with recall {fmt(R['E1_detection']['ours@7']['recall'],2)}. Learning each supplier's delay is a trade-off: "
      f"without it precision is near 1.0 but recall falls (at 7 days {fmt(R['E1_detection']['fair_ablation@7']['recall'],2)} "
      f"vs {fmt(R['E1_detection']['ours@7']['recall'],2)}); at 3 days the version without learning has the better F1 "
      f"({fmt(R['E1_detection']['fair_ablation@3']['f1'],2)} vs {fmt(R['E1_detection']['ours@3']['f1'],2)}).\n")
    a("**Early warning** (truly late orders: days between the first 'late' flag and the CRD):\n")
    a("| Detector | Median days | Mean days [95% CI] | Flagged ≥ 3 days ahead |")
    a("|---|---|---|---|")
    for n, label in (("ours", "Sentinel"), ("fair_ablation", "Without delay learning"),
                     ("ablation", "Trusts promised dates"), ("no_system", "No system (learn at the CRD)")):
        l = R["E1_lead_time"][n]
        ci = l.get("mean_ci")
        a(f"| {label} | {fmt(l['median_days'])} | {fmt(l['mean_days'],2)}"
          f"{' [' + fmt(ci[0],2) + '–' + fmt(ci[1],2) + ']' if ci else ''} | {fmt(l['pct_ge_3_days'])}% |")
    fa = R["E1_false_alarm_ever_pct"]
    a(f"\nOn-time orders that were *ever* flagged late at some point: Sentinel {fmt(fa['ours'])}%, "
      f"without learning {fmt(fa['fair_ablation'])}%, trusting promised dates {fmt(fa['ablation'])}%.\n")
    mm = R.get("E1b_rule_mismatch")
    if mm:
        a("**E1b – What if the shop floor does not follow the documented rule?** Sentinel assumes earliest-CRD-first; "
          "here the simulated plant builds oldest order first instead "
          f"({mm['late_orders']} of {mm['orders']} orders late).\n")
        a("| H | Precision | Recall | F1 |")
        a("|---|---|---|---|")
        for H in HORIZONS:
            d = mm["detection"][str(H)] if str(H) in mm["detection"] else mm["detection"][H]
            a(f"| {H} | {fmt(d['precision'],2)} | {fmt(d['recall'],2)} | {fmt(d['f1'],2)} |")
        a("")

    # ---------------------------------------------------------------- E2
    a("## E2 – Alerts\n")
    f = R["E2_feed"]
    a(f"- **Feed outages** (scored from Sentinel's own feed alert, sampled every 6 h): {f['detected']}/{f['outages']} "
      f"detected; median {fmt(f['median_latency_hours'])} h after the feed went quiet (the silence timeout is "
      f"{fmt(f['timeout_hours'])} h, so that is the floor). False feed alerts: {f['false_alert_episodes']}. "
      "*Sanity check:* any working timeout rule finds a 18-48 h outage.")
    s = R["E2_spikes"]
    a(f"- **Demand spikes:** {s['detected']}/{s['spikes']} detected on the day; {s['false_alerts']} false alerts "
      f"(precision {fmt(s['precision'],2)}).")
    sd = R["E2_supplier_delays"]
    a(f"- **Supplier delays (1-7 days):** {sd['warned_before_arrival']}/{sd['delayed_pos']} late POs flagged before the "
      f"goods arrived, {sd['warned_1day_ahead']} at least a day before. When the supplier warned "
      f"({sd['with_notice']} POs) the median warning was {fmt(sd['median_warning_notice'])} days; when it did not, "
      f"{fmt(sd['median_warning_silent'])} days, which is simply 'the PO is half a day overdue'. "
      f"{sd['false_po_alerts']} alerts on POs that were not late. *Mostly a sanity check:* an overdue PO is late by "
      "definition.")
    a(f"- **Arrival forecasts:** mean absolute error of a PO's arrival day, one day after it is placed: "
      f"Sentinel {fmt(sd['eta_mae_ours'],2)} days vs supplier's promised date {fmt(sd['eta_mae_promised'],2)} days "
      f"(n = {sd['eta_n']}); root-mean-square error {fmt(sd['eta_rmse_ours'],2)} vs {fmt(sd['eta_rmse_promised'],2)}. "
      f"Goods arrived *after* the forecast for {fmt(sd['late_surprise_pct_ours'])}% of POs vs "
      f"{fmt(sd['late_surprise_pct_promised'])}% with the promised date. The adjustment is deliberately pessimistic: "
      "fewer late surprises, but a *higher* absolute error, because most POs arrive on time.")
    di = R["E2_discrepancies"]
    a(f"- **Hidden stock loss:** {di['detected']}/{di['planted']} found within the run, median "
      f"{fmt(di['median_days_to_detect'])} days after it happened. The plant counts two materials a day, chosen without "
      "knowing where stock went missing, so detection waits for the next routine count.\n")

    # ---------------------------------------------------------------- E3
    a("## E3 – Are the numbers right?\n")
    n = R["E3_numbers"]
    a(f"- On hand, incoming and allocated (and net, their sum) checked against a **separately written reference "
      f"calculator** (same team; it re-derives the book from the clean events without sharing Sentinel's code) on "
      f"{n['snapshots']} daily snapshots × {n['materials_checked'] // max(n['snapshots'], 1)} materials: "
      f"**{n['mismatches']} mismatches**. It checks bookkeeping, not the allocation order or ship dates; E1b tests "
      "those against a plant that uses a different rule.")
    fv = R["E3_faulty_vs_clean"]
    ff = R["E3_feed_faults"]
    a(f"- Faulty feed ({ff['dups']} duplicates, {ff['junk']} junk events, shuffled arrivals) vs the same events delivered "
      f"cleanly: **{fv['identical']}/{fv['snapshots']}** daily snapshots identical. "
      f"{ff['quarantined']} events quarantined, {ff['dup_ignored']} duplicates ignored.")
    rp = R["E3_replay"]
    a(f"- Full replay in random order with extra duplicates: **{rp['identical']}/{rp['replays']}** identical final state.\n")

    # ---------------------------------------------------------------- E4
    a("## E4 – Can the agent's words be trusted?\n")
    g = R["E4_agent"]
    a(f"- {g['briefings']} role briefings: **{g['grounded']}** pass the grounding check. *Sanity check only:* the "
      "template text is built from the same facts it is checked against.")
    a(f"- Material claims (on hand / incoming / allocated / net) vs the reference calculator: "
      f"**{g['material_ok']}/{g['material_claims']}** correct. Order CRD claims vs ground truth: "
      f"**{g['order_ok']}/{g['order_claims']}**.")
    a("- **How strong is the check?** (the real question, since its job is to stop a language model's mistakes). "
      "One error was planted in a third of the briefings; the table shows how often each check rejected it. The "
      "strict check is the one Sentinel uses: a number must belong to the order, PO or material its sentence "
      "names, IDs must exist, and counts ('3 orders late', 'nothing is late') must match.\n")
    a("| Planted error | n | Strict check rejects | Earlier loose check rejected |")
    a("|---|---|---|---|")
    lab = {"number_changed": "A number changed by 1-3", "numbers_swapped": "Two numbers in a sentence swapped",
           "false_count": "A false count ('nothing is late')", "unknown_id": "An order or PO ID that does not exist"}
    for k, v in R["E4_mutations"].items():
        a(f"| {lab[k]} | {v['n']} | **{100 * v['strict'] / max(v['n'], 1):.0f}%** | {100 * v['loose'] / max(v['n'], 1):.0f}% |")
    a("\nA changed number slips through when the new value happens to be another true fact about the same thing; "
      "swapped numbers mostly slip through because both values are true facts of that entity. The optional Claude "
      "rewrite (`SENTINEL_LLM=1`) is shown only when it passes this check; it was **not** run in this evaluation "
      "(no API calls), so how often it would be rejected is not measured.\n")

    # ---------------------------------------------------------------- E5
    a("## E5 – Do planners do better with the agent?\n")
    a("Same seeds, same faults, **identical demand** in every arm (supplier lateness has its own random stream, so "
      "placing a PO never changes the orders that follow). Paired by seed; 95% bootstrap CI of the mean "
      "difference; p from a sign-flip permutation test.\n")
    a("| Policy | On-time % | Late orders | Days late (total) | Stock-out material-days | PO spend | Avg stock value | Proposals (approved) |")
    a("|---|---|---|---|---|---|---|---|")
    labels = {"baseline": "Baseline: existing reorder-point planner",
              "baseline_safety4": "Baseline + 4 days of safety stock (instead of 1)",
              "rule_expedite": "Baseline + rule: rush-reorder any PO that is overdue or slips",
              "agent": "**Sentinel agent** (all proposals approved)",
              "agent_half_approved": "Sentinel agent, planner approves ~50% at random",
              "agent_no_expedite": "Sentinel agent, no expediting",
              "agent_no_learning": "Sentinel agent, no supplier-delay learning"}
    order = ["baseline", "baseline_safety4", "rule_expedite", "agent", "agent_half_approved", "agent_no_expedite",
             "agent_no_learning"]
    for pp in order:
        m = R["E5_policy_means"].get(pp)
        if m:
            a(f"| {labels[pp]} | {fmt(m['on_time_pct'],2)} | {fmt(m['late_orders'])} | {fmt(m['late_days_total'])} | "
              f"{fmt(m['stockout_material_days'])} | {fmt(m['po_spend'],0)} | {fmt(m['avg_stock_value'],0)} | "
              f"{fmt(m['proposals'])} ({fmt(m['approved'])}) |")
    a("\n| Comparison | Metric | Mean difference [95% CI] | p | Seeds better / worse |")
    a("|---|---|---|---|---|")
    rows = [("Agent vs baseline", R["E5_vs_baseline"]["agent"])]
    for ref, nm in (("rule_expedite", "Agent vs rush-reorder rule"), ("baseline_safety4", "Agent vs extra safety stock")):
        if ref in R.get("E5_agent_vs", {}):
            rows.append((nm, R["E5_agent_vs"][ref]))
    for nm, cm in rows:
        for m in ("on_time_pct", "late_days_total", "stockout_material_days", "po_spend", "avg_stock_value"):
            c = cm[m]
            a(f"| {nm} | {m} | {c['mean_diff']:+.2f} [{c['ci95'][0]:+.2f}, {c['ci95'][1]:+.2f}] | "
              f"{c['p_value']:.4f} | {c['seeds_better']} / {c['seeds_worse']} |")
    ag, b0 = R["E5_policy_means"]["agent"], R["E5_policy_means"]["baseline"]
    re_, s4 = R["E5_policy_means"].get("rule_expedite"), R["E5_policy_means"].get("baseline_safety4")
    a(f"\n**What this shows.** The agent lifts on-time delivery from {fmt(b0['on_time_pct'],1)}% to "
      f"{fmt(ag['on_time_pct'],1)}% at {100 * (ag['po_spend'] / b0['po_spend'] - 1):+.1f}% purchase spend. "
      + (f"Two simple rules a planner could use without Sentinel reach a similar on-time rate "
         f"({fmt(re_['on_time_pct'],1)}% and {fmt(s4['on_time_pct'],1)}%), but the rush-reorder rule spends "
         f"{100 * (re_['po_spend'] / ag['po_spend'] - 1):+.0f}% more than the agent and holds "
         f"{100 * (re_['avg_stock_value'] / ag['avg_stock_value'] - 1):+.0f}% more stock, and extra safety stock "
         f"spends {100 * (s4['po_spend'] / ag['po_spend'] - 1):+.1f}% more and holds "
         f"{100 * (s4['avg_stock_value'] / ag['avg_stock_value'] - 1):+.1f}% more stock. Sentinel's value is "
         "reaching that on-time rate with targeted purchases, not a higher ceiling." if re_ and s4 else "") + "\n")

    a("## E6 – Supplier messages in plain language (linked Problem 6)\n")
    a("Reported separately in [AMBIGUITY.md](AMBIGUITY.md) (`python -m evaluation.run_ambiguity --seeds 30`). "
      "E1-E5 above use structured supplier dates.\n")
    a("## Limitations (read before trusting the numbers)\n")
    a("- The plant is a simulator we wrote. It encodes our assumptions about suppliers, demand and stock loss; "
      "results transfer to a real plant only as far as those assumptions hold.")
    a("- In E1-E5 the plant follows Sentinel's documented allocation rule; E1b shows what happens when it does not.")
    a("- The 'human' approving proposals is simulated (approve all, or ~50 % at random). No real planner was tested.")
    a("- All of the agent's gain comes from expediting slow supply early ('no expediting' equals the baseline).")
    a("- About one 'late' flag in four is a false alarm, the price of assuming each supplier will be as late as it "
      "has been; without that learning, flags are almost always right but fewer late orders are caught early.")
    a("- Ship dates are whole days; production time is fixed at 2 days.")
    path.write_text("\n".join(L), encoding="utf-8")


def charts(R: dict):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    OUT.mkdir(exist_ok=True)
    # detection
    fig, ax = plt.subplots(figsize=(7, 3.6))
    names = [("not_started", "Every unstarted\norder"), ("ablation", "Trusts promised\ndates"),
             ("fair_ablation", "No delay\nlearning"), ("ours", "Sentinel")]
    x = np.arange(len(names))
    for i, (metric, col) in enumerate((("precision", "#7a8aa6"), ("recall", "#2e9e6a"), ("f1", "#1f4e79"))):
        vals = [R["E1_detection"][f"{n}@7"][metric] for n, _ in names]
        ax.bar(x + (i - 1) * 0.26, vals, 0.24, label=metric.capitalize(), color=col)
    ax.set_xticks(x, [l for _, l in names]); ax.set_ylim(0, 1.05)
    ax.set_title("Late-order detection, 7 days before CRD"); ax.legend(frameon=False, ncol=3, loc="upper left")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT / "detection.png", dpi=150); plt.close(fig)
    # outcomes
    fig, ax = plt.subplots(figsize=(7, 3.6))
    pols = [p for p in ("baseline", "baseline_safety4", "rule_expedite", "agent", "agent_half_approved")
            if p in R["E5_policy_means"]]
    vals = [R["E5_policy_means"][p]["on_time_pct"] for p in pols]
    lab = {"baseline": "Baseline", "baseline_safety4": "+ safety\nstock", "rule_expedite": "+ rush\nrule",
           "agent": "Sentinel\nagent", "agent_half_approved": "Agent\n(50% approved)"}
    ax.bar(range(len(pols)), vals, color=["#2e9e6a" if p.startswith("agent") else "#7a8aa6" for p in pols])
    lo = min(vals) - 3
    ax.set_ylim(max(0, lo), 100.5); ax.set_xticks(range(len(pols)), [lab.get(p, p) for p in pols])
    for i, v in enumerate(vals):
        ax.text(i, v + 0.15, f"{v:.1f}%", ha="center", fontsize=9)
    ax.set_title("Orders shipped by their CRD (mean over seeds)"); ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT / "outcomes.png", dpi=150); plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--first-seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    a = ap.parse_args()
    seeds = list(range(a.first_seed, a.first_seed + a.seeds))
    t0 = time.time()
    policies = ["baseline", "baseline_safety4", "rule_expedite", "agent", "agent_half_approved",
                "agent_no_expedite", "agent_no_learning"]
    with ProcessPoolExecutor(a.workers) as ex:
        obs = list(ex.map(run_observed, seeds))
        pol = list(ex.map(run_policy, [(s, p) for s in seeds for p in policies]))
        mism = list(ex.map(run_mismatch, seeds))
    R = summarize(obs, pol, seeds, time.time() - t0, mism)
    OUT.mkdir(exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(R, indent=1, default=float), encoding="utf-8")
    write_markdown(R, OUT / "RESULTS.md")
    charts(R)
    sys.stdout.reconfigure(encoding="utf-8")
    print((OUT / "RESULTS.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
