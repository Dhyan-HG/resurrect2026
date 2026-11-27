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
from evaluation.score_messages import score  # noqa: E402
from evaluation.run_eval import boot_ci, sign_flip_p  # noqa: E402

DAYS = 60
OUT = Path(__file__).resolve().parents[1] / "results"
EV = Path(__file__).resolve().parent
POLICIES = {
    "structured": ("worst", False, 0.0),     # suppliers send exact dates (reference)
    "ignore": ("ignore", False, 1.0),       # free text not read at all
    "guess": ("guess", False, 1.0),         # earliest reading, never asks
    "mid": ("mid", False, 1.0),             # middle of the range, never asks
    "worst": ("worst", False, 1.0),         # latest reading, no questions
    "worst_ask": ("worst", True, 1.0),      # latest reading + approved question (Sentinel)
}


# ---------------------------------------------------------------- B + C. plants
def run(args) -> dict:
    seed, policy = args
    text_policy, ask, share = POLICIES[policy]
    w = World(seed=seed, days=DAYS, faults=Faults(supplier_text=share, confirmations=0.2 if share else 0.0))
    opt = engine.EngineOptions(text_policy=text_policy)
    surprise = []      # per (PO, day after its delay notice): engine's planned arrival < true arrival
    too_late = []      # ... planned arrival > true arrival
    abs_err = []
    for d in range(DAYS):
        w.simulate_day(d)
        w.rop_planner(d)
        agent_planner(w, d, opt=opt, ask=ask)
        t = d + 0.95
        st = engine.rebuild(w.store, t, text_policy)
        sup = engine.supplier_stats(st, w.cat)
        for pid, p in st.pos.items():
            tp = w.truth.pos.get(pid)
            if p["status"] != "open" or not tp or tp["notified_at"] is None or tp["notified_at"] > t:
                continue
            eta = math.ceil(engine.po_eta(p, t, sup, opt))   # the day the engine actually plans with
            surprise.append(eta < tp["actual"])
            too_late.append(eta > tp["actual"])
            abs_err.append(abs(eta - tp["actual"]))
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
            "po_days_planned_too_late": float(np.mean(too_late)) if too_late else 0.0,
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
               "po_days_planned_too_early", "po_days_planned_too_late", "po_abs_err", "questions")
    means = {p: {m: float(np.mean([by[p][s][m] for s in seeds])) for m in metrics} for p in POLICIES}
    ah = [r["answer_hours"] for r in rows if r["policy"] == "worst_ask" and r["answer_hours"] is not None]
    means["worst_ask"]["answer_hours"] = float(np.mean(ah)) if ah else None
    comp = {}
    for ref in ("guess", "mid", "ignore", "worst"):
        comp[ref] = {}
        for m in ("on_time_pct", "late_days_total", "stockout_material_days", "po_spend", "po_days_planned_too_early",
                  "po_abs_err"):
            diffs = [by["worst_ask"][s][m] - by[ref][s][m] for s in seeds]
            better = (lambda x: x > 0) if m == "on_time_pct" else (lambda x: x < 0)
            comp[ref][m] = {"mean_diff": float(np.mean(diffs)), "ci95": boot_ci(diffs), "p_value": sign_flip_p(diffs),
                            "seeds_better": int(sum(better(x) for x in diffs)),
                            "seeds_worse": int(sum(better(-x) for x in diffs))}
    return {"means": means, "sentinel_vs": comp}


