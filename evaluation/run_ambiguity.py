"""E6 - Supplier messages in plain language (Problem 6 inside Problem 2).

    python -m evaluation.run_ambiguity --seeds 30

Three questions:
  A. Text: does Sentinel tell clear, ambiguous and no-date messages apart, and is
     its range right? Scored on hand-labelled sets (development set, and blind
     sets written by authors who never saw the code; first runs are frozen).
  B. Stream: the same, on every message the simulated suppliers write (30 plants).
  C. Outcomes: the same plants, the same faults, every delay notice written as
     text. Compare ways of handling it: ignore the text / guess one date
     silently / plan for the latest reading / plan for the latest and ask.
     Reference: suppliers send exact structured dates (the ideal).
Writes results/ambiguity.json and results/AMBIGUITY.md.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sentinel import engine  # noqa: E402
from sentinel.ambiguity import interpret  # noqa: E402
from sentinel.world import Faults, World, agent_planner  # noqa: E402
from evaluation.heldout_check import score  # noqa: E402
from evaluation.run_eval import boot_ci, sign_flip_p  # noqa: E402

DAYS = 60
OUT = Path(__file__).resolve().parents[1] / "results"
EV = Path(__file__).resolve().parent
POLICIES = {
    "structured": ("worst", False, 0.0),     # suppliers send exact dates (reference)
    "ignore": ("ignore", False, 1.0),       # free text not read at all
    "guess": ("guess", False, 1.0),         # earliest reading, never asks
    "worst": ("worst", False, 1.0),         # latest reading, no questions
    "worst_ask": ("worst", True, 1.0),      # latest reading + approved question (Sentinel)
}


# ---------------------------------------------------------------- A. text sets
def text_sets() -> dict:
    out = {}
    sets = [("development", "ambiguity_heldout.json", None),
            ("blind_1", "ambiguity_blind.json", "ambiguity_blind_first_run.json"),
            ("blind_2", "ambiguity_blind2.json", "ambiguity_blind2_first_run.json")]
    for name, fn, first in sets:
        p = EV / fn
        if not p.exists():
            continue
        R = score(json.loads(p.read_text(encoding="utf-8"))["items"])
        entry = {"now": {k: v for k, v in R.items() if k != "failures"},
                 "failures_now": [f"{x['text']} -> {x['status']}" for x in R["failures"]]}
        if first:
            fp = OUT / first
            if not fp.exists():                       # freeze the very first score
                fp.write_text(json.dumps(R, indent=1, default=str), encoding="utf-8")
            F = json.loads(fp.read_text(encoding="utf-8"))
            entry["first_run"] = {k: v for k, v in F.items() if k != "failures"}
            entry["first_run_failures"] = [f"{x['text']} -> {x['status']}" for x in F["failures"]]
        out[name] = entry
    return out


# ---------------------------------------------------------------- B + C. plants
def run(args) -> dict:
    seed, policy = args
    text_policy, ask, share = POLICIES[policy]
    w = World(seed=seed, days=DAYS, faults=Faults(supplier_text=share, confirmations=0.2 if share else 0.0))
    opt = engine.EngineOptions(text_policy=text_policy)
    surprise = []      # per (PO, day after its delay notice): planned day < true arrival
    abs_err = []
    for d in range(DAYS):
        w.simulate_day(d)
        w.rop_planner(d)
        agent_planner(w, d, opt=opt, ask=ask)
        t = d + 0.95
        st = engine.rebuild(w.store, t, text_policy)
        for pid, p in st.pos.items():
            tp = w.truth.pos.get(pid)
            if p["status"] != "open" or not tp or tp["notified_at"] is None or tp["notified_at"] > t:
                continue
            surprise.append(p["expected"] < tp["actual"])
            abs_err.append(abs(p["expected"] - tp["actual"]))
    ev = [o for o in w.truth.orders.values() if o["crd"] <= DAYS - 1]
    late = [o for o in ev if o["ship_day"] is None or o["ship_day"] > o["crd"]]
    days_late = [(o["ship_day"] if o["ship_day"] is not None else DAYS) - o["crd"] for o in late]
    msgs = []
    if policy == "worst_ask":
        for m in w.truth.messages:
            r = interpret(m["text"], m["ts"], m["due"])
            L = m["label"]
            msgs.append({"label": L["label"], "type": L.get("type"), "kind": m["kind"], "status": r["status"],
                         "lo": r["lo"], "hi": r["hi"], "llo": L["lo"], "lhi": L["hi"], "meant": L["meant"],
                         "no_change": r["no_change"], "due": m["due"]})
    return {"seed": seed, "policy": policy, "orders": len(ev), "on_time_pct": 100 * (1 - len(late) / len(ev)),
            "late_orders": len(late), "late_days_total": sum(days_late),
            "stockout_material_days": w.truth.stockout_material_days, "po_spend": w.truth.po_spend,
            "po_days_planned_too_early": float(np.mean(surprise)) if surprise else 0.0,
            "po_abs_err": float(np.mean(abs_err)) if abs_err else 0.0, "po_days": len(surprise),
            "notices": sum(1 for m in w.truth.messages if m["kind"] == "notice"),
            "questions": len(w.truth.questions),
            "answer_hours": float(np.mean([24 * (q["answered_at"] - q["asked"]) for q in w.truth.questions]))
            if w.truth.questions else None,
            "messages": msgs}


def stream_scores(rows: list[dict]) -> dict:
    ms = [m for r in rows for m in r["messages"]]
    amb = [m for m in ms if m["label"] == "ambiguous"]
    clear = [m for m in ms if m["label"] == "clear"]
    tp = sum(m["status"] == "ambiguous" for m in amb)
    fp = sum(m["status"] == "ambiguous" for m in clear)
    clear_ok = sum(m["status"] == "clear" and (m["lo"] == m["meant"]) for m in clear)
    cover = sum(m["status"] == "ambiguous" and m["lo"] <= m["meant"] <= m["hi"] for m in amb)
    exact = sum(m["status"] == "ambiguous" and m["lo"] == m["llo"] and m["hi"] == m["lhi"] for m in amb)
    by_type = {}
    for m in amb:
        by_type.setdefault(m["type"], [0, 0])
        by_type[m["type"]][0] += 1
        by_type[m["type"]][1] += m["status"] == "ambiguous" and m["lo"] <= m["meant"] <= m["hi"]
    return {"messages": len(ms), "ambiguous": len(amb), "clear": len(clear), "detected": tp, "asked_when_clear": fp,
            "clear_read_right": clear_ok, "range_covers_truth": cover, "range_exact": exact,
            "by_type": {k: {"n": v[0], "right": v[1]} for k, v in sorted(by_type.items())},
            "kinds": {k: sum(1 for m in ms if m["kind"] == k) for k in ("notice", "confirm", "reply")}}


def outcomes(rows: list[dict], seeds: list[int]) -> dict:
    by = {p: {r["seed"]: r for r in rows if r["policy"] == p} for p in POLICIES}
    metrics = ("on_time_pct", "late_orders", "late_days_total", "stockout_material_days", "po_spend",
               "po_days_planned_too_early", "po_abs_err", "questions")
    means = {p: {m: float(np.mean([by[p][s][m] for s in seeds])) for m in metrics} for p in POLICIES}
    ah = [r["answer_hours"] for r in rows if r["policy"] == "worst_ask" and r["answer_hours"] is not None]
    means["worst_ask"]["answer_hours"] = float(np.mean(ah)) if ah else None
    comp = {}
    for ref in ("guess", "ignore", "worst"):
        comp[ref] = {}
        for m in ("on_time_pct", "late_days_total", "stockout_material_days", "po_spend", "po_days_planned_too_early"):
            diffs = [by["worst_ask"][s][m] - by[ref][s][m] for s in seeds]
            better = (lambda x: x > 0) if m == "on_time_pct" else (lambda x: x < 0)
            comp[ref][m] = {"mean_diff": float(np.mean(diffs)), "ci95": boot_ci(diffs), "p_value": sign_flip_p(diffs),
                            "seeds_better": int(sum(better(x) for x in diffs)),
                            "seeds_worse": int(sum(better(-x) for x in diffs))}
    return {"means": means, "sentinel_vs": comp}


def fmt(x, d=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def markdown(R: dict) -> str:
    L = []
    a = L.append
    a("# E6 – Supplier messages in plain language\n")
    a(f"Reproduce: `python -m evaluation.run_ambiguity --seeds {R['seeds']}` ({R['runtime_s']} s). "
      "Definitions of clear / ambiguous / no date: [docs/AMBIGUITY.md](../docs/AMBIGUITY.md).\n")
    a("**Success means:** every ambiguous message is caught (so nobody plans on a guess), clear messages are not "
      "questioned, the range Sentinel plans for contains the real date, and fewer orders ship late than when the "
      "same messages are read by a plain date parser.\n")
    a("## A. Hand-labelled messages\n")
    a("| Set | Who wrote it | n (ambiguous / clear / none) | Fully right | Ambiguity precision / recall | "
      "Asked when clear | Range contains the real date | Planned too early: Sentinel vs plain parser |")
    a("|---|---|---|---|---|---|---|---|")
    who = {"development": "us, while building the reader (not evidence)",
           "blind_1": "separate author, saw only the definitions", "blind_2": "a second separate author"}
    for name, e in R["text"].items():
        for tag, X in (("first run", e.get("first_run")), ("now", e["now"])):
            if X is None or (tag == "now" and e.get("first_run") == e["now"]):
                continue
            if name == "development" and tag == "first run":
                continue
            d = X["detection"]
            a(f"| {name.replace('_', ' ')}{' – ' + tag if e.get('first_run') else ''} | {who[name]} | {X['n']} "
              f"({X['n_ambiguous']} / {X['n_clear']} / {X['n_none']}) | **{X['fully_correct']}/{X['n']}** | "
              f"{fmt(d['precision'], 2)} / {fmt(d['recall'], 2)} | {X['asked_when_clear']} | "
              f"{X['ambiguous_range_covers_meant']}/{X['n_ambiguous']} | "
              f"{X['planned_too_early']['sentinel']} vs {X['planned_too_early']['naive']} of {X['planned_too_early']['n']} |")
    a("\nThe **first run** on a blind set is the honest accuracy figure: it was scored once, before any change. "
      "Rules were then fixed for the failures it exposed, so later rows on that set are no longer blind. "
      "The plain parser is the same date reader without the ambiguity step: it takes the earliest reading and never asks.\n")
    for name, e in R["text"].items():
        if e.get("first_run_failures"):
            a(f"<details><summary>{name.replace('_', ' ')}: the {len(e['first_run_failures'])} first-run failures</summary>\n")
            for f in e["first_run_failures"]:
                a(f"- {f}")
            a("\n</details>\n")
    s = R["stream"]
    a("## B. Every message the simulated suppliers wrote\n")
    a(f"{s['messages']} messages across {R['seeds']} plants ({s['kinds']['notice']} delay notices, "
      f"{s['kinds']['confirm']} 'on track' notes, {s['kinds']['reply']} answers to questions). "
      f"Ambiguous: **{s['detected']}/{s['ambiguous']}** caught, range contains the real date in "
      f"**{s['range_covers_truth']}/{s['ambiguous']}**. Clear: **{s['clear_read_right']}/{s['clear']}** read "
      f"exactly, {s['asked_when_clear']} questioned needlessly. By type: "
      + ", ".join(f"{k} {v['right']}/{v['n']}" for k, v in s["by_type"].items()) + ".")
    a("\n*Caveat:* the simulated suppliers' phrasing was written by the same team as the reader, so B shows the "
      "machinery works at scale; A's blind sets are the accuracy evidence.\n")
    o = R["outcomes"]["means"]
    a("## C. Does it change what ships on time?\n")
    a(f"Same {R['seeds']} plants and faults as E5 (60 days each); every delay notice is written as text "
      "(60 % vaguely when a truthful vague phrasing exists), 20 % of on-time POs get an 'on track' note. "
      "The agent's purchase proposals are approved in every arm; only the handling of supplier text differs.\n")
    a("| How supplier text is handled | On-time % | Days late (total) | Stock-out material-days | PO spend | "
      "PO-days planned too early | Mean error of PO date (days) | Questions sent |")
    a("|---|---|---|---|---|---|---|---|")
    lab = {"structured": "Reference: supplier sends an exact date (no text)", "ignore": "Ignore the text",
           "guess": "Plain parser: earliest reading, never asks", "worst": "Sentinel: latest reading, no questions",
           "worst_ask": "**Sentinel: latest reading + approved question**"}
    for p in ("structured", "ignore", "guess", "worst", "worst_ask"):
        m = o[p]
        a(f"| {lab[p]} | {fmt(m['on_time_pct'], 2)} | {fmt(m['late_days_total'])} | {fmt(m['stockout_material_days'])} | "
          f"{fmt(m['po_spend'], 0)} | {fmt(100 * m['po_days_planned_too_early'])}% | {fmt(m['po_abs_err'], 2)} | "
          f"{fmt(m['questions'])} |")
    a("\n| Sentinel (latest + ask) vs | Metric | Mean difference [95% CI] | p | Seeds better / worse |")
    a("|---|---|---|---|---|")
    for ref, cm in R["outcomes"]["sentinel_vs"].items():
        for k, c in cm.items():
            a(f"| {lab[ref].replace('**', '')} | {k} | {c['mean_diff']:+.2f} [{c['ci95'][0]:+.2f}, {c['ci95'][1]:+.2f}] | "
              f"{c['p_value']:.4f} | {c['seeds_better']} / {c['seeds_worse']} |")
    g = R["outcomes"]["sentinel_vs"]["guess"]["on_time_pct"]
    w = R["outcomes"]["sentinel_vs"]["worst"]
    a(f"\n**Reading the table.** When suppliers write dates vaguely, a plain date parser plans too early on "
      f"{fmt(100 * o['guess']['po_days_planned_too_early'])}% of the days a delayed PO is open, and on-time falls to "
      f"{fmt(o['guess']['on_time_pct'], 2)}% (exact dates: {fmt(o['structured']['on_time_pct'], 2)}%). Sentinel plans "
      f"for the latest reading and keeps {fmt(o['worst_ask']['on_time_pct'], 2)}%: {g['mean_diff']:+.2f} points vs the "
      f"plain parser (95% CI {g['ci95'][0]:+.2f} to {g['ci95'][1]:+.2f}, p = {g['p_value']:.3f}, better in "
      f"{g['seeds_better']} of {R['seeds']} plants). The gain is modest because only delayed POs with a vague "
      f"notice are affected. Asking the supplier makes the planned date about "
      f"{o['worst']['po_abs_err'] / max(o['worst_ask']['po_abs_err'], 1e-9):.0f}x more accurate "
      f"({fmt(o['worst']['po_abs_err'], 2)} → {fmt(o['worst_ask']['po_abs_err'], 2)} days of error), but on top of "
      f"planning for the latest reading it does **not** measurably change on-time "
      f"({w['on_time_pct']['mean_diff']:+.2f} points, p = {w['on_time_pct']['p_value']:.2f}). Its value is a correct "
      f"date for the planner, not extra on-time orders in this simulator.\n")
    ah = o["worst_ask"].get("answer_hours")
    a(f"\nIn the simulator a supplier answers an approved question in {fmt(ah)} hours on average "
      "(drawn from 4-11 h); real suppliers may be slower, which would shrink the gain of asking towards the "
      "'latest reading, no questions' row.\n")
    a("## Limits\n")
    a("- English only, rule-based, a fixed list of phrases (see docs/AMBIGUITY.md). Blind sets are 120 messages each; "
      "a few hundred messages cannot cover how every supplier writes.")
    a("- Blind authors were separate AI agents given only the definitions page, not real suppliers. Real messages "
      "would be messier.")
    a("- Outcome results come from our simulator, where suppliers always tell the truth and answer questions. "
      "A supplier who is wrong about their own date is not modelled.")
    a("- Planning for the latest reading is deliberately cautious: when the true date is early in the range, "
      "Sentinel may expedite or warn when it did not need to.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    a = ap.parse_args()
    seeds = list(range(1, a.seeds + 1))
    t0 = time.time()
    OUT.mkdir(exist_ok=True)
    R = {"seeds": len(seeds), "days": DAYS, "text": text_sets()}
    with ProcessPoolExecutor(a.workers) as ex:
        rows = list(ex.map(run, [(s, p) for s in seeds for p in POLICIES]))
    R["stream"] = stream_scores(rows)
    R["outcomes"] = outcomes(rows, seeds)
    R["per_seed"] = [{k: v for k, v in r.items() if k != "messages"} for r in rows]
    R["runtime_s"] = round(time.time() - t0, 1)
    o = R["outcomes"]["means"]
    best_blind = R["text"].get("blind_2") or R["text"].get("blind_1")
    fr = best_blind.get("first_run") if best_blind else None
    gp = R["outcomes"]["sentinel_vs"]["guess"]["on_time_pct"]
    R["headline"] = {
        "big": f"{o['guess']['on_time_pct']:.1f}% → {o['worst_ask']['on_time_pct']:.1f}%",
        "text": (f"orders on time when suppliers write their dates vaguely (“next week”, “2-3 days late”): "
                 f"a plain date parser vs Sentinel, which plans for the latest reading and asks the supplier after a "
                 f"planner approves (p = {gp['p_value']:.3f}; with exact dates it is "
                 f"{o['structured']['on_time_pct']:.1f}%). A plain parser plans too early "
                 f"{100 * o['guess']['po_days_planned_too_early']:.0f}% of the time, Sentinel "
                 f"{100 * o['worst_ask']['po_days_planned_too_early']:.0f}%. "
                 + (f"On {fr['n']} messages written blind by someone who never saw the code, its first run got "
                    f"{fr['fully_correct']} fully right and caught {fr['detection']['tp']} of {fr['n_ambiguous']} "
                    f"ambiguous ones." if fr else ""))}
    (OUT / "ambiguity.json").write_text(json.dumps(R, indent=1, default=float), encoding="utf-8")
    (OUT / "AMBIGUITY.md").write_text(markdown(R), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print((OUT / "AMBIGUITY.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
