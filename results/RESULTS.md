# Sentinel – evaluation results

30 seeded runs × 60 days. Ground truth = the plant simulator. Reproduce: `python -m evaluation.run_eval --seeds 30` (runtime 3.9 s).

Orders scored: **6455**, of which **420** really missed their CRD (6.51%) under the company's existing reorder-point planner.

**How to read this report.** Some checks below are *evidence* (they could have failed and would tell you something about a real plant); others are *sanity checks* that pass largely because of how the simulator is built. Each section says which.

## E1 – Does Sentinel flag the orders that will miss their CRD, early enough to act?

Prediction made at the end of the day **H days before the CRD**; positive = projected late. 95% CIs from a bootstrap over seeds. Production takes 2 days, so at H = 3 an order that has not started is already close to late: **H = 5, 7 and 10 are the informative horizons.**

| Detector | H | Precision | Recall | F1 | TP / FP / FN |
|---|---|---|---|---|---|
| **Sentinel** (time-phased + learned supplier delay) | 3 | 0.73 [0.67–0.79] | 0.97 [0.95–0.99] | 0.84 | 409 / 149 / 11 |
| Sentinel without delay learning (still handles overdue POs) | 3 | 0.88 [0.84–0.91] | 0.94 [0.90–0.96] | 0.91 | 393 / 54 / 27 |
| Trusts every promised date (no learning, no overdue handling) | 3 | 1.00 [1.00–1.00] | 0.62 [0.53–0.71] | 0.77 | 261 / 0 / 159 |
| Naive: stock on hand only, ignores incoming POs | 3 | 0.64 [0.59–0.69] | 0.98 [0.96–0.99] | 0.78 | 410 / 228 / 10 |
| Naive: every order not yet in production | 3 | 0.62 [0.57–0.67] | 1.00 [1.00–1.00] | 0.77 | 420 / 255 / 0 |
| Sentinel, 'late' or 'at risk' | 3 | 0.62 [0.57–0.67] | 1.00 [1.00–1.00] | 0.77 | 420 / 255 / 0 |
| **Sentinel** (time-phased + learned supplier delay) | 5 | 0.69 [0.62–0.75] | 0.73 [0.65–0.80] | 0.71 | 308 / 139 / 112 |
| Sentinel without delay learning (still handles overdue POs) | 5 | 1.00 [1.00–1.00] | 0.62 [0.53–0.71] | 0.76 | 260 / 0 / 160 |
| Trusts every promised date (no learning, no overdue handling) | 5 | 1.00 [1.00–1.00] | 0.62 [0.53–0.71] | 0.76 | 260 / 0 / 160 |
| Naive: stock on hand only, ignores incoming POs | 5 | 0.33 [0.28–0.37] | 0.98 [0.96–1.00] | 0.49 | 412 / 844 / 8 |
| Naive: every order not yet in production | 5 | 0.32 [0.28–0.36] | 1.00 [1.00–1.00] | 0.49 | 420 / 883 / 0 |
| Sentinel, 'late' or 'at risk' | 5 | 0.36 [0.31–0.40] | 0.98 [0.96–0.99] | 0.53 | 411 / 732 / 9 |
| **Sentinel** (time-phased + learned supplier delay) | 7 | 0.73 [0.65–0.80] | 0.65 [0.57–0.73] | 0.69 | 275 / 102 / 145 |
| Sentinel without delay learning (still handles overdue POs) | 7 | 0.99 [0.97–1.00] | 0.56 [0.48–0.65] | 0.72 | 236 / 2 / 184 |
| Trusts every promised date (no learning, no overdue handling) | 7 | 0.99 [0.97–1.00] | 0.56 [0.48–0.65] | 0.72 | 236 / 2 / 184 |
| Naive: stock on hand only, ignores incoming POs | 7 | 0.22 [0.18–0.26] | 0.86 [0.82–0.89] | 0.35 | 360 / 1297 / 60 |
| Naive: every order not yet in production | 7 | 0.20 [0.16–0.23] | 0.88 [0.85–0.91] | 0.32 | 370 / 1492 / 50 |
| Sentinel, 'late' or 'at risk' | 7 | 0.40 [0.34–0.46] | 0.80 [0.75–0.84] | 0.54 | 334 / 491 / 86 |
| **Sentinel** (time-phased + learned supplier delay) | 10 | 0.73 [0.63–0.82] | 0.36 [0.28–0.44] | 0.48 | 152 / 57 / 268 |
| Sentinel without delay learning (still handles overdue POs) | 10 | 1.00 [1.00–1.00] | 0.26 [0.19–0.34] | 0.42 | 111 / 0 / 309 |
| Trusts every promised date (no learning, no overdue handling) | 10 | 1.00 [1.00–1.00] | 0.26 [0.19–0.34] | 0.42 | 111 / 0 / 309 |
| Naive: stock on hand only, ignores incoming POs | 10 | 0.18 [0.14–0.22] | 0.50 [0.43–0.58] | 0.26 | 212 / 994 / 208 |
| Naive: every order not yet in production | 10 | 0.14 [0.10–0.17] | 0.57 [0.51–0.63] | 0.22 | 240 / 1532 / 180 |
| Sentinel, 'late' or 'at risk' | 10 | 0.45 [0.37–0.52] | 0.46 [0.39–0.53] | 0.45 | 193 / 240 / 227 |

