# E6 – Supplier messages in plain language

Reproduce: `python -m evaluation.run_ambiguity --seeds 30` (3.6 s). Definitions of clear / ambiguous / no date: [docs/AMBIGUITY.md](../docs/AMBIGUITY.md).

**Success means:** every ambiguous message is caught (so nobody plans on a guess), clear messages are not questioned, the range Sentinel plans for contains the real date, and fewer orders ship late than when the same messages are read by a plain date parser.

## A. Hand-labelled messages

| Set | Who wrote it | n (ambiguous / clear / none) | Fully right | Ambiguity precision / recall | Asked when clear | Range contains the real date | Planned too early: Sentinel vs plain parser |
|---|---|---|---|---|---|---|---|
| development | us, while building the reader (not evidence) | 119 (51 / 62 / 6) | **119/119** | 1.00 / 1.00 | 0 | 51/51 | 0 vs 41 of 113 |
| blind 1 – first run | separate author, saw only the definitions | 120 (52 / 55 / 13) | **101/120** | 0.88 / 0.88 | 6 | 46/52 | 5 vs 42 of 107 |
| blind 1 – now | separate author, saw only the definitions | 120 (52 / 55 / 13) | **120/120** | 1.00 / 1.00 | 0 | 52/52 | 0 vs 38 of 107 |
| blind 2 – first run | a second separate author | 120 (51 / 54 / 15) | **110/120** | 0.91 / 0.96 | 5 | 49/51 | 1 vs 42 of 100 |

The **first run** on a blind set is the honest accuracy figure: it was scored once, before any change. Rules were then fixed for the failures it exposed, so later rows on that set are no longer blind. The plain parser is the same date reader without the ambiguity step: it takes the earliest reading and never asks.

<details><summary>blind 1: the 19 first-run failures</summary>

- PO 3390 dated 2 Nov - material ready, will deliver by thrusday -> ambiguous
- PO 7310 on track for delivery, truck loading tomorrow -> ambiguous
- Kindly note the shipment is delayed by a week. -> ambiguous
- Dear Sir, as discussed on call, revised delivery date is 11-Dec-2026. -> none
- 300 nos already delivered today. Balance qty 200 nos will come tomorrow. -> ambiguous
- dear sir greetings from Shree Ganesh Enterprises. we regret to inform the delay of 5 days in PO 1123 due to raw material shortage. regards -> ambiguous
- Will take a few days more, labour shortage -> none
- Need a couple of days sir, coating line down -> none
- 1-2 weeks more sir, imported part pending -> none
- 3/4 days late sir -> clear
- About a week more -> none
- Roughly by 29th -> clear
- Invoice no. SGE/224 dated 12 Nov attached for your reference. -> clear
- Kindly release payment due on 15th sir -> clear
- Credit note for the rejected lot will be issued by Friday -> clear
- Our office will be closed on 25 Dec for Christmas -> clear
- Please send the payment by month end to avoid hold -> ambiguous
- Received your PO 8812 dated 9 Dec, thank you. Will revert with delivery schedule. -> clear
- Meeting with your QA team on Wednesday at 11 am is fine for us -> clear

</details>

<details><summary>blind 2: the 10 first-run failures</summary>

- Hi team, our lathe machine broke down on Monday, so the lot of flanges is running 3 days late. Rest all ok. Thanks, Prakash -> ambiguous
- Dear Sir, The consignment will reach on 12.11.2026 without fail. Regards, Ramesh -> ambiguous
- Dear Sir, Earlier we had informed 2 Dec, but now the material will reach on 4 Dec. Sorry for the change. Regards -> ambiguous
- Dear Sir, Not on the 4th - the consignment will reach on 7th Dec, because of the bandh on Thursday our truck cannot move. Regards -> ambiguous
- Dear Sir, Due to shortage of raw material, the delivery will be delayed by 5 to 7 days. Regards, Purchase coordination -> clear
- Dear Sir, Delivery will be about a week from today. Regards -> none
- Dear Sir, Please find the quotation for the new die, valid till 30 Nov. Regards -> clear
- Hi sir, GST invoice will be corrected and resent tomorrow. -> clear
- Dear Sir, Please release the pending payment of Rs 2,45,000 by 5 Dec. Our bank details are unchanged. Regards -> clear
- Sir pls send C-form for Q2 by 20/12. Thx -> ambiguous

</details>

## B. Every message the simulated suppliers wrote

2525 messages across 30 plants (1022 delay notices, 862 'on track' notes, 641 answers to questions). Ambiguous: **642/642** caught, range contains the real date in **642/642**. Clear: **1883/1883** read exactly, 0 questioned needlessly. By type: approximate 110/110, dispatch_vs_arrival 43/43, missing_date 103/103, next_weekday 96/96, range 131/131, vague_period 159/159.

*Caveat:* the simulated suppliers' phrasing was written by the same team as the reader, so B shows the machinery works at scale; A's blind sets are the accuracy evidence.

