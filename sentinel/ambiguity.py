"""Reading supplier messages written in plain language (Problem 6 inside Problem 2).

Suppliers rarely send a clean date. They write "delayed to next week",
"2-3 days late", "next Friday", "will dispatch Monday". A system that turns
that into one date is guessing silently. This module:

  1. reads the message and lists every date it could mean (its *readings*),
     using context: the day it was sent and the PO's promised date;
  2. decides whether the message is CLEAR (one reading), AMBIGUOUS (several
     plausible readings remain after context) or NONE (no date information);
  3. for an ambiguous message, gives the range of readings so the planner can
     plan for the latest one, and drafts one plain question for the supplier
     (sent only after a human approves it).

Definitions (also in docs/AMBIGUITY.md; the held-out test set was labelled
with them before this code was written):
  week = Monday-Sunday. "this week" = tomorrow..Sunday. "end of the week" =
  Friday..Sunday. "weekend" = Saturday..Sunday. "next week" = Monday..Sunday of
  the next week; early = Mon-Wed, mid = Tue-Thu, late = Thu-Sun, end = Fri-Sun.
  "end of the month" = its last 7 days. "early next month" = days 1-10.
  "a few days" = 2-5, "a couple of days" = 2-3, "around X" = X-1..X+1.
  "next <weekday>" = the coming one or the one a week later. A plain weekday is
  the next one after today. "N days late" counts from the promised date;
  "in N days" from the day of the message. Dispatched goods arrive 0-2 days
  after dispatch. "Delayed" with no date = up to 7 days after the promised
  date (or after tomorrow, whichever is later). A weekday that does not match
  the written date (e.g. "Thursday 27 Nov", a Friday) is a conflict.
  Numeric dates are read as dd/mm first; mm/dd is kept only if it is also
  plausible (within the next 90 days).

The reader is deterministic (same text, same context -> same answer), so
Sentinel's replay guarantee still holds. It is rule-based and English-only.
"""
from __future__ import annotations

import calendar
import difflib
import re
from datetime import date, timedelta
from functools import lru_cache

DAY0 = date(2026, 11, 2)          # simulation day 0 is Monday 2 November 2026
TRANSIT_DAYS = 2
MISSING_WINDOW = 7
FEW = (2, 5)
COUPLE = (2, 3)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD_ABBR = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
           "fri": 4, "sat": 5, "sun": 6}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "a": 1, "an": 1}
HOLIDAYS = {"christmas": date(2026, 12, 25), "diwali": date(2026, 11, 8), "new year": date(2027, 1, 1)}

MONTH_RE = r"(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b\.?"
WD_RE = r"(" + "|".join(WEEKDAYS) + r")"
N_RE = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|an?|a few|few|a couple of|couple of)"
RANGE_SEP = r"\s*(?:-|–|/|to|or)\s*"

DELAY_WORDS = re.compile(r"\b(delay\w*|late|behind|postpon\w*|stuck|breakdown|strike|issue|problem|"
                         r"reschedul\w*|as soon as possible|soon|not confirmed|running late|on hold|"
                         r"shortage)\b")
# Dates in a clause about something other than the delivery (invoice, payment, meeting...) are not delivery dates.
NON_DELIVERY = re.compile(r"\b(invoice\w*|payment\w*|pay|credit note|debit note|meeting|office|closed|bill|"
                          r"quotation|quote|dated|holiday list|visit)\b")
# A clause that reports something already done ("300 nos already delivered today").
PAST = re.compile(r"\b(delivered|received|arrived|reached|supplied)\b")
FUTURE = re.compile(r"\b(will|shall|going to|to be|be|by|expected|expect|plan\w*|balance|remaining|rest)\b")
NO_CHANGE = re.compile(r"\b(on time|as planned|as per (the )?(schedule|plan|po)|on schedule|on track|"
                       r"no change|as scheduled|no delay)\b")
HEDGE = re.compile(r"\b(hopefully|maybe|probably|try|trying|should|might|tentative\w*|expect\w*)\b")
DISPATCH = re.compile(r"\b(dispatch\w*|ship|ships|shipped|shipping|send|sent|leave|leaves|leaving|"
                      r"left|pick ?up|load|loaded|loading)\b")