The static ERP-style check (net availability < 0) flags 1 order(s) in total: the existing planner keeps that total positive by design, so it is not a meaningful baseline here and is left out of the table.

**What this shows.** At 3 days, naive rules reach recall 1.0 too, so a high recall there proves little. Further ahead the difference is real: at 7 days 'every unstarted order' has precision 0.20, Sentinel 0.73 with recall 0.65. Learning each supplier's delay is a trade-off: without it precision is near 1.0 but recall falls (at 7 days 0.56 vs 0.65); at 3 days the version without learning has the better F1 (0.91 vs 0.84).

**Early warning** (truly late orders: days between the first 'late' flag and the CRD):

| Detector | Median days | Mean days [95% CI] | Flagged ≥ 3 days ahead |
|---|---|---|---|
| Sentinel | 8.0 | 7.88 [7.57–8.20] | 97.4% |
| Without delay learning | 7.0 | 6.80 [6.47–7.14] | 93.6% |
| Trusts promised dates | 7.0 | 6.49 [6.13–6.85] | 62.4% |
| No system (learn at the CRD) | 0.0 | 0.00 | 0.0% |

On-time orders that were *ever* flagged late at some point: Sentinel 4.6%, without learning 1.0%, trusting promised dates 0.3%.

**E1b – What if the shop floor does not follow the documented rule?** Sentinel assumes earliest-CRD-first; here the simulated plant builds oldest order first instead (668 of 6455 orders late).

| H | Precision | Recall | F1 |
|---|---|---|---|
| 3 | 0.78 | 0.89 | 0.83 |
| 5 | 0.71 | 0.61 | 0.66 |
| 7 | 0.67 | 0.41 | 0.51 |
| 10 | 0.54 | 0.16 | 0.25 |

## E2 – Alerts

