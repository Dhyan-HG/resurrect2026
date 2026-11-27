# Sentinel – inventory intelligence that warns before an order goes late

Resurrect 2026, Problem 2: Smart Inventory Intelligence.

Sentinel reads a live stream of inventory and order events, keeps exact stock figures even when
the feed is messy, projects a ship date for every order against its Customer Request Date (CRD),
and raises alerts days before a miss. An agent explains each problem from the data and drafts
purchase orders. **Nothing is ordered and no supplier email is sent until a planner approves.**

## Results (30 seeded runs, 6,609 orders; full report in [results/RESULTS.md](results/RESULTS.md))

| Question | Result |
|---|---|
| Are the numbers right? | **0 mismatches** in 172,800 checks against an independently written calculator; identical state under duplicates, junk and shuffled delivery (1,800/1,800) and on replay (90/90) |
| Does it catch late orders? | **100% recall** 3 days before CRD (precision 0.75); median warning **8 days** ahead vs 0 for a static ERP check |
| Does it catch feed problems? | 30/30 feed outages, 60/60 demand spikes, 89/89 hidden stock losses, 1,780/1,780 late supplier deliveries flagged before arrival |
| Is the agent trustworthy? | 5,400/5,400 briefings grounded (0 invented numbers in 76,838); 21,668/21,668 material claims correct |
| Do planners do better? | On-time **92.6% → 96.3%** (+3.8 pts, 95% CI +2.7 to +4.8, p < 0.001, 28/30 seeds); days late −63%; stock-out days −57%; **no significant change in spend or stock held** |

Reproduce every number with one command (about a minute):

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
| Does it read messages it has never seen? | First run on **120 messages written blind** (author saw only the definitions, never the code): **110 fully right**; ambiguity precision 0.91, recall 0.96; 5 clear messages questioned needlessly. All failures are listed. (An earlier blind set scored 101/120 on its first run; rules were fixed after it, so it no longer counts.) |
| Does it plan on a guess? | Planned date earlier than the real one: **1 of 100** messages vs **42 of 100** for a plain date parser |
| Does it change outcomes? | 30 plants, every delay notice written as text: on-time **95.6% (plain parser) → 96.5%** (+0.9 pts, 95% CI +0.1 to +1.7, p = 0.045, 20/30 seeds); stock-out days −3.9 (p < 0.001). Same as when suppliers send exact dates (96.3%). Asking makes the planned date 5x more accurate but adds no on-time gain beyond planning for the latest day |

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
.venv/Scripts/python -m pytest -q                    # 14 tests
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
see but cannot approve; the API returns 403).

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
- **Baselines:** (1) a static ERP check, net availability < 0; (2) an ablation that trusts
  supplier dates; (3) no system, where the planner learns at the CRD; and for outcomes, the
  company's existing reorder-point planner on the same seeds.
- **Statistics:** detection CIs bootstrap over seeds (orders inside a run are correlated); outcome
  differences are paired by seed, with bootstrap CIs and a sign-flip permutation test.
- **Limitations** are listed at the end of [results/RESULTS.md](results/RESULTS.md). The main one:
  the plant is our simulator, and no real planner has used the tool yet.

## Demo script (3 minutes)

See [DEMO.md](DEMO.md).