def fmt(x, d=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def _git(*args) -> str:
    import subprocess
    try:
        return subprocess.run(["git", *args], cwd=EV.parent, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- A. text sets
SETS = [("development", "ambiguity_dev.json", None, "us, while building the reader (not evidence)"),
        ("audit", "ambiguity_audit.json", None, "the independent code auditor; used to test fixes (not blind)"),
        ("blind_1", "ambiguity_blind.json", "ambiguity_blind_first_run.json", "separate AI agent, saw only the definitions"),
        ("blind_2", "ambiguity_blind2.json", "ambiguity_blind2_first_run.json", "a second separate AI agent"),
        ("blind_3", "ambiguity_blind3.json", "ambiguity_blind3_first_run.json",
         "a third separate AI agent; set committed before it was scored")]


def text_sets() -> dict:
    out = {}
    for name, fn, first, who in SETS:
        p = EV / fn
        if not p.exists():
            continue
        R = score(json.loads(p.read_text(encoding="utf-8"))["items"])
        entry = {"who": who, "now": {k: v for k, v in R.items() if k != "failures"},
                 "failures_now": [f"{x['text']} -> {x['status']}" for x in R["failures"]]}
        if first:
            fp = OUT / first
            if not fp.exists():
                # Freeze the very first score, but only for a set that is already committed, and
                # record which commit of the set and of the reader produced it.
                set_commit = _git("log", "-1", "--format=%H", "--", f"evaluation/{fn}")
                if not set_commit or _git("status", "--porcelain", "--", f"evaluation/{fn}"):
                    print(f"NOT freezing {fn}: commit the set first, then score it.")
                else:
                    R["frozen"] = {"set_commit": set_commit, "reader_commit": _git("rev-parse", "HEAD"),
                                   "reader_dirty": bool(_git("status", "--porcelain", "--", "sentinel")),
                                   "scored_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                    fp.write_text(json.dumps(R, indent=1, default=str), encoding="utf-8")
            if fp.exists():
                F = json.loads(fp.read_text(encoding="utf-8"))
                entry["first_run"] = {k: v for k, v in F.items() if k != "failures"}
                entry["first_run_failures"] = [f"{x['text']} -> {x['status']}" for x in F["failures"]]
        out[name] = entry
    return out


def markdown(R: dict) -> str:
    L = []
    a = L.append
    a("# E6 – Supplier messages in plain language\n")
    a(f"Reproduce: `python -m evaluation.run_ambiguity --seeds {R['seeds']}` ({R['runtime_s']} s). "
      "Definitions of clear / ambiguous / no date: [docs/AMBIGUITY.md](../docs/AMBIGUITY.md).\n")
    a("**Success means:** ambiguous messages are caught (nobody plans on a guess), clear ones are not questioned, "
      "the range Sentinel plans for contains the real date, and fewer orders ship late than when the same "
      "messages are read by a plain date parser. **The trade-off is stated, not hidden:** planning for the "
      "latest reading means Sentinel is rarely too early but often too late until the supplier answers.\n")
    a("## A. Hand-labelled messages\n")
    a("| Set | Written by | n (amb / clear / none) | Fully right | Ambiguity precision / recall | Asked when clear | Range contains the real date |")
    a("|---|---|---|---|---|---|---|")
    for name, e in R["text"].items():
        rows = []
        if e.get("first_run"):
            rows.append(("first run", e["first_run"]))
        if not e.get("first_run") or e["first_run"]["fully_correct"] != e["now"]["fully_correct"]:
            rows.append(("now" if e.get("first_run") else "", e["now"]))
        for tag, X in rows:
            d = X["detection"]
            a(f"| {name.replace('_', ' ')}{' – ' + tag if tag else ''} | {e['who']} | {X['n']} "
              f"({X['n_ambiguous']} / {X['n_clear']} / {X['n_none']}) | **{X['fully_correct']}/{X['n']}** | "
              f"{fmt(d['precision'], 2)} / {fmt(d['recall'], 2)} | {X['asked_when_clear']} | "
              f"{X['ambiguous_range_covers_meant']}/{X['n_ambiguous']} |")
    a("\nThe **first run** on a blind set is the honest accuracy figure. Rules were fixed after blind sets 1 and 2 "
      "and after the auditor's set, so their later rows are no longer blind. Blind set 3 was written after those "
      "fixes and committed to git before it was scored (commit recorded in `results/ambiguity_blind3_first_run.json`). "
      "Note: after review the numeric-date rule changed (dd/mm first, see docs/AMBIGUITY.md); one item in blind 1 "
      "and one in the audit set were labelled under the old rule and now count as failures.\n")
    a("**What each way of reading would plan for**, against the date the supplier meant (messages with a known "
      "date; current reader, so blind sets 1-2 are no longer blind here):\n")
    a("| Set | Reader | Too early | Exact | Too late | Mean error (days) |")
    a("|---|---|---|---|---|---|")
    lab = {"sentinel": "Sentinel (latest reading)", "earliest": "Plain parser (earliest reading)",
           "middle": "Middle of the range"}
    for name, e in R["text"].items():
        X = e["now"]
        for k in ("sentinel", "earliest", "middle"):
            p = X["plans"][k]
            a(f"| {name.replace('_', ' ')} | {lab[k]} | {p['too_early']} | {p['exact']} | {p['too_late']} | "
              f"{p['mean_abs_err']:.2f} |")
    a("\nOn clear messages all three agree. On ambiguous ones the plain parser is too early about as often as "
      "Sentinel is too late, and the middle of the range has the smallest average error. Sentinel accepts the "
      "larger error on purpose: being too early makes an order miss its date by surprise, being too late only "
      "makes the planner act sooner than needed, and the question to the supplier then removes the error.\n")
    for name, e in R["text"].items():
        if e.get("first_run_failures"):
            a(f"<details><summary>{name.replace('_', ' ')}: the {len(e['first_run_failures'])} first-run failures</summary>\n")
            for f in e["first_run_failures"]:
                a(f"- {f}")
            a("\n</details>\n")
    s = R["stream"]
    a("## B. Every message the simulated suppliers wrote (a sanity check)\n")
    a(f"{s['messages']} messages across {R['seeds']} plants ({s['kinds']['notice']} delay notices, "
      f"{s['kinds']['confirm']} 'on track' notes, {s['kinds']['reply']} answers to questions). "
      f"Ambiguous: {s['detected']}/{s['ambiguous']} caught, range contains the real date in "
      f"{s['range_covers_truth']}/{s['ambiguous']}. Clear: {s['clear_read_right']}/{s['clear']} read exactly. "
      "**This is close to 100% by construction:** the simulated suppliers only use phrasings from the same "
      "definitions the reader implements. It shows the plumbing works at scale, not that the reader is accurate; "
      "section A is the accuracy evidence.\n")
    o = R["outcomes"]["means"]
    a("## C. Does it change what ships on time?\n")
    a(f"Same {R['seeds']} plants, faults and demand in every arm (60 days each); every delay notice is written as "
      "text (60 % vaguely when a truthful vague phrasing exists), 20 % of on-time POs get an 'on track' note. The "
      "agent's purchase proposals are approved in every arm; only the handling of supplier text differs. 'Planned "
      "too early / too late' is measured on the arrival day the engine actually plans with, on every day a "
      "delayed PO is open after its notice.\n")
    a("| How supplier text is handled | On-time % | Days late (total) | Stock-out material-days | PO spend | "
      "Planned too early | Planned too late | Mean error of planned arrival (days) | Questions sent |")
    a("|---|---|---|---|---|---|---|---|---|")
    lab2 = {"structured": "Reference: supplier sends an exact date (no text)", "ignore": "Ignore the text",
            "guess": "Plain parser: earliest reading, never asks", "mid": "Middle of the range, never asks",
            "worst": "Sentinel: latest reading, no questions",
            "worst_ask": "**Sentinel: latest reading + approved question**"}
    for p in ("structured", "ignore", "guess", "mid", "worst", "worst_ask"):
        m = o[p]
        a(f"| {lab2[p]} | {fmt(m['on_time_pct'], 2)} | {fmt(m['late_days_total'])} | {fmt(m['stockout_material_days'])} | "
          f"{fmt(m['po_spend'], 0)} | {fmt(100 * m['po_days_planned_too_early'])}% | "
          f"{fmt(100 * m['po_days_planned_too_late'])}% | {fmt(m['po_abs_err'], 2)} | {fmt(m['questions'])} |")
    a("\n| Sentinel (latest + ask) vs | Metric | Mean difference [95% CI] | p | Seeds better / worse |")
    a("|---|---|---|---|---|")
    for ref, cm in R["outcomes"]["sentinel_vs"].items():
        for k, c in cm.items():
            a(f"| {lab2[ref].replace('**', '')} | {k} | {c['mean_diff']:+.2f} [{c['ci95'][0]:+.2f}, {c['ci95'][1]:+.2f}] | "
              f"{c['p_value']:.4f} | {c['seeds_better']} / {c['seeds_worse']} |")
    g = R["outcomes"]["sentinel_vs"]["guess"]["on_time_pct"]
    mm = R["outcomes"]["sentinel_vs"]["mid"]["on_time_pct"]
    w = R["outcomes"]["sentinel_vs"]["worst"]
    a(f"\n**Reading the table.** With vague supplier text, Sentinel ships {fmt(o['worst_ask']['on_time_pct'], 2)}% on "
      f"time vs {fmt(o['guess']['on_time_pct'], 2)}% for the earliest-reading parser ({g['mean_diff']:+.2f} points, "
      f"95% CI {g['ci95'][0]:+.2f} to {g['ci95'][1]:+.2f}, p = {g['p_value']:.3f}) and "
      f"{fmt(o['mid']['on_time_pct'], 2)}% for the middle-of-range parser ({mm['mean_diff']:+.2f}, p = "
      f"{mm['p_value']:.3f}); with exact dates from the supplier it is {fmt(o['structured']['on_time_pct'], 2)}%. "
      f"Asking the supplier, on top of planning for the latest reading, makes the planned arrival more accurate "
      f"({fmt(o['worst']['po_abs_err'], 2)} → {fmt(o['worst_ask']['po_abs_err'], 2)} days of error) but changes "
      f"on-time by {w['on_time_pct']['mean_diff']:+.2f} points (p = {w['on_time_pct']['p_value']:.2f}): its value is a "
      "correct date for the planner, not extra on-time orders in this simulator.\n")
    ah = o["worst_ask"].get("answer_hours")
    a(f"In the simulator a supplier answers an approved question in {fmt(ah)} hours on average (drawn from 4-11 h); "
      "real suppliers may be slower.\n")
    a("## Limits\n")
    a("- English only, rule-based, a fixed list of phrases (docs/AMBIGUITY.md). A few hundred labelled messages "
      "cannot cover how every supplier writes.")
    a("- Blind authors were separate AI agents given only the definitions page, not real suppliers, and one labeller "
      "per message: we did not measure agreement between labellers, so the definitions themselves are ours.")
    a("- Uncertainty is expressed as a range of days, not a probability.")
    a("- Outcome results come from our simulator, where suppliers tell the truth and answer questions.")
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
    fr = None
    for k in ("blind_3", "blind_2"):
        if R["text"].get(k, {}).get("first_run"):
            fr = R["text"][k]["first_run"]
            break
    gp = R["outcomes"]["sentinel_vs"]["guess"]["on_time_pct"]
    R["headline"] = {
        "big": f"{o['guess']['on_time_pct']:.1f}% → {o['worst_ask']['on_time_pct']:.1f}%",
        "text": (f"orders on time when suppliers write their dates vaguely (“next week”, “2-3 days late”): "
                 f"a plain date parser vs Sentinel, which plans for the latest reading and asks the supplier after a "
                 f"planner approves (p = {gp['p_value']:.3f}; with exact dates it is {o['structured']['on_time_pct']:.1f}%). "
                 f"The price: until the supplier answers, Sentinel's planned date is often later than the real one. "
                 + (f"On {fr['n']} test messages written blind by a separate AI agent, the first run got "
                    f"{fr['fully_correct']} fully right." if fr else ""))}
    (OUT / "ambiguity.json").write_text(json.dumps(R, indent=1, default=float), encoding="utf-8")
    (OUT / "AMBIGUITY.md").write_text(markdown(R), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print((OUT / "AMBIGUITY.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
