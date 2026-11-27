# Sentinel – inventory intelligence that warns before an order goes late

Resurrect 2026, Problem 2: Smart Inventory Intelligence.

Sentinel reads a live stream of inventory and order events, keeps exact stock figures even when
the feed is messy, projects a ship date for every order against its Customer Request Date (CRD),
and raises alerts days before a miss. An agent explains each problem from the data and drafts
purchase orders. **Nothing is ordered and no supplier email is sent until a planner approves.**

## Results (30 seeded runs, 6,455 orders; full report in [results/RESULTS.md](results/RESULTS.md))

Every comparison uses the same plants with **identical demand** in each arm. Where a number is a
sanity check rather than evidence (it passes largely because of how the simulator is built), the
report says so.

| Question | Result |
|---|---|
| Do planners do better? | On-time **93.4% → 96.7%** (+3.2 pts, 95% CI +2.3 to +4.2, p < 0.001, better in 26/30 plants); days late −60%; stock-out days −51%; purchase spend **+0.75%** (significant). |
| Is that better than simple rules? | Two rules without Sentinel reach a similar on-time rate: rush-reorder every late PO (96.5%) and 4 days of safety stock (96.1%). They spend **9%** and **1.5%** more than Sentinel and hold **23%** and **4.5%** more stock. Sentinel's gain is the same on-time with targeted purchases, not a higher ceiling. |
| Does it warn early? | A **week** before the CRD: 65% of truly late orders flagged, 73% of flags right (flagging every unstarted order: 20% right). Median warning 8 days. At 3 days naive rules also reach 100% recall, so we do not lean on that number. Without supplier-delay learning flags are almost always right but fewer late orders are caught early. |
| Are the numbers right? | **0 mismatches** in 129,600 checks of on hand / incoming / allocated against a separately written calculator; identical state under duplicates, junk and shuffled delivery (1,800/1,800) and on replay (90/90). If the shop floor uses a different rule than Sentinel assumes, detection at 3 days drops only slightly (F1 0.83 vs 0.84). |
| Can the agent's words be trusted? | The grounding check rejects **100%** of planted false counts ("nothing is late") and made-up order IDs, **91%** of changed numbers, but only **29%** of swapped numbers. The optional Claude rewrite was not exercised in the evaluation. |
| Other alerts | Feed outages 29/30 (median 20 h; the timeout is 18 h); demand spikes 60/60 (19 false alerts); hidden stock loss 77/86 found, median 9 days later (waits for a routine count). |

Reproduce every number with one command (a few seconds):

```bash
python -m evaluation.run_eval --seeds 30
```

## Linked problem: #6 Ambiguity, in the supplier messages

Suppliers rarely send a clean date. They write "delayed to next week", "2-3 days late", "next Friday",
"will dispatch Monday". Sentinel reads each message ([sentinel/ambiguity.py](sentinel/ambiguity.py)),
decides whether it is **clear**, **ambiguous** or has **no date**, and for an ambiguous one:
plans for the **latest** possible day (so nothing is late by surprise), raises an alert, and drafts
**one** question to the supplier, which is sent only after a planner approves it. When the
supplier replies, or the planner sets the date after a phone call, the plan updates.
Definitions of what counts as ambiguous: [docs/AMBIGUITY.md](docs/AMBIGUITY.md).
Full report: [results/AMBIGUITY.md](results/AMBIGUITY.md).

| Question | Result |
|---|---|
| Does it read messages it has never seen? | First run on **150 messages written blind** by a separate AI agent that saw only the definitions, committed to git before scoring (commit `ec5e9c3`): **135 fully right (90%)**; 58 of 60 ambiguous ones caught, 8 clear ones questioned needlessly; all 15 failures listed. Earlier blind sets scored 101/120 and 110/120 on their first runs; rules were fixed after them, so they no longer count as blind. |
| What does planning for the latest day cost? | Until the supplier answers, Sentinel is rarely **too early** but often **too late** (blind set 3: 3 too early and 37 too late of 121 dated messages, vs 42 too early for a plain parser that takes the earliest reading; average error 0.94 days vs 0.60, and 0.50 for a middle-of-range reader). Being late in the plan only makes the planner act sooner; being early makes an order miss by surprise. |
| Does it change outcomes? | 30 plants, every delay notice written as text: on-time **96.1% (earliest-reading parser) → 96.7%** (+0.6 pts, p = 0.050) and +0.5 vs a middle-of-range parser (p = 0.08, not significant); the same as when suppliers send exact dates (96.7%). With the question answered, the planned arrival is within **0.18 days** on average, the most accurate of all approaches. Asking adds accuracy, not extra on-time orders. |

