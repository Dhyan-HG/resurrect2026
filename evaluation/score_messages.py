"""Score the supplier-message reader on the hand-labelled held-out set.

    python -m evaluation.score_messages          # summary + every failure
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sentinel.ambiguity import interpret, naive_guess, to_day  # noqa: E402

PATH = Path(__file__).with_name("ambiguity_dev.json")


def day(s):
    return to_day(date.fromisoformat(s)) if s else None


def score(items=None) -> dict:
    items = items or json.loads(PATH.read_text(encoding="utf-8"))["items"]
    rows = []
    for it in items:
        S, D = day(it["sent"]), day(it["due"])
        r = interpret(it["text"], S + 0.5, D)
        g = naive_guess(it["text"], S + 0.5, D)
        meant = day(it["meant"])
        rng = [day(x) for x in it["range"]] if it.get("range") else None
        # what each system would plan for
        if r["status"] == "none" or (r["status"] == "clear" and r["no_change"]):
            ours_plan = D
        else:
            ours_plan = r["plan_day"]
        naive_plan = g if g is not None else D
        mid_plan = (r["lo"] + r["hi"] + 1) // 2 if r["status"] == "ambiguous" else ours_plan
        truth_plan = meant if meant is not None else D
        rows.append({"text": it["text"], "label": it["label"], "type": it.get("type"),
                     "status": r["status"], "rtype": r["type"], "lo": r["lo"], "hi": r["hi"],
                     "meant": meant, "range": rng, "ours_plan": ours_plan, "naive_plan": naive_plan,
                     "mid_plan": mid_plan,
                     "truth_plan": truth_plan, "explanation": r["explanation"]})
    amb = [x for x in rows if x["label"] == "ambiguous"]
    clear = [x for x in rows if x["label"] == "clear"]
    none = [x for x in rows if x["label"] == "none"]
    tp = sum(x["status"] == "ambiguous" for x in amb)
    fp = sum(x["status"] == "ambiguous" for x in clear + none)
    fn = len(amb) - tp
    p = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
    clear_ok = sum(x["status"] == "clear" and x["lo"] == x["meant"] for x in clear)
    none_ok = sum(x["status"] == "none" for x in none)
    inside = lambda x: x["meant"] is None or x["lo"] <= x["meant"] <= x["hi"]   # no true date given: any range is fine
    cover = sum(x["status"] == "ambiguous" and inside(x) for x in amb)
    range_exact = sum(x["status"] == "ambiguous" and [x["lo"], x["hi"]] == x["range"] for x in amb)
    dated = [x for x in rows if x["meant"] is not None]
    ours_under = sum(x["ours_plan"] < x["truth_plan"] for x in dated)
    plans = {}
    for k, f in (("sentinel", "ours_plan"), ("earliest", "naive_plan"), ("middle", "mid_plan")):
        errs = [x[f] - x["truth_plan"] for x in dated]
        plans[k] = {"too_early": sum(e < 0 for e in errs), "too_late": sum(e > 0 for e in errs),
                    "exact": sum(e == 0 for e in errs),
                    "mean_abs_err": sum(abs(e) for e in errs) / len(errs) if errs else 0.0}
    naive_under = sum(x["naive_plan"] < x["truth_plan"] for x in dated)
    naive_exact_amb = sum(x["naive_plan"] == x["meant"] for x in amb)
    ours_exact_clear = clear_ok
    naive_exact_clear = sum(x["naive_plan"] == x["meant"] for x in clear)
    fails = [x for x in rows if not (
        (x["label"] == "ambiguous" and x["status"] == "ambiguous" and inside(x)) or
        (x["label"] == "clear" and x["status"] == "clear" and x["lo"] == x["meant"]) or
        (x["label"] == "none" and x["status"] == "none"))]
    return {"n": len(rows), "n_ambiguous": len(amb), "n_clear": len(clear), "n_none": len(none),
            "detection": {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": rc, "f1": f1},
            "asked_when_clear": fp, "clear_correct": clear_ok, "none_correct": none_ok,
            "ambiguous_range_covers_meant": cover, "ambiguous_range_exact": range_exact,
            "planned_too_early": {"sentinel": ours_under, "naive": naive_under, "n": len(dated)},
            "plans": plans, "n_dated": len(dated),
            "exact_on_clear": {"sentinel": ours_exact_clear, "naive": naive_exact_clear},
            "naive_right_on_ambiguous": naive_exact_amb,
            "fully_correct": len(rows) - len(fails), "failures": fails}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    R = score()
    for k, v in R.items():
        if k != "failures":
            print(f"{k}: {v}")
    print(f"\n{len(R['failures'])} failures:")
    for x in R["failures"]:
        print(f"- [{x['label']}/{x['type']}] {x['text']!r} -> {x['status']}/{x['rtype']} "
              f"{x['lo']}..{x['hi']} (meant {x['meant']}, range {x['range']})")
