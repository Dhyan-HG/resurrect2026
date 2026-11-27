# Sentinel – evaluation results

30 seeded runs × 60 days. Ground truth = the plant simulator. Runtime 2.3 s. Reproduce: `python -m evaluation.run_eval --seeds 30`.

Orders scored: **6609**, of which **496** really missed their CRD (7.5%) under the company's existing reorder-point planner.

## E1 – Does Sentinel flag the orders that will miss their CRD?

Prediction made at the end of the day **H days before the CRD**; positive = projected late. 95% CIs from a bootstrap over seeds.

| Detector | H | Precision | Recall | F1 | TP / FP / FN |
|---|---|---|---|---|---|
| Sentinel (time-phased + learned supplier delay) | 3 | 0.75 [0.71–0.79] | 1.00 [1.00–1.00] | 0.86 [0.83–0.88] | 496 / 164 / 0 |
| Sentinel, 'late' or 'at risk' | 3 | 0.66 [0.61–0.70] | 1.00 [1.00–1.00] | 0.79 [0.76–0.83] | 496 / 256 / 0 |
| Ablation: time-phased, trusts supplier dates | 3 | 1.00 [1.00–1.00] | 0.61 [0.53–0.70] | 0.76 [0.69–0.82] | 304 / 0 / 192 |
| Baseline: static net availability < 0 (ERP screen) | 3 | n/a [n/a–n/a] | 0.00 [0.00–0.00] | n/a [n/a–n/a] | 0 / 0 / 496 |
| Sentinel (time-phased + learned supplier delay) | 5 | 0.67 [0.60–0.72] | 0.76 [0.70–0.82] | 0.71 [0.66–0.75] | 376 / 189 / 120 |
| Sentinel, 'late' or 'at risk' | 5 | 0.36 [0.32–0.40] | 1.00 [0.99–1.00] | 0.53 [0.49–0.57] | 495 / 868 / 1 |
| Ablation: time-phased, trusts supplier dates | 5 | 1.00 [1.00–1.00] | 0.61 [0.53–0.70] | 0.76 [0.69–0.82] | 303 / 0 / 193 |
| Baseline: static net availability < 0 (ERP screen) | 5 | 0.33 [0.00–1.00] | 0.00 [0.00–0.01] | 0.01 [0.00–0.02] | 2 / 4 / 494 |

**Early warning** (truly late orders: days between the first 'late' flag and the CRD):

| Detector | Median days | Mean days [95% CI] | Flagged ≥ 3 days ahead |
|---|---|---|---|
| Sentinel | 8.0 | 7.57 [7.30–7.83] | 100.0% |
| Ablation (trusts supplier dates) | 7.0 | 6.20 [5.87–6.52] | 61.5% |
| Static net availability | 0.0 | 1.46 [1.14–1.81] | 13.9% |
| No system (learn at CRD) | 0.0 | 0.00 | 0.0% |

The static ERP check almost never fires in these runs because the existing planner keeps net availability (on hand + incoming - allocated) positive: the totals look fine while the *timing* fails. That gap is what the time-phased projection closes.

On-time orders that were *ever* flagged late at some point: Sentinel 5.8%, ablation 0.2%, static 1.7%.

## E2 – Alerts

- **Feed outages:** 30/30 detected; median latency 16.8 h (max 18.0 h; the silence timeout is 18.0 h, so this is the floor). False feed alerts: 2.
- **Demand spikes:** 60/60 detected on the day; 24 false alerts (precision 0.71).
- **Supplier delays:** 1780/1780 late POs flagged before the goods arrived (median 5.1 days ahead); 24 alerts on POs that were not late.
- **Arrival forecasts:** mean absolute error of a PO's arrival day, one day after it is placed: Sentinel 1.77 days [1.74–1.81] vs supplier's promised date 1.44 days [1.39–1.50] (n = 6748 POs); root-mean-square error 2.34 vs 2.73 days. Goods arrived *after* the forecast day for 28.9% of POs with Sentinel's risk-adjusted date vs 32.1% with the promised date. (The adjustment is deliberately pessimistic: it adds the supplier's average delay to every PO. That cuts late surprises and squared error but raises absolute error, because most POs arrive on time.)
- **Hidden stock loss:** 89/89 found, median 3.0 days after it happened (detection is bounded by when the next cycle count happens).

## E3 – Are the numbers right?

- On hand, incoming, allocated and net checked against an **independently written reference calculator** on 1800 daily snapshots (43200 material-days × 4 fields): **0 mismatches**.
- Faulty feed (1829 duplicates, 1232 junk events, shuffled arrivals) vs the same events delivered cleanly: **1800/1800** daily snapshots identical. 1232 events quarantined, 1829 duplicates ignored.
- Full replay in random order with extra duplicates: **90/90** identical final state.

## E4 – Can the agent be trusted?

- 5400 role briefings generated; **5400** passed the grounding check (0 ungrounded numbers out of 76838).
- Material claims (on hand / incoming / allocated / net) checked against the reference: **21668/21668** correct. Order CRD claims vs ground truth: **5314/5314** correct.
- Every purchase proposal waits for a human; in E5 the 'human' is simulated.