ARRIVAL = re.compile(r"\b(deliver\w*|reach\w*|arriv\w*|receiv\w*|unload\w*|come|coming|land\w*)\b")
COMMS = re.compile(r"\b(will|shall)\s+(update|inform|share|confirm|revert|call|let you know)\b|"
                   r"\bwill be (shared|informed|updated|confirmed|communicated)\b")
EXCLUDE_BEFORE = re.compile(r"(instead of|not|not the|from|was due( on)?|originally( on)?|old date( was)?|"
                            r"earlier date( was)?|dated)\s*$")
APPROX_BEFORE = re.compile(r"(around|about|approx\.?|approximately|roughly)\s*(by\s+|on\s+)?(the\s+)?$")

TYPOS = [
    (r"\b(tmrw|tmr|tmrow|tomorow|tommorow|tommorrow|tomorrw|2moro|2morrow|tmw)\b", "tomorrow"),
    (r"\bnxt\b", "next"), (r"\bwk\b", "week"), (r"\beow\b", "end of the week"),
    (r"\beom\b", "end of the month"), (r"\basap\b", "as soon as possible"),
    (r"\bmonth[- ]end\b", "end of the month"), (r"\bb/w\b", "between"),
    (r"\b(till|until|before end of)\b", "by"), (r"\bmid-", "mid "),
]


def to_date(day: int) -> date:
    return DAY0 + timedelta(days=int(day))


def to_day(d: date) -> int:
    return (d - DAY0).days


def fmt(day: int) -> str:
    d = to_date(day)
    return f"{d:%a} {d.day} {d:%b}"


def _normalize(text: str) -> str:
    s = text.lower().replace("’", "'").replace("–", "-")
    for pat, rep in TYPOS:
        s = re.sub(pat, rep, s)
    out = []
    for tok in re.split(r"(\W+)", s):
        if tok in WD_ABBR:
            out.append(WEEKDAYS[WD_ABBR[tok]])
        elif tok.isalpha() and len(tok) >= 5 and tok not in WEEKDAYS:
            m = difflib.get_close_matches(tok, WEEKDAYS, n=1, cutoff=0.82)
            out.append(m[0] if m else tok)
        else:
            out.append(tok)
    return "".join(out)


def _num(tok: str) -> tuple[int, int]:
    tok = tok.strip()
    if tok in ("a few", "few"):
        return FEW
    if tok in ("a couple of", "couple of"):
        return COUPLE
    v = NUMWORDS.get(tok)
    if v is None:
        v = int(tok)
    return (v, v)