## C. Does it change what ships on time?

Same 30 plants and faults as E5 (60 days each); every delay notice is written as text (60 % vaguely when a truthful vague phrasing exists), 20 % of on-time POs get an 'on track' note. The agent's purchase proposals are approved in every arm; only the handling of supplier text differs.

| How supplier text is handled | On-time % | Days late (total) | Stock-out material-days | PO spend | PO-days planned too early | Mean error of PO date (days) | Questions sent |
|---|---|---|---|---|---|---|---|
| Reference: supplier sends an exact date (no text) | 96.32 | 15.4 | 8.2 | 785027 | 1.4% | 0.06 | 0.0 |
| Ignore the text | 94.60 | 25.4 | 13.8 | 790449 | 100.0% | 4.80 | 0.0 |
| Plain parser: earliest reading, never asks | 95.65 | 19.9 | 11.2 | 794457 | 54.5% | 1.96 | 0.0 |
| Sentinel: latest reading, no questions | 96.38 | 15.3 | 8.1 | 791503 | 1.6% | 0.90 | 0.0 |
| **Sentinel: latest reading + approved question** | 96.52 | 14.6 | 7.4 | 793795 | 1.4% | 0.18 | 21.4 |

| Sentinel (latest + ask) vs | Metric | Mean difference [95% CI] | p | Seeds better / worse |
|---|---|---|---|---|
| Plain parser: earliest reading, never asks | on_time_pct | +0.87 [+0.07, +1.70] | 0.0454 | 20 / 6 |
| Plain parser: earliest reading, never asks | late_days_total | -5.23 [-11.40, -0.33] | 0.0604 | 16 / 6 |
| Plain parser: earliest reading, never asks | stockout_material_days | -3.87 [-6.10, -1.87] | 0.0008 | 18 / 7 |
| Plain parser: earliest reading, never asks | po_spend | -661.80 [-27612.42, +25242.92] | 0.9635 | 12 / 15 |
| Plain parser: earliest reading, never asks | po_days_planned_too_early | -0.53 [-0.56, -0.50] | 0.0000 | 30 / 0 |
| Ignore the text | on_time_pct | +1.92 [+1.00, +2.94] | 0.0006 | 22 / 6 |
| Ignore the text | late_days_total | -10.80 [-17.60, -5.13] | 0.0003 | 22 / 6 |
| Ignore the text | stockout_material_days | -6.47 [-9.07, -4.13] | 0.0000 | 24 / 2 |
| Ignore the text | po_spend | +3345.65 [-17429.86, +21591.78] | 0.7443 | 11 / 17 |
| Ignore the text | po_days_planned_too_early | -0.99 [-0.99, -0.98] | 0.0000 | 30 / 0 |
| Sentinel: latest reading, no questions | on_time_pct | +0.14 [-0.56, +0.82] | 0.6996 | 13 / 11 |
| Sentinel: latest reading, no questions | late_days_total | -0.63 [-3.17, +2.20] | 0.6912 | 13 / 9 |
| Sentinel: latest reading, no questions | stockout_material_days | -0.73 [-1.93, +0.57] | 0.2994 | 16 / 8 |
| Sentinel: latest reading, no questions | po_spend | +2291.76 [-18063.33, +22473.26] | 0.8287 | 13 / 14 |
| Sentinel: latest reading, no questions | po_days_planned_too_early | -0.00 [-0.00, +0.00] | 0.3206 | 16 / 10 |

**Reading the table.** When suppliers write dates vaguely, a plain date parser plans too early on 54.5% of the days a delayed PO is open, and on-time falls to 95.65% (exact dates: 96.32%). Sentinel plans for the latest reading and keeps 96.52%: +0.87 points vs the plain parser (95% CI +0.07 to +1.70, p = 0.045, better in 20 of 30 plants). The gain is modest because only delayed POs with a vague notice are affected. Asking the supplier makes the planned date about 5x more accurate (0.90 → 0.18 days of error), but on top of planning for the latest reading it does **not** measurably change on-time (+0.14 points, p = 0.70). Its value is a correct date for the planner, not extra on-time orders in this simulator.


In the simulator a supplier answers an approved question in 7.2 hours on average (drawn from 4-11 h); real suppliers may be slower, which would shrink the gain of asking towards the 'latest reading, no questions' row.

## Limits

- English only, rule-based, a fixed list of phrases (see docs/AMBIGUITY.md). Blind sets are 120 messages each; a few hundred messages cannot cover how every supplier writes.
- Blind authors were separate AI agents given only the definitions page, not real suppliers. Real messages would be messier.
- Outcome results come from our simulator, where suppliers always tell the truth and answer questions. A supplier who is wrong about their own date is not modelled.
- Planning for the latest reading is deliberately cautious: when the true date is early in the range, Sentinel may expedite or warn when it did not need to.