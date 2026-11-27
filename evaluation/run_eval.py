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
HORIZONS = (3, 5)
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
        w.rop_planner(d)
        t = d + 0.95
        w.deliver_until(t)
        snap = engine.snapshot(w.store, t)
        st = snap["_state"]
        abl = engine.project(st, w.cat, ablate)
        static = engine.static_flags(st, w.cat)
        abl_by = {o["id"]: o for o in abl["orders"]}
        flags[d] = {o["id"]: {"ours": o["risk"] == "late", "ours_any": o["risk"] != "ok",
                              "ablation": abl_by[o["id"]]["risk"] == "late",
                              "static": static.get(o["id"], False)}
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
        for det_name in ("ours", "ours_any", "ablation", "static"):
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
    lead = {"ours": [], "ablation": [], "static": []}
    for oid, o in eval_orders.items():
        if not is_late[oid]:
            continue
        for name in lead:
            first = next((dd for dd in range(DAYS) if dd < o["crd"] and flags.get(dd, {}).get(oid, {}).get(name)), None)
            lead[name].append(o["crd"] - first if first is not None else 0)
    ever_false = {name: sum(1 for oid, o in eval_orders.items() if not is_late[oid] and any(
        flags.get(dd, {}).get(oid, {}).get(name) for dd in range(DAYS))) for name in ("ours", "ablation", "static")}

    # ------------------------------------------------ E2 scoring
    # feed outage: fine time grid over arrivals
    feed = {}
    for o in w.truth.outages:
        src = o["source"]
        arr = sorted((w.store.arrival[e["event_id"]], e["ts"]) for e in w.store.events.values() if e["source"] == src)
        grid = np.arange(0.5, DAYS, 0.01)
        last, j, first_alert, false_eps, active_prev = -1.0, 0, None, 0, False
        for t in grid:
            while j < len(arr) and arr[j][0] <= t:
                last = max(last, arr[j][1]); j += 1
            active = last >= 0 and t - last > engine.FEED_TIMEOUT
            if active and o["start"] <= t <= o["end"] + 0.1 and first_alert is None:
                first_alert = t
            if active and not active_prev and not (o["start"] <= t <= o["end"] + 0.1):
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
               "false": po_false, "eta_err_ours": eta_err["ours"], "eta_err_promised": eta_err["promised"],
               "surprise_ours": eta_err["surprise_ours"], "surprise_promised": eta_err["surprise_promised"]},
        "shrink": shrink_found,
        "numbers": numbers_check, "faulty_vs_clean": faulty_vs_clean, "replay_ok": replay_ok,
        "grounding": grounding, "claims": claim_checks,
        "feed_stats": {"junk": w.truth.junk, "dups": w.truth.dups,
                       "quarantined": w.store.stats["quarantined"], "dup_ignored": w.store.stats["duplicates"]},
    }


# ====================================================================== E5: outcomes
def run_policy(args) -> dict:
    seed, policy = args
    w = World(seed=seed, days=DAYS)
    n_props = n_appr = 0
    for d in range(DAYS):
        w.simulate_day(d)
        w.rop_planner(d)
        if policy == "agent":
            ps = agent_planner(w, d)
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