## E5 – Do planners do better with the agent?

Same seeds, same plant, same faults. *Baseline* = the existing reorder-point planner. *Agent* = baseline + Sentinel's proposals approved by the planner. Paired by seed; 95% bootstrap CI of the mean difference; p from a sign-flip permutation test.

| Policy | On-time % | Late orders | Days late (total) | Stock-out material-days | PO spend | Avg stock value | Proposals (approved) |
|---|---|---|---|---|---|---|---|
| Agent (all approved) | 96.32 | 8.0 | 15.4 | 8.2 | 785027 | 64885 | 9.0 (9.0) |
| Agent (planner approves ~50%) | 95.20 | 10.5 | 20.3 | 12.6 | 793172 | 64879 | 13.4 (7.3) |
| Agent, no expediting | 92.55 | 16.5 | 41.8 | 19.2 | 787512 | 65006 | 0.0 (0.0) |
| Agent, no supplier-delay learning | 94.79 | 11.4 | 24.9 | 11.8 | 788034 | 65709 | 6.4 (6.4) |
| Baseline | 92.55 | 16.5 | 41.8 | 19.2 | 787512 | 65006 | 0.0 (0.0) |

| Policy vs baseline | Metric | Mean difference [95% CI] | p | Seeds better / worse |
|---|---|---|---|---|
| Agent (all approved) | on_time_pct | +3.76 [+2.73, +4.83] | 0.0000 | 28 / 2 |
| Agent (all approved) | late_days_total | -26.40 [-35.93, -17.77] | 0.0000 | 28 / 1 |
| Agent (all approved) | stockout_material_days | -10.93 [-13.37, -8.70] | 0.0000 | 29 / 0 |
| Agent (all approved) | po_spend | -2484.56 [-23597.65, +18808.17] | 0.8198 | 17 / 13 |
| Agent (all approved) | avg_stock_value | -121.36 [-1562.98, +1403.38] | 0.8786 | 18 / 12 |
| Agent (planner approves ~50%) | on_time_pct | +2.65 [+1.41, +3.81] | 0.0003 | 24 / 6 |
| Agent (planner approves ~50%) | late_days_total | -21.50 [-31.50, -12.37] | 0.0001 | 25 / 5 |
| Agent (planner approves ~50%) | stockout_material_days | -6.60 [-9.67, -3.50] | 0.0003 | 22 / 6 |
| Agent (planner approves ~50%) | po_spend | +5660.00 [-15947.77, +27781.83] | 0.6131 | 13 / 17 |
| Agent (planner approves ~50%) | avg_stock_value | -127.30 [-1465.78, +1266.41] | 0.8628 | 17 / 13 |
| Agent, no expediting | on_time_pct | +0.00 [+0.00, +0.00] | 1.0000 | 0 / 0 |
| Agent, no expediting | late_days_total | +0.00 [+0.00, +0.00] | 1.0000 | 0 / 0 |
| Agent, no expediting | stockout_material_days | +0.00 [+0.00, +0.00] | 1.0000 | 0 / 0 |
| Agent, no expediting | po_spend | +0.00 [+0.00, +0.00] | 1.0000 | 0 / 0 |
| Agent, no expediting | avg_stock_value | +0.00 [+0.00, +0.00] | 1.0000 | 0 / 0 |
| Agent, no supplier-delay learning | on_time_pct | +2.23 [+0.78, +3.64] | 0.0062 | 23 / 7 |
| Agent, no supplier-delay learning | late_days_total | -16.93 [-27.70, -7.10] | 0.0028 | 22 / 7 |
| Agent, no supplier-delay learning | stockout_material_days | -7.40 [-10.17, -4.73] | 0.0000 | 24 / 6 |
| Agent, no supplier-delay learning | po_spend | +522.46 [-22386.49, +22779.45] | 0.9640 | 16 / 14 |
| Agent, no supplier-delay learning | avg_stock_value | +702.97 [-1071.00, +2599.85] | 0.4727 | 15 / 15 |

## Limitations (read before trusting the numbers)

- The plant is a simulator we wrote. It encodes our assumptions about suppliers, demand and stock loss; results transfer to a real plant only as far as those assumptions hold.
- Sentinel's allocation rule is the same rule the simulated plant uses. In a real plant, a mismatch between the documented rule and shop-floor practice would reduce accuracy.
- The 'human' approving proposals is simulated (approve all, or ~50 % at random). No real planner was tested.
- Supplier-delay learning needs history: early in a run, or for a new supplier, it falls back to a 1-day prior.
- Ship dates are whole days; production time is fixed at 2 days.
- 'Agent, no expediting' is identical to the baseline: the existing planner already covers every outright shortage, so all of the agent's gain comes from spotting slow supply early and expediting it. A plant with no expedite option would see no outcome gain (the early warnings in E1 would still apply).
- Precision of the 'late' flag is 0.67-0.75: about one flag in four is a false alarm, the price of assuming each supplier will be as late as it has been. Planners can filter by severity.