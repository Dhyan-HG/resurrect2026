# Supplier messages: what counts as ambiguous (Problem 6 inside Sentinel)

Suppliers tell us about delivery dates in plain language: "delayed to next week",
"2-3 days late", "next Friday", "will dispatch Monday". Sentinel reads each message,
decides whether it is **clear**, **ambiguous** or carries **no date**, and acts:

| Verdict | What Sentinel does |
|---|---|
| **Clear** – one reading | Updates the PO's expected date. No question. |
| **Ambiguous** – more than one plausible reading remains after using context | Plans for the **latest** reading (so nothing is late by surprise), raises an alert, and drafts **one** question to the supplier. The question is sent only after a planner or buyer approves it. When the supplier answers, the plan is updated. A planner can also set the date by hand (e.g. after a phone call). |
| **None** – no delivery-date information (invoice, payment, drawings…) | Changes nothing. |

Context used: the day the message was sent, and the PO's promised (due) date.

## Definitions

Weeks run **Monday to Sunday**.

| Phrase | Readings |
|---|---|
| a written date ("19 Nov", "Nov 19", "19th", "on 25", "2026-11-19", "19-11-2026") | that date (the next one on or after the message day). A written date that is already past is not a delivery date ("the 12 Nov consignment…") |
| a date that does not exist ("31 Nov", "31 Feb") | the month's last day **or** the 1st of the next (a conflict) |
| "today" / "tomorrow" / "day after tomorrow" | message day / +1 / +2 |
| "in N days", "after N days", "within N days", "N more days" | message day + N (clear) |
| "N days late", "delayed by N days", "N days delay" | due date + N (clear) |
| "in a week" / "delayed by a week" / "in 2 weeks" / "in a fortnight" | +7 / +7 / +14 / +14 (clear) |
| "N days early", "2 days earlier than planned" | due date − N (clear) |
| "about a week from today" | ±1 day around +7 |
| "Monday after next" | the Monday a week after the coming one (clear); "week after next" = Mon–Sun of the week after next |
| a plain weekday ("Friday", "this Friday", "by Friday") | the next such day after the message day (clear) |
| "next week Monday", "Tuesday next week" | that weekday in next week (clear) |
| "on time", "as planned", "on track", "no change" | the due date (clear) |
| "by X", "within X" | X when X is one day ("by Friday" is clear); when X is a period ("by month end") the period's readings |
| hedges ("hopefully", "should", "trying to") | do not make a message ambiguous; they are noted |
| **"next week"** | Mon–Sun of next week |
| "early next week" / "mid next week" / "late next week" / "end of next week" | Mon–Wed / Tue–Thu / Thu–Sun / Fri–Sun of next week |
| **"this week"**, "later this week" | tomorrow to Sunday |
| "end of the week", "by EOW" | Friday–Sunday of this week |
| "this weekend" | Saturday–Sunday |
| "end of the month", "month end" | the month's last 7 days |
| "early next month" | days 1–10 of next month |
| "first week of December" | 1–7 December |
| **"a few days"** / "in the coming days" | 2–5 days |
| "a couple of days" | 2–3 days |
| "2-3 days late", "5 to 7 days delay", "1-2 weeks" | every value in the range |
| "around the 20th", "about a week" | ±1 day |
| **"next Friday"** | the coming Friday **or** the one a week later |
| two options ("18th or 19th", "Mon/Tue", "tomorrow or day after") | both |
| weekday that does not match the written date ("Thursday 27 Nov" – a Friday) | both (a conflict) |
| **dispatch only** ("will dispatch Monday", "ships tomorrow") with no arrival date | dispatch day + 0 to 2 days |
| a dispatch date **and** an arrival date | the arrival date (clear) |
| **delay with no date** ("delayed, will update", "stuck at checkpost", "soon", "ASAP") | due date (or tomorrow, if later) + up to 7 days |
| "before Christmas" / "after Diwali (8 Nov 2026)" | the 3 days before / the 5 days after; "2 days before Christmas" = 23 Dec (clear) |
| numeric dates "05/12" | **dd/mm** (5 Dec), the convention of the suppliers we target. mm/dd only when dd/mm is impossible or already past and mm/dd falls within 90 days. A dot with a one-digit part ("2.5 MT", "11.30 am") is a quantity or a time, not a date. *Changed on 27 Nov after review: the first version also counted mm/dd within 90 days as a second reading, which made Sentinel plan up to ten weeks out for messages like "2/12".* |
| a date named only to be replaced ("instead of 12 Nov", "not the 18th", "from 12th to 16th", "originally due 20 Nov", "we had informed 20th") | ignored |
| a negated date next to another date ("cannot deliver on 20th, will deliver on 23rd") or a past event ("machine broke down on Monday") | ignored |
| only a negated date ("will not reach before 5 Dec", "won't reach tomorrow") | open-ended: from that day (or the day after) up to 7 days later |
| dates in a message about invoices, payments, meetings, forms, quotations, with no mention of the goods | no date |
| a date about when they will *tell* us ("new date will be shared tomorrow") | ignored |

## How it is measured

- **Development set** – `evaluation/ambiguity_dev.json`, 119 messages. Written by the same
  person who wrote the reader, and the reader was built while looking at it. Scores on it
  show the rules work as intended; they are **not** evidence of accuracy on new text.
- **Blind sets** – `evaluation/ambiguity_blind*.json`. Each written by a separate AI agent
  (Claude, in its own session) that was given only this page, never the code. The first score
  on each set is kept in `results/*_first_run.json` and reported as is; rules fixed after seeing a
  set's failures make that set no longer blind. Sets 1 and 2 were committed together with their
  first-run scores, so git history cannot prove the order; from set 3 on, the set is committed
  before it is scored.
- **Audit set** – `evaluation/ambiguity_audit.json`, 70 adversarial messages written by the
  independent code auditor. Used to test fixes, so it is a regression set, not a blind one.
- **Simulated stream** – the plant simulator writes supplier messages with known true dates
  (`sentinel/supplier_text.py`), across 30 seeds. Large, but the phrasing is ours too.
- **Effect on Problem 2** – the same plants run with each way of handling messages:
  ignore the text, guess one date silently, plan for the latest reading, plan for the latest
  reading and ask. Measured on PO arrival forecasts and on orders shipped by their CRD.

## Limits

English only; rule-based; a fixed list of phrases. Phrases outside the table are read as
"no date" or, if they mention a delay, as "delayed, no date" – the safe side, but it asks
more questions than needed. Holidays: only Christmas, Diwali 2026 and New Year.