# ====================================================================== report
def summarize(obs: list[dict], pol: list[dict], seeds: list[int], secs: float) -> dict:
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
    for name in ("ours", "ablation", "static"):
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
                                    for n in ("ours", "ablation", "static")}
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
    R["E2_supplier_delays"] = {"delayed_pos": dl, "late_surprise_pct_ours": sur_o, "late_surprise_pct_promised": sur_p,
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
    return R


def fmt(x, d=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{d}f}"


def write_markdown(R: dict, path: Path):
    L = []
    a = L.append
    a(f"# Sentinel – evaluation results\n")
    a(f"{R['seeds']} seeded runs × {R['days']} days. Ground truth = the plant simulator. "
      f"Runtime {R['runtime_s']} s. Reproduce: `python -m evaluation.run_eval --seeds {R['seeds']}`.\n")
    ds = R["dataset"]
    a(f"Orders scored: **{ds['orders_scored']}**, of which **{ds['late_orders']}** really missed their CRD "
      f"({ds['base_late_rate_pct']}%) under the company's existing reorder-point planner.\n")
    a("## E1 – Does Sentinel flag the orders that will miss their CRD?\n")
    a("Prediction made at the end of the day **H days before the CRD**; positive = projected late. "
      "95% CIs from a bootstrap over seeds.\n")
    a("| Detector | H | Precision | Recall | F1 | TP / FP / FN |")
    a("|---|---|---|---|---|---|")
    names = {"ours": "Sentinel (time-phased + learned supplier delay)",
             "ours_any": "Sentinel, 'late' or 'at risk'",
             "ablation": "Ablation: time-phased, trusts supplier dates",
             "static": "Baseline: static net availability < 0 (ERP screen)"}
    for H in HORIZONS:
        for n in ("ours", "ours_any", "ablation", "static"):
            d = R["E1_detection"][f"{n}@{H}"]
            a(f"| {names[n]} | {H} | {fmt(d['precision'],2)} [{fmt(d['precision_ci'][0],2)}–{fmt(d['precision_ci'][1],2)}] "
              f"| {fmt(d['recall'],2)} [{fmt(d['recall_ci'][0],2)}–{fmt(d['recall_ci'][1],2)}] "
              f"| {fmt(d['f1'],2)} [{fmt(d['f1_ci'][0],2)}–{fmt(d['f1_ci'][1],2)}] | {d['tp']} / {d['fp']} / {d['fn']} |")
    a("\n**Early warning** (truly late orders: days between the first 'late' flag and the CRD):\n")
    a("| Detector | Median days | Mean days [95% CI] | Flagged ≥ 3 days ahead |")
    a("|---|---|---|---|")
    for n, label in (("ours", "Sentinel"), ("ablation", "Ablation (trusts supplier dates)"),
                     ("static", "Static net availability"), ("no_system", "No system (learn at CRD)")):
        l = R["E1_lead_time"][n]
        ci = l.get("mean_ci")
        a(f"| {label} | {fmt(l['median_days'])} | {fmt(l['mean_days'],2)}"
          f"{' [' + fmt(ci[0],2) + '–' + fmt(ci[1],2) + ']' if ci else ''} | {fmt(l['pct_ge_3_days'])}% |")
    fa = R["E1_false_alarm_ever_pct"]
    a("\nThe static ERP check almost never fires in these runs because the existing planner keeps net availability "
      "(on hand + incoming - allocated) positive: the totals look fine while the *timing* fails. That gap is "
      "what the time-phased projection closes.")
    a(f"\nOn-time orders that were *ever* flagged late at some point: Sentinel {fmt(fa['ours'])}%, "
      f"ablation {fmt(fa['ablation'])}%, static {fmt(fa['static'])}%.\n")
    a("## E2 – Alerts\n")
    f = R["E2_feed"]
    a(f"- **Feed outages:** {f['detected']}/{f['outages']} detected; median latency {fmt(f['median_latency_hours'])} h "
      f"(max {fmt(f['max_latency_hours'])} h; the silence timeout is {fmt(f['timeout_hours'])} h, so this is the floor). "
      f"False feed alerts: {f['false_alert_episodes']}.")
    s = R["E2_spikes"]
    a(f"- **Demand spikes:** {s['detected']}/{s['spikes']} detected on the day; {s['false_alerts']} false alerts "
      f"(precision {fmt(s['precision'],2)}).")
    sd = R["E2_supplier_delays"]
    a(f"- **Supplier delays:** {sd['warned_before_arrival']}/{sd['delayed_pos']} late POs flagged before the goods "
      f"arrived (median {fmt(sd['median_warning_days'])} days ahead); {sd['false_po_alerts']} alerts on POs that were not late.")
    a(f"- **Arrival forecasts:** mean absolute error of a PO's arrival day, one day after it is placed: "
      f"Sentinel {fmt(sd['eta_mae_ours'],2)} days [{fmt(sd['eta_mae_ours_ci'][0],2)}–{fmt(sd['eta_mae_ours_ci'][1],2)}] vs "
      f"supplier's promised date {fmt(sd['eta_mae_promised'],2)} days "
      f"[{fmt(sd['eta_mae_promised_ci'][0],2)}–{fmt(sd['eta_mae_promised_ci'][1],2)}] (n = {sd['eta_n']} POs); "
      f"root-mean-square error {fmt(sd['eta_rmse_ours'],2)} vs {fmt(sd['eta_rmse_promised'],2)} days. "
      f"Goods arrived *after* the forecast day for {fmt(sd['late_surprise_pct_ours'])}% of POs with Sentinel's "
      f"risk-adjusted date vs {fmt(sd['late_surprise_pct_promised'])}% with the promised date. "
      f"(The adjustment is deliberately pessimistic: it adds the supplier's average delay to every PO. That cuts "
      f"late surprises and squared error but raises absolute error, because most POs arrive on time.)")
    di = R["E2_discrepancies"]
    a(f"- **Hidden stock loss:** {di['detected']}/{di['planted']} found, median {fmt(di['median_days_to_detect'])} days "
      f"after it happened ({di['note']}).\n")
    a("## E3 – Are the numbers right?\n")
    n = R["E3_numbers"]
    a(f"- On hand, incoming, allocated and net checked against an **independently written reference calculator** "
      f"on {n['snapshots']} daily snapshots ({n['materials_checked']} material-days × 4 fields): "
      f"**{n['mismatches']} mismatches**.")
    fv = R["E3_faulty_vs_clean"]
    ff = R["E3_feed_faults"]
    a(f"- Faulty feed ({ff['dups']} duplicates, {ff['junk']} junk events, shuffled arrivals) vs the same events delivered "
      f"cleanly: **{fv['identical']}/{fv['snapshots']}** daily snapshots identical. "
      f"{ff['quarantined']} events quarantined, {ff['dup_ignored']} duplicates ignored.")
    rp = R["E3_replay"]
    a(f"- Full replay in random order with extra duplicates: **{rp['identical']}/{rp['replays']}** identical final state.\n")
    a("## E4 – Can the agent be trusted?\n")
    g = R["E4_agent"]
    a(f"- {g['briefings']} role briefings generated; **{g['grounded']}** passed the grounding check "
      f"({g['ungrounded_numbers']} ungrounded numbers out of {g['numbers']}).")
    a(f"- Material claims (on hand / incoming / allocated / net) checked against the reference: "
      f"**{g['material_ok']}/{g['material_claims']}** correct. Order CRD claims vs ground truth: "
      f"**{g['order_ok']}/{g['order_claims']}** correct.")
    a("- Every purchase proposal waits for a human; in E5 the 'human' is simulated.\n")
    a("## E5 – Do planners do better with the agent?\n")
    a("Same seeds, same plant, same faults. *Baseline* = the existing reorder-point planner. "
      "*Agent* = baseline + Sentinel's proposals approved by the planner. Paired by seed; "
      "95% bootstrap CI of the mean difference; p from a sign-flip permutation test.\n")
    a("| Policy | On-time % | Late orders | Days late (total) | Stock-out material-days | PO spend | Avg stock value | Proposals (approved) |")
    a("|---|---|---|---|---|---|---|---|")
    labels = {"baseline": "Baseline", "agent": "Agent (all approved)", "agent_half_approved": "Agent (planner approves ~50%)",
              "agent_no_expedite": "Agent, no expediting", "agent_no_learning": "Agent, no supplier-delay learning"}
    for pp, m in R["E5_policy_means"].items():
        a(f"| {labels.get(pp, pp)} | {fmt(m['on_time_pct'],2)} | {fmt(m['late_orders'])} | {fmt(m['late_days_total'])} | "
          f"{fmt(m['stockout_material_days'])} | {fmt(m['po_spend'],0)} | {fmt(m['avg_stock_value'],0)} | "
          f"{fmt(m['proposals'])} ({fmt(m['approved'])}) |")
    a("\n| Policy vs baseline | Metric | Mean difference [95% CI] | p | Seeds better / worse |")
    a("|---|---|---|---|---|")
    for pp, cm in R["E5_vs_baseline"].items():
        for m in ("on_time_pct", "late_days_total", "stockout_material_days", "po_spend", "avg_stock_value"):
            c = cm[m]
            a(f"| {labels.get(pp, pp)} | {m} | {c['mean_diff']:+.2f} [{c['ci95'][0]:+.2f}, {c['ci95'][1]:+.2f}] | "
              f"{c['p_value']:.4f} | {c['seeds_better']} / {c['seeds_worse']} |")
    a("\n## Limitations (read before trusting the numbers)\n")
    a("- The plant is a simulator we wrote. It encodes our assumptions about suppliers, demand and stock loss; "
      "results transfer to a real plant only as far as those assumptions hold.")
    a("- Sentinel's allocation rule is the same rule the simulated plant uses. In a real plant, a mismatch between "
      "the documented rule and shop-floor practice would reduce accuracy.")
    a("- The 'human' approving proposals is simulated (approve all, or ~50 % at random). No real planner was tested.")
    a("- Supplier-delay learning needs history: early in a run, or for a new supplier, it falls back to a 1-day prior.")
    a("- Ship dates are whole days; production time is fixed at 2 days.")
    a("- 'Agent, no expediting' is identical to the baseline: the existing planner already covers every outright "
      "shortage, so all of the agent's gain comes from spotting slow supply early and expediting it. A plant "
      "with no expedite option would see no outcome gain (the early warnings in E1 would still apply).")
    a("- Precision of the 'late' flag is 0.67-0.75: about one flag in four is a false alarm, the price of "
      "assuming each supplier will be as late as it has been. Planners can filter by severity.")
    path.write_text("\n".join(L), encoding="utf-8")


def charts(R: dict):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    OUT.mkdir(exist_ok=True)
    # detection
    fig, ax = plt.subplots(figsize=(7, 3.6))
    names = [("static", "Static net\navailability"), ("ablation", "Time-phased,\ntrusts suppliers"), ("ours", "Sentinel")]
    x = np.arange(len(names))
    for i, (metric, col) in enumerate((("precision", "#7a8aa6"), ("recall", "#2e9e6a"), ("f1", "#1f4e79"))):
        vals = [R["E1_detection"][f"{n}@5"][metric] for n, _ in names]
        ax.bar(x + (i - 1) * 0.26, vals, 0.24, label=metric.capitalize(), color=col)
    ax.set_xticks(x, [l for _, l in names]); ax.set_ylim(0, 1.05)
    ax.set_title("Late-order detection, 5 days before CRD"); ax.legend(frameon=False, ncol=3, loc="upper left")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT / "detection.png", dpi=150); plt.close(fig)
    # outcomes
    fig, ax = plt.subplots(figsize=(7, 3.6))
    pols = list(R["E5_policy_means"].keys())
    pols = ["baseline"] + [p for p in pols if p != "baseline"]
    vals = [R["E5_policy_means"][p]["on_time_pct"] for p in pols]
    lab = {"baseline": "Baseline", "agent": "Agent", "agent_half_approved": "Agent\n(50% approved)",
           "agent_no_expedite": "Agent\nno expedite", "agent_no_learning": "Agent\nno learning"}
    ax.bar(range(len(pols)), vals, color=["#7a8aa6"] + ["#2e9e6a"] * (len(pols) - 1))
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
    policies = ["baseline", "agent", "agent_half_approved", "agent_no_expedite", "agent_no_learning"]
    with ProcessPoolExecutor(a.workers) as ex:
        obs = list(ex.map(run_observed, seeds))
        pol = list(ex.map(run_policy, [(s, p) for s in seeds for p in policies]))
    R = summarize(obs, pol, seeds, time.time() - t0)
    OUT.mkdir(exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(R, indent=1, default=float), encoding="utf-8")
    write_markdown(R, OUT / "RESULTS.md")
    charts(R)
    sys.stdout.reconfigure(encoding="utf-8")
    print((OUT / "RESULTS.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