def _year_for(month: int, day: int, S: int) -> date | None:
    """Pick the year that puts (month, day) closest to the message day, not in the past."""
    sd = to_date(S)
    best = None
    for y in (sd.year - 1, sd.year, sd.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if to_day(d) >= S - 3 and (best is None or d < best):
            best = d
    return best


def _next_dom(dom: int, S: int) -> int | None:
    """Next calendar date with this day-of-month, on or after the message day."""
    sd = to_date(S)
    y, m = sd.year, sd.month
    for _ in range(3):
        try:
            d = date(y, m, dom)
            if to_day(d) >= S:
                return to_day(d)
        except ValueError:
            pass
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return None


def _coming(wd: int, S: int) -> int:
    """The next weekday wd strictly after day S."""
    cur = to_date(S).weekday()
    delta = (wd - cur) % 7 or 7
    return S + delta


def _week_start(S: int, offset_weeks: int) -> int:
    return S - to_date(S).weekday() + 7 * offset_weeks


def _month_bounds(S: int, offset: int) -> tuple[int, int]:
    sd = to_date(S)
    y, m = sd.year, sd.month + offset
    while m > 12:
        m, y = m - 12, y + 1
    last = calendar.monthrange(y, m)[1]
    return to_day(date(y, m, 1)), to_day(date(y, m, last))


class _M:
    """One date mention found in the text."""
    __slots__ = ("start", "end", "lo", "hi", "readings", "amb", "type", "cue", "wd", "date", "anchor")

    def __init__(self, start, end, lo, hi, cue, amb=False, type=None, readings=None, wd=None, dt=None):
        self.start, self.end, self.lo, self.hi, self.cue = start, end, lo, hi, cue
        self.amb, self.type, self.readings, self.wd, self.date = amb, type, readings, wd, dt
        self.anchor = None


def _extract(s: str, S: int, D: int) -> list[_M]:
    taken: list[tuple[int, int]] = []
    out: list[_M] = []

    def free(a, b):
        return all(b <= x or a >= y for x, y in taken)

    def add(m: _M):
        out.append(m)
        taken.append((m.start, m.end))

    def scan(pattern, fn):
        for mt in re.finditer(pattern, s):
            if free(mt.start(), mt.end()):
                m = fn(mt)
                if m is not None:
                    add(m)

    # holidays: before / after / on
    def holiday(mt):
        rel, name = mt.group(1), mt.group(2)
        h = to_day(HOLIDAYS[name])
        if rel == "before" or rel == "by":
            return _M(mt.start(), mt.end(), max(S + 1, h - 3), h - 1, mt.group(0), True, "vague_period")
        if rel == "after":
            return _M(mt.start(), mt.end(), h + 1, h + 5, mt.group(0), True, "vague_period")
        return _M(mt.start(), mt.end(), h, h, mt.group(0))
    scan(r"\b(before|after|by|on)\s+(christmas|diwali|new year)\b", holiday)

    # "1st week of December"
    def nth_week(mt):
        k = {"1st": 0, "first": 0, "2nd": 1, "second": 1, "3rd": 2, "third": 2, "4th": 3, "fourth": 3}.get(mt.group(1))
        mon = mt.group(2)
        if mon in ("this month", "next month"):
            lo, hi = _month_bounds(S, 0 if mon == "this month" else 1)
        else:
            d1 = _year_for(MONTHS[mon], 1, S - 31) or _year_for(MONTHS[mon], 1, S)
            lo = to_day(d1)
            hi = lo + calendar.monthrange(d1.year, d1.month)[1] - 1
        if mt.group(1) == "last":
            return _M(mt.start(), mt.end(), hi - 6, hi, mt.group(0), True, "vague_period")
        return _M(mt.start(), mt.end(), lo + 7 * k, lo + 7 * k + 6, mt.group(0), True, "vague_period")
    scan(r"\b(1st|first|2nd|second|3rd|third|4th|fourth|last)\s+week\s+of\s+(this month|next month|"
         + MONTH_RE[1:-6] + r")\b", nth_week)

    # "14-15 Dec", "between 14 and 16 December", "18 or 19 Nov"
    def dm_range(mt):
        if EXCLUDE_BEFORE.search(s[:mt.start()]) and re.search(r"from\s*$", s[:mt.start()]):
            return None
        a, b, sep, mon = int(mt.group(1)), int(mt.group(3)), mt.group(2), MONTHS[mt.group(4)]
        da, db = _year_for(mon, a, S), _year_for(mon, b, S)
        if not da or not db or db < da:
            return None
        lo, hi = to_day(da), to_day(db)
        alt = sep.strip() in ("or", "/")
        return _M(mt.start(), mt.end(), lo, hi, mt.group(0), True, "alternatives" if alt else "range",
                  [lo, hi] if alt else None)
    scan(r"(?:between\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?(\s*(?:-|to|and|or|/)\s*)(?:the\s+)?(\d{1,2})"
         r"(?:st|nd|rd|th)?\s+(?:of\s+)?" + MONTH_RE, dm_range)

    # explicit day + month
    def dm(mt, day_g, mon_g):
        d = _year_for(MONTHS[mt.group(mon_g)], int(mt.group(day_g)), S)
        if not d:
            return None
        return _M(mt.start(), mt.end(), to_day(d), to_day(d), mt.group(0), dt=to_day(d))
    scan(r"\b(\d{1,2})(?:st|nd|rd|th)?(?:\s+|\s*-\s*)(?:of\s+)?" + MONTH_RE + r"(?:\s*-?\s*20\d\d\b)?",
         lambda mt: dm(mt, 1, 2))
    scan(r"\b" + MONTH_RE + r"\s*(\d{1,2})(?:st|nd|rd|th)?\b", lambda mt: dm(mt, 2, 1))

    # numeric dd/mm (and mm/dd if also plausible)
    def numeric(mt):
        a, b = int(mt.group(1)), int(mt.group(2))
        cands = []
        for day_, mon_ in ((a, b), (b, a)):
            if 1 <= mon_ <= 12 and 1 <= day_ <= 31:
                d = _year_for(mon_, day_, S)
                if d and S - 3 <= to_day(d) <= S + 90:
                    cands.append(to_day(d))
        cands = sorted(set(cands))
        if not cands:
            return None
        if len(cands) == 1:
            return _M(mt.start(), mt.end(), cands[0], cands[0], mt.group(0), dt=cands[0])
        return _M(mt.start(), mt.end(), cands[0], cands[-1], mt.group(0), True, "format", cands)
    scan(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b(?!\s*(?:days?|weeks?|nos|units?|pcs))", numeric)

    # ordinal ranges without a month: "between 14th and 16th", "18th or 19th"
    def ord_range(mt):
        if re.search(r"from\s*$", s[:mt.start()]):
            return None
        a, b = _next_dom(int(mt.group(1)), S), _next_dom(int(mt.group(3)), S)
        if a is None or b is None or b < a:
            return None
        alt = mt.group(2).strip() in ("or", "/")
        return _M(mt.start(), mt.end(), a, b, mt.group(0), True, "alternatives" if alt else "range",
                  [a, b] if alt else None)
    scan(r"(?:between\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)(\s*(?:-|to|and|or|/)\s*)(?:the\s+)?(\d{1,2})"
         r"(?:st|nd|rd|th)\b", ord_range)

    # ordinal day: "the 21st", "on 4th"
    def ordinal(mt):
        v = _next_dom(int(mt.group(1)), S)
        return None if v is None else _M(mt.start(), mt.end(), v, v, mt.group(0), dt=v)
    scan(r"(?:the\s+)?\b(\d{1,2})(?:st|nd|rd|th)\b(?!\s*(?:week|day))", ordinal)
    scan(r"\b(?:on|by|for|reaching|reach)\s+(\d{1,2})\b(?!\s*(?:days?|weeks?|[/.:-]|\d|am|pm|units?|pcs|nos|kg))",
         ordinal)

    # relative counts
    def rel(mt, anchor, n_g=2, n2_g=3, unit_g=4, approx_g=1):
        lo, hi = _num(mt.group(n_g))
        if n2_g and mt.group(n2_g):
            hi = _num(mt.group(n2_g))[1]
        mult = 7 if mt.group(unit_g).startswith("week") else 1
        lo, hi = lo * mult, hi * mult
        approx = bool(approx_g and mt.group(approx_g))
        if approx:
            lo, hi = lo - 1, hi + 1
        base = S if anchor == "S" else D
        amb = hi > lo
        typ = ("approximate" if approx else "range") if amb else None
        m = _M(mt.start(), mt.end(), base + lo, base + hi, mt.group(0), amb, typ)
        m.anchor = anchor
        return m
    scan(r"\b(?:delay(?:ed)?|late|behind|postponed|pushed|extended)\s+(?:by\s+|of\s+)?(about\s+|around\s+)?"
         + N_RE + r"(?:" + RANGE_SEP + N_RE + r")?\s+(days?|weeks?)\b",
         lambda mt: rel(mt, "D"))
    scan(r"\b(about\s+|around\s+)?" + N_RE + r"(?:" + RANGE_SEP + N_RE + r")?\s+(days?|weeks?)\s+"
         r"(?:of\s+)?(?:delay\w*|late|behind)\b", lambda mt: rel(mt, "D"))
    scan(r"\b(?:in|after|within)\s+(?:the\s+)?(?:next\s+)?(about\s+|around\s+|approx\.?\s+)?" + N_RE
         + r"(?:" + RANGE_SEP + N_RE + r")?\s+(days?|weeks?)\b", lambda mt: rel(mt, "S"))
    scan(r"\b()" + N_RE + r"(?:" + RANGE_SEP + N_RE + r")?\s+more\s+(days?|weeks?)\b", lambda mt: rel(mt, "S"))
    # "a few days more", "1-2 weeks more", "need a couple of days", "about a week more"
    scan(r"\b(about\s+|around\s+|approx\.?\s+|roughly\s+)?" + N_RE + r"(?:" + RANGE_SEP + N_RE
         + r")?\s+(days?|weeks?)\s+more\b", lambda mt: rel(mt, "S"))
    scan(r"\b(?:need|needs|take|takes|require|requires)\s+(?:another\s+)?(about\s+|around\s+)?" + N_RE
         + r"(?:" + RANGE_SEP + N_RE + r")?\s+(days?|weeks?)\b", lambda mt: rel(mt, "S"))
    scan(r"\b(?:in\s+)?(?:the\s+)?(?:next|coming)\s+(?:few\s+)?days\b",
         lambda mt: _M(mt.start(), mt.end(), S + FEW[0], S + FEW[1], mt.group(0), True, "range"))

    # today / tomorrow / day after
    scan(r"\bday after tomorrow\b", lambda mt: _M(mt.start(), mt.end(), S + 2, S + 2, mt.group(0)))
    scan(r"\btomorrow\b", lambda mt: _M(mt.start(), mt.end(), S + 1, S + 1, mt.group(0)))
    scan(r"\b(today|tonight|this evening|this afternoon)\b", lambda mt: _M(mt.start(), mt.end(), S, S, mt.group(0)))
    scan(r"\bday after\b", lambda mt: _M(mt.start(), mt.end(), S + 2, S + 2, mt.group(0)))

    # weeks
    nw = _week_start(S, 1)
    def next_week(mt):
        mod = (mt.group(1) or "").strip()
        lo, hi = nw, nw + 6
        if mod in ("early", "beginning of", "start of", "starting of"):
            lo, hi = nw, nw + 2
        elif mod in ("mid", "middle of"):
            lo, hi = nw + 1, nw + 3
        elif mod in ("late", "later"):
            lo, hi = nw + 3, nw + 6
        elif mod in ("end of", "towards end of", "towards the end of", "by end of", "by the end of"):
            lo, hi = nw + 4, nw + 6
        return _M(mt.start(), mt.end(), lo, hi, mt.group(0), True, "vague_period")
    scan(r"\b(early|beginning of|start of|starting of|mid|middle of|late|later|towards the end of|"
         r"towards end of|end of)?\s*(?:the\s+)?next\s+week\b", next_week)
    tw = _week_start(S, 0)
    def end_of_week(mt):
        lo, hi = max(S + 1, tw + 4), tw + 6
        if lo > hi:
            lo, hi = nw + 4, nw + 6
        return _M(mt.start(), mt.end(), lo, hi, mt.group(0), True, "vague_period")
    scan(r"\bend of (?:the |this )?week\b", end_of_week)
    def weekend(mt):
        lo, hi = max(S + 1, tw + 5), tw + 6
        if lo > hi:
            lo, hi = nw + 5, nw + 6
        return _M(mt.start(), mt.end(), lo, hi, mt.group(0), lo != hi, "vague_period" if lo != hi else None)
    scan(r"\b(?:this\s+|the\s+)?weekend\b", weekend)
    def this_week(mt):
        lo, hi = S + 1, tw + 6
        if lo > hi:
            lo, hi = nw, nw + 6
        return _M(mt.start(), mt.end(), lo, hi, mt.group(0), True, "vague_period")
    scan(r"\b(?:later\s+|sometime\s+)?this\s+week\b", this_week)

    # months
    def month_end(mt):
        lo, hi = _month_bounds(S, 0)
        lo = max(hi - 6, S + 1)
        if lo > hi:
            lo2, hi = _month_bounds(S, 1)
            lo = hi - 6
        return _M(mt.start(), mt.end(), lo, hi, mt.group(0), True, "vague_period")
    scan(r"\bend of (?:the |this )?month\b", month_end)
    def early_next_month(mt):
        lo, _ = _month_bounds(S, 1)
        return _M(mt.start(), mt.end(), lo, lo + 9, mt.group(0), True, "vague_period")
    scan(r"\b(?:early|beginning of|start of|first week of)\s+(?:the\s+)?next month\b", early_next_month)
    def mid_month(mt):
        lo, _ = _month_bounds(S, 0)
        if S > lo + 19:
            lo, _ = _month_bounds(S, 1)
        return _M(mt.start(), mt.end(), max(S + 1, lo + 10), lo + 19, mt.group(0), True, "vague_period")
    scan(r"\b(?:mid|middle of (?:the |this )?)\s*month\b", mid_month)
    def next_month(mt):
        lo, hi = _month_bounds(S, 1)
        return _M(mt.start(), mt.end(), lo, hi, mt.group(0), True, "vague_period")
    scan(r"\bnext month\b", next_month)

    # weekdays (bound to "next week"/"this week" when the message names one)
    week_ctx = None
    for m in out:
        if "next week" in m.cue:
            week_ctx = nw
        elif "this week" in m.cue:
            week_ctx = tw
    def weekday(mt):
        mod, wd = (mt.group(1) or "").strip(), WEEKDAYS.index(mt.group(2))
        if week_ctx is not None and mod not in ("next", "this", "coming"):
            v = week_ctx + wd
            return _M(mt.start(), mt.end(), v, v, mt.group(0), wd=wd)
        c = _coming(wd, S)
        if mod == "next":
            return _M(mt.start(), mt.end(), c, c + 7, mt.group(0), True, "next_weekday", [c, c + 7], wd=wd)
        return _M(mt.start(), mt.end(), c, c, mt.group(0), wd=wd)
    scan(r"\b(?:(this|next|coming|on|by)\s+)?" + WD_RE + r"\b", weekday)
    if week_ctx is not None and any(m.wd is not None for m in out):
        out = [m for m in out if not ("next week" in m.cue or "this week" in m.cue)]
    out.sort(key=lambda m: m.start)
    return out


@lru_cache(maxsize=20000)
def _interpret(text: str, S: int, D: int) -> tuple:
    s = _normalize(text)
    ms = _extract(s, S, D)

    # 1. drop dates that are named only to be replaced ("instead of 12 Nov", "not the 18th", "from 12th")
    ms = [m for m in ms if not EXCLUDE_BEFORE.search(s[:m.start])]

    # 2. approximations: "around the 20th"
    for m in ms:
        if not m.amb and APPROX_BEFORE.search(s[:m.start]):
            m.lo, m.hi, m.amb, m.type = m.lo - 1, m.hi + 1, True, "approximate"

    # 3. clauses: drop dates about *when they will tell us*, and dispatch dates when an arrival date exists
    bounds = [0] + [x.end() for x in re.finditer(r"[,;.!?]|\band\b|\bbut\b|\bthen\b", s)] + [len(s) + 1]
    def clause(m):
        for a, b in zip(bounds, bounds[1:]):
            if a <= m.start < b:
                return s[a:b]
        return s
    ms = [m for m in ms if not (COMMS.search(clause(m)) and not ARRIVAL.search(clause(m)))]
    ms = [m for m in ms if not (NON_DELIVERY.search(clause(m)) and not ARRIVAL.search(clause(m)))]
    ms = [m for m in ms if not (PAST.search(clause(m)) and not FUTURE.search(clause(m)))]
    disp = [m for m in ms if DISPATCH.search(clause(m)) and not ARRIVAL.search(clause(m)) and m.anchor != "D"]
    if disp and len(disp) < len(ms):
        ms = [m for m in ms if m not in disp]
    elif disp and NO_CHANGE.search(s):        # "on track, truck loading tomorrow"
        return ("clear", None, D, D, None, ("no_change",), bool(HEDGE.search(s)), True)
    elif disp:
        for m in ms:
            m.hi += TRANSIT_DAYS
            m.amb, m.type = True, "dispatch_vs_arrival"
            m.readings = None

    # 4. weekday + written date that disagree
    for w in [m for m in ms if m.wd is not None and m.lo == m.hi]:
        near = [d for d in ms if d.date is not None and abs(d.start - w.start) < 25]
        for d in near:
            if to_date(d.date).weekday() == w.wd:
                ms = [m for m in ms if m is not w]
            else:
                ms = [m for m in ms if m is not w and m is not d]
                lo, hi = sorted((w.lo, d.lo))
                ms.append(_M(min(w.start, d.start), max(w.end, d.end), lo, hi,
                             s[min(w.start, d.start):max(w.end, d.end)], True, "conflict", [lo, hi], wd=w.wd))
            break

    hedged = bool(HEDGE.search(s))
    cues = tuple(m.cue.strip() for m in ms)
    if not ms:
        if NON_DELIVERY.search(s) and not ARRIVAL.search(s) and not DELAY_WORDS.search(s):
            return ("none", None, None, None, None, (), hedged, False)
        if NO_CHANGE.search(s):
            return ("clear", None, D, D, None, ("no_change",), hedged, True)
        if DELAY_WORDS.search(s):
            lo = max(D, S + 1)
            cue = DELAY_WORDS.search(s).group(0)
            return ("ambiguous", "missing_date", lo, lo + MISSING_WINDOW, None, (cue,), hedged, False)
        return ("none", None, None, None, None, (), hedged, False)
    if len(ms) == 1:
        m = ms[0]
        if m.amb:
            return ("ambiguous", m.type, m.lo, m.hi, tuple(m.readings) if m.readings else None, cues, hedged, False)
        return ("clear", None, m.lo, m.lo, None, cues, hedged, False)
    pts = sorted({(m.lo, m.hi) for m in ms})
    if len(pts) == 1 and pts[0][0] == pts[0][1]:
        return ("clear", None, pts[0][0], pts[0][0], None, cues, hedged, False)
    lo, hi = min(m.lo for m in ms), max(m.hi for m in ms)
    if all(m.lo == m.hi for m in ms):
        readings = tuple(sorted({m.lo for m in ms}))
    else:
        readings = None
    typ = "conflict" if any(m.type == "conflict" for m in ms) else "alternatives"
    return ("ambiguous", typ, lo, hi, readings, cues, hedged, False)


def interpret(text: str, sent: float, due: float, po: str = "this order") -> dict:
    """Read one supplier message. `sent` = event time (days), `due` = the PO's promised day."""
    S, D = int(sent // 1), int(round(due))
    status, typ, lo, hi, readings, cues, hedged, no_change = _interpret(text, S, D)
    r = {"status": status, "type": typ, "lo": lo, "hi": hi, "day": lo if status == "clear" else None,
         "plan_day": hi, "readings": list(readings) if readings else None, "cues": list(cues),
         "hedged": hedged, "no_change": no_change}
    r["explanation"] = _explain(r)
    r["question"] = _question(r, po, text) if status == "ambiguous" else None
    return r


def _explain(r: dict) -> str:
    if r["status"] == "none":
        return "No delivery date in this message. Nothing changed."
    if r["status"] == "clear":
        if r["no_change"]:
            return f"Supplier confirms the plan: {fmt(r['day'])} (day {r['day']})."
        return f"Clear: {fmt(r['day'])} (day {r['day']})."
    if r["readings"] and len(r["readings"]) <= 4:
        opts = " or ".join(f"{fmt(x)} (day {x})" for x in r["readings"])
        return f"Unclear: could be {opts}. Planning for the latest, day {r['hi']}, until the supplier confirms."
    return (f"Unclear: could be any day from {fmt(r['lo'])} to {fmt(r['hi'])} (day {r['lo']}-{r['hi']}). "
            f"Planning for the latest, day {r['hi']}, until the supplier confirms.")


def _question(r: dict, po: str, text: str) -> str:
    cue = r["cues"][0] if r["cues"] else text
    lo, hi, t = r["lo"], r["hi"], r["type"]
    head = f"Thank you for the update on {po}. To plan our production we need one exact date. "
    if t == "missing_date":
        return head + f"What is the new delivery date? Until we hear from you we are planning for {fmt(hi)} at the latest."
    if t == "dispatch_vs_arrival":
        return head + f"Is {fmt(lo)} the day it leaves your factory, or the day it reaches our plant? If it is the dispatch day, when will it arrive?"
    if t == "conflict" and r["readings"]:
        a, b = r["readings"]
        return head + f"You wrote \"{cue}\", but those do not match. Is it {fmt(a)} or {fmt(b)}?"
    if r["readings"] and len(r["readings"]) <= 4:
        return head + "Which day will it reach our plant: " + " or ".join(fmt(x) for x in r["readings"]) + "?"
    return head + f"\"{cue}\" could be any day from {fmt(lo)} to {fmt(hi)}. Which day will it reach our plant?"


def naive_guess(text: str, sent: float, due: float) -> int | None:
    """Baseline: the same date reader, but it never asks. It takes the earliest
    reading as *the* date (what a plain date parser does) and ignores messages
    with no date. Returns None when it would leave the PO unchanged."""
    r = interpret(text, sent, due)
    if r["status"] == "none" or r["no_change"]:
        return None
    if r["type"] == "missing_date":
        return None
    if r["type"] == "dispatch_vs_arrival":
        return r["lo"]
    return r["lo"]