```bash
python -m evaluation.run_ambiguity --seeds 30     # a few seconds
```

In the dashboard: **Simulate a problem → A supplier writes something vague**, or **Write one yourself**
to type any message and watch Sentinel read it live.

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # macOS/Linux: .venv/bin/pip
.venv/Scripts/python -m uvicorn sentinel.server:app --port 8000
# open http://localhost:8000
.venv/Scripts/python -m pytest -q                    # tests
```

Optional: set `ANTHROPIC_API_KEY` and `SENTINEL_LLM=1` to let Claude (`claude-opus-5-5`) rewrite
the briefings in plainer language. Its text is used only if every number in it is found in the data;
otherwise the template text is kept. Everything else, including the evaluation, runs without a key.

## How it works

```
plant / ERP / WMS / supplier events
        │  (duplicates, junk, out of order, outages)
        ▼
 EventStore ── validates, de-duplicates by event_id, quarantines bad events
        │
        ▼
 Engine ── rebuilds the books from events sorted by (time, id)  → same answer on every replay
        ├─ net availability = on hand + incoming − allocated
        ├─ allocation by the documented rule (below), day by day
        ├─ learned supplier delay → risk-adjusted arrival dates
        └─ projected ship date + risk for every order, alerts
        ▼
 Agent ── reads the projection, finds slow or missing supply,
        │  runs a what-if for each candidate PO, keeps only POs that save late orders
        ▼
 Planner approves / rejects  ──►  PO created (email draft queued for the buyer, never auto-sent)
```

**Allocation rule.** Open orders are served by earliest CRD (ties: earliest created, then id). Each
order takes each material from stock on hand first, then from incoming purchase orders by
risk-adjusted arrival. Production starts only when every material is covered, and takes 2 days.

**Roles.** Planner (orders, risk, approve), Buyer (POs, suppliers, approve), Manager (KPIs; can
see but cannot approve; the API returns 403). In this demo the role is a switch in the page (an
`X-Role` header), so anyone can act as a planner *in their own sandbox*: every visitor gets a
separate plant (cookie session), writes are rate limited, and nothing leaves the server. A real
deployment would take the role from the company's sign-in (SSO), and the approved supplier email
would go to an outbox for the buyer; neither is built here.

| Path | What |
|---|---|
| `sentinel/events.py` | Event schema, validation, idempotent store, quarantine |
| `sentinel/engine.py` | Book rebuild, net availability, allocation, projection, alerts |
| `sentinel/agent.py` | PO proposals with what-if, role briefings, grounding check, optional Claude |
| `sentinel/ambiguity.py` | Reads supplier messages: clear / ambiguous / no date, range, question draft |
| `sentinel/supplier_text.py` | The simulated suppliers' writing (truthful, often vague), with labels |
| `sentinel/world.py` | Seeded plant simulator: ground truth and the faulty feed |
| `sentinel/server.py` | Web API and live demo session |
| `web/index.html` | Dashboard |
| `evaluation/` | Evaluation harness and independent reference calculator |
| `tests/` | Unit and API tests |

## Evaluation design

The judges asked for defined success, justified metrics, a baseline and reproducible evidence.

- **Ground truth:** a seeded plant simulator that owns the real stock and the real supplier
  behaviour and decides each order's real ship date. It plants known problems: late suppliers
  (some warn, some don't), demand spikes, hidden stock loss, duplicate, junk and shuffled events,
  and a feed outage. Sentinel only ever sees the event feed.
- **Baselines:** for detection, naive rules (every unstarted order; stock on hand only), a
  version that trusts every promised date, and Sentinel without delay learning; for outcomes,
  the company's existing reorder-point planner, the same with 4 days of safety stock, and a
  "rush-reorder any late PO" rule. A plant that does not follow Sentinel's allocation rule tests
  what happens when the documented rule is wrong.
- **Statistics:** detection CIs bootstrap over seeds (orders inside a run are correlated); outcome
  differences are paired by seed (identical demand in every arm), with bootstrap CIs and a
  sign-flip permutation test.
- **Limitations** are listed at the end of [results/RESULTS.md](results/RESULTS.md). The main one:
  the plant is our simulator, and no real planner has used the tool yet.

## Demo script (3 minutes)

See [DEMO.md](DEMO.md).