- **Feed outages** (scored from Sentinel's own feed alert, sampled every 6 h): 29/30 detected; median 19.7 h after the feed went quiet (the silence timeout is 18.0 h, so that is the floor). False feed alerts: 0. *Sanity check:* any working timeout rule finds a 18-48 h outage.
- **Demand spikes:** 60/60 detected on the day; 19 false alerts (precision 0.76).
- **Supplier delays (1-7 days):** 1691/1691 late POs flagged before the goods arrived, 1586 at least a day before. When the supplier warned (866 POs) the median warning was 7.1 days; when it did not, 3.0 days, which is simply 'the PO is half a day overdue'. 29 alerts on POs that were not late. *Mostly a sanity check:* an overdue PO is late by definition.
- **Arrival forecasts:** mean absolute error of a PO's arrival day, one day after it is placed: Sentinel 1.61 days vs supplier's promised date 1.28 days (n = 6608); root-mean-square error 2.21 vs 2.53. Goods arrived *after* the forecast for 25.3% of POs vs 31.7% with the promised date. The adjustment is deliberately pessimistic: fewer late surprises, but a *higher* absolute error, because most POs arrive on time.
- **Hidden stock loss:** 77/86 found within the run, median 9.0 days after it happened. The plant counts two materials a day, chosen without knowing where stock went missing, so detection waits for the next routine count.

## E3 – Are the numbers right?

- On hand, incoming and allocated (and net, their sum) checked against a **separately written reference calculator** (same team; it re-derives the book from the clean events without sharing Sentinel's code) on 1800 daily snapshots × 24 materials: **0 mismatches**. It checks bookkeeping, not the allocation order or ship dates; E1b tests those against a plant that uses a different rule.
- Faulty feed (1797 duplicates, 1239 junk events, shuffled arrivals) vs the same events delivered cleanly: **1800/1800** daily snapshots identical. 1239 events quarantined, 1797 duplicates ignored.
- Full replay in random order with extra duplicates: **90/90** identical final state.

## E4 – Can the agent's words be trusted?

- 5400 role briefings: **5400** pass the grounding check. *Sanity check only:* the template text is built from the same facts it is checked against.
- Material claims (on hand / incoming / allocated / net) vs the reference calculator: **21668/21668** correct. Order CRD claims vs ground truth: **4624/4624**.
- **How strong is the check?** (the real question, since its job is to stop a language model's mistakes). One error was planted in a third of the briefings; the table shows how often each check rejected it. The strict check is the one Sentinel uses: a number must belong to the order, PO or material its sentence names, IDs must exist, and counts ('3 orders late', 'nothing is late') must match.

| Planted error | n | Strict check rejects | Earlier loose check rejected |
|---|---|---|---|
| A number changed by 1-3 | 1800 | **91%** | 74% |
| Two numbers in a sentence swapped | 1643 | **29%** | 12% |
| A false count ('nothing is late') | 1800 | **100%** | 29% |
| An order or PO ID that does not exist | 768 | **100%** | 0% |

A changed number slips through when the new value happens to be another true fact about the same thing; swapped numbers mostly slip through because both values are true facts of that entity. The optional Claude rewrite (`SENTINEL_LLM=1`) is shown only when it passes this check; it was **not** run in this evaluation (no API calls), so how often it would be rejected is not measured.

## E5 – Do planners do better with the agent?

Same seeds, same faults, **identical demand** in every arm (supplier lateness has its own random stream, so placing a PO never changes the orders that follow). Paired by seed; 95% bootstrap CI of the mean difference; p from a sign-flip permutation test.

| Policy | On-time % | Late orders | Days late (total) | Stock-out material-days | PO spend | Avg stock value | Proposals (approved) |
|---|---|---|---|---|---|---|---|
| Baseline: existing reorder-point planner | 93.44 | 14.0 | 34.3 | 17.1 | 760288 | 66597 | 0.0 (0.0) |
| Baseline + 4 days of safety stock (instead of 1) | 96.12 | 8.2 | 21.7 | 9.0 | 777150 | 68982 | 0.0 (0.0) |
| Baseline + rule: rush-reorder any PO that is overdue or slips | 96.50 | 7.5 | 16.8 | 8.5 | 836333 | 80926 | 51.4 (51.4) |
| **Sentinel agent** (all proposals approved) | 96.66 | 7.1 | 13.6 | 8.4 | 766004 | 65984 | 7.7 (7.7) |
| Sentinel agent, planner approves ~50% at random | 95.69 | 9.1 | 18.2 | 10.4 | 764845 | 65890 | 10.6 (5.9) |
| Sentinel agent, no expediting | 93.44 | 14.0 | 34.3 | 17.1 | 760288 | 66597 | 0.0 (0.0) |
| Sentinel agent, no supplier-delay learning | 96.39 | 7.6 | 15.6 | 9.6 | 764473 | 65972 | 5.5 (5.5) |

| Comparison | Metric | Mean difference [95% CI] | p | Seeds better / worse |
|---|---|---|---|---|
| Agent vs baseline | on_time_pct | +3.22 [+2.27, +4.17] | 0.0000 | 26 / 2 |
| Agent vs baseline | late_days_total | -20.63 [-27.67, -14.10] | 0.0000 | 26 / 3 |
| Agent vs baseline | stockout_material_days | -8.73 [-10.93, -6.70] | 0.0000 | 30 / 0 |
| Agent vs baseline | po_spend | +5715.85 [+3847.50, +7824.35] | 0.0000 | 4 / 26 |
| Agent vs baseline | avg_stock_value | -613.34 [-1374.62, +123.43] | 0.1243 | 18 / 12 |
| Agent vs rush-reorder rule | on_time_pct | +0.17 [-0.80, +1.11] | 0.7360 | 15 / 13 |
| Agent vs rush-reorder rule | late_days_total | -3.17 [-9.37, +2.90] | 0.3268 | 18 / 12 |
| Agent vs rush-reorder rule | stockout_material_days | -0.13 [-2.27, +2.07] | 0.9308 | 15 / 14 |
| Agent vs rush-reorder rule | po_spend | -70329.21 [-77613.64, -63649.95] | 0.0000 | 30 / 0 |
| Agent vs rush-reorder rule | avg_stock_value | -14941.96 [-16389.17, -13533.64] | 0.0000 | 30 / 0 |
| Agent vs extra safety stock | on_time_pct | +0.54 [-0.65, +1.75] | 0.3904 | 15 / 15 |
| Agent vs extra safety stock | late_days_total | -8.03 [-16.27, -0.10] | 0.0609 | 17 / 13 |
| Agent vs extra safety stock | stockout_material_days | -0.63 [-3.47, +2.20] | 0.6804 | 15 / 13 |
| Agent vs extra safety stock | po_spend | -11146.14 [-14144.91, -8121.89] | 0.0000 | 28 / 2 |
| Agent vs extra safety stock | avg_stock_value | -2998.52 [-3930.30, -2046.16] | 0.0000 | 28 / 2 |

**What this shows.** The agent lifts on-time delivery from 93.4% to 96.7% at +0.8% purchase spend. Two simple rules a planner could use without Sentinel reach a similar on-time rate (96.5% and 96.1%), but the rush-reorder rule spends +9% more than the agent and holds +23% more stock, and extra safety stock spends +1.5% more and holds +4.5% more stock. Sentinel's value is reaching that on-time rate with targeted purchases, not a higher ceiling.

## E6 – Supplier messages in plain language (linked Problem 6)

Reported separately in [AMBIGUITY.md](AMBIGUITY.md) (`python -m evaluation.run_ambiguity --seeds 30`). E1-E5 above use structured supplier dates.

## Limitations (read before trusting the numbers)

- The plant is a simulator we wrote. It encodes our assumptions about suppliers, demand and stock loss; results transfer to a real plant only as far as those assumptions hold.
- In E1-E5 the plant follows Sentinel's documented allocation rule; E1b shows what happens when it does not.
- The 'human' approving proposals is simulated (approve all, or ~50 % at random). No real planner was tested.
- All of the agent's gain comes from expediting slow supply early ('no expediting' equals the baseline).
- About one 'late' flag in four is a false alarm, the price of assuming each supplier will be as late as it has been; without that learning, flags are almost always right but fewer late orders are caught early.
- Ship dates are whole days; production time is fixed at 2 days.