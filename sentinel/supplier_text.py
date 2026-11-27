"""The simulated suppliers' writing: plain-language messages with known meaning.

When a supplier in the plant simulator warns about a delay, it can write the
new date the way real suppliers do - sometimes exactly, often vaguely - but
always truthfully (the true arrival day is one of the message's readings).
Each message comes with its label (clear/ambiguous, readings) so the
evaluation can score Sentinel's reading against the truth.

This file is the simulator's side. Sentinel's reader is sentinel/ambiguity.py
and does not import this module.
"""
from __future__ import annotations

import calendar
import random

from .ambiguity import to_date

OPEN = ["", "", "Dear team, ", "Hi, ", "Sir, ", "Kindly note: ", "Update: ", "Good morning. "]
CLOSE = ["", "", "", " Sorry for the inconvenience.", " Regards.", " Thanks.", " Will keep you posted."]
WD = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _d(day: int) -> str:
    d = to_date(day)
    return f"{d.day} {d:%b}"


def _ord(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _week0(day: int) -> int:
    return day - to_date(day).weekday()


def _wrap(r: random.Random, body: str) -> str:
    opener = r.choice(OPEN)
    if opener.endswith(", ") and not body.startswith("PO"):
        body = body[0].lower() + body[1:]
    return (opener + body + r.choice(CLOSE)).strip()


def clear_styles(r: random.Random, po: str, S: int, D: int, A: int) -> list[str]:
    out = [f"{po}: delivery moved to {WD[to_date(A).weekday()]} {_d(A)}.",
           f"Sorry for the delay on {po}. New delivery date is {_d(A)}.",
           f"{po} will now reach your plant on the {_ord(to_date(A).day)}."]
    if A - D >= 1:
        n = f"{A - D} day" + ("" if A - D == 1 else "s")
        out.append(f"{po} is running {n} late.")
        out.append(f"Delivery of {po} delayed by {n}.")
    if 2 <= A - S <= 14:
        out.append(f"{po} will reach you in {A - S} days.")
    if A - S == 1:
        out.append(f"{po} delivery tomorrow.")
    if 2 <= A - S <= 6:
        out.append(f"{po} delivery now on {WD[to_date(A).weekday()]}.")
    d = to_date(A)
    if d.day > 12:                      # dd/mm cannot be read the other way round
        out.append(f"Revised date for {po}: {d.day:02d}/{d.month:02d}.")
    return out


def ambiguous_styles(r: random.Random, po: str, S: int, D: int, A: int) -> list[tuple[str, str, int, int]]:
    """(text, type, lo, hi) for every vague phrasing that is still true for arrival day A."""
    out = []
    nw = _week0(S) + 7
    if nw <= A <= nw + 6:
        out.append((r.choice([f"{po} delayed, will deliver next week.", f"Material for {po} will reach next week.",
                              f"{po}: nxt wk delivery."]), "vague_period", nw, nw + 6))
    if nw <= A <= nw + 2:
        out.append((f"{po} should reach early next week.", "vague_period", nw, nw + 2))
    if nw + 4 <= A <= nw + 6:
        out.append((f"{po} will come by end of next week.", "vague_period", nw + 4, nw + 6))
    tw = _week0(S)
    if S + 1 <= A <= tw + 6 and S + 1 < tw + 6:
        out.append((f"{po} will arrive later this week.", "vague_period", S + 1, tw + 6))
    k = A - D
    if k >= 1:
        lo = r.randint(max(1, k - 2), k)
        if lo + 2 >= k:
            out.append((f"We are running {lo}-{lo + 2} days late on {po}.", "range", D + lo, D + lo + 2))
    if 2 <= A - S <= 5:
        out.append((f"{po} will reach in a few days.", "range", S + 2, S + 5))
    wd = to_date(A).weekday()
    c = S + ((wd - to_date(S).weekday()) % 7 or 7)
    if A in (c, c + 7):
        out.append((f"{po} postponed to next {WD[wd]}.", "next_weekday", c, c + 7))
    if A - S >= 3:
        out.append((f"{po} delivery around the {_ord(to_date(A).day)}.", "approximate", A - 1, A + 1))
    for back in (0, 1, 2):
        X = A - back
        if 1 <= X - S <= 6:
            out.append((f"{po} will be dispatched on {WD[to_date(X).weekday()]}.", "dispatch_vs_arrival", X, X + 2))
            break
    lo = max(D, S + 1)
    if lo <= A <= lo + 7:
        out.append((r.choice([f"{po} is delayed, we will share the new date.",
                              f"Problem at our end on {po}, delivery will be late. Will update.",
                              f"{po} stuck at checkpost, sorry for the delay."]), "missing_date", lo, lo + 7))
    d = to_date(A)
    last = calendar.monthrange(d.year, d.month)[1]
    if d.day > last - 7 and S < A and to_date(S).month == d.month:
        mlo = A - (d.day - (last - 6))
        if mlo >= S + 1:
            out.append((f"{po} delivery by month end.", "vague_period", mlo, A + (last - d.day)))
    return out


def delay_message(r: random.Random, po: str, sent: float, due: int, actual: int,
                  p_vague: float = 0.6, force: str | None = None) -> tuple[str, dict]:
    """A truthful message about a delayed PO. Returns (text, label)."""
    S = int(sent // 1)
    amb = ambiguous_styles(r, po, S, due, actual)
    if amb and (force == "ambiguous" or (force is None and r.random() < p_vague)):
        text, typ, lo, hi = r.choice(amb)
        return _wrap(r, text), {"label": "ambiguous", "type": typ, "lo": lo, "hi": hi, "meant": actual}
    text = r.choice(clear_styles(r, po, S, due, actual))
    return _wrap(r, text), {"label": "clear", "type": None, "lo": actual, "hi": actual, "meant": actual}


def confirm_message(r: random.Random, po: str, due: int) -> tuple[str, dict]:
    text = r.choice([f"{po} on track, delivery as planned.", f"{po}: no change, delivery on time.",
                     f"Confirming {po} for {_d(due)}."])
    return _wrap(r, text), {"label": "clear", "type": None, "lo": due, "hi": due, "meant": due}


def reply_message(r: random.Random, po: str, actual: int) -> str:
    return r.choice([f"Confirmed: {po} will reach your plant on {WD[to_date(actual).weekday()]} {_d(actual)}.",
                     f"Sorry for the confusion. {po} delivery date is {_d(actual)}.",
                     f"{po} - exact date {_d(actual)}."])
