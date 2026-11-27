# E6 – Supplier messages in plain language

Reproduce: `python -m evaluation.run_ambiguity --seeds 30` (5.0 s). Definitions of clear / ambiguous / no date: [docs/AMBIGUITY.md](../docs/AMBIGUITY.md).

**Success means:** ambiguous messages are caught (nobody plans on a guess), clear ones are not questioned, the range Sentinel plans for contains the real date, and fewer orders ship late than when the same messages are read by a plain date parser. **The trade-off is stated, not hidden:** planning for the latest reading means Sentinel is rarely too early but often too late until the supplier answers.

## A. Hand-labelled messages

| Set | Written by | n (amb / clear / none) | Fully right | Ambiguity precision / recall | Asked when clear | Range contains the real date |
|---|---|---|---|---|---|---|
| development | us, while building the reader (not evidence) | 119 (51 / 62 / 6) | **119/119** | 1.00 / 1.00 | 0 | 51/51 |
| audit | the independent code auditor; used to test fixes (not blind) | 70 (21 / 45 / 4) | **69/70** | 1.00 / 0.95 | 0 | 20/21 |
| blind 1 – first run | separate AI agent, saw only the definitions | 120 (52 / 55 / 13) | **101/120** | 0.88 / 0.88 | 6 | 46/52 |
| blind 1 – now | separate AI agent, saw only the definitions | 120 (52 / 55 / 13) | **119/120** | 1.00 / 0.98 | 0 | 51/52 |
| blind 2 – first run | a second separate AI agent | 120 (51 / 54 / 15) | **110/120** | 0.91 / 0.96 | 5 | 49/51 |
| blind 2 – now | a second separate AI agent | 120 (51 / 54 / 15) | **120/120** | 1.00 / 1.00 | 0 | 51/51 |
| blind 3 – first run | a third separate AI agent; set committed before it was scored | 150 (60 / 70 / 20) | **135/150** | 0.88 / 0.97 | 8 | 58/60 |

The **first run** on a blind set is the honest accuracy figure. Rules were fixed after blind sets 1 and 2 and after the auditor's set, so their later rows are no longer blind. Blind set 3 was written after those fixes and committed to git before it was scored (commit recorded in `results/ambiguity_blind3_first_run.json`). Note: after review the numeric-date rule changed (dd/mm first, see docs/AMBIGUITY.md); one item in blind 1 and one in the audit set were labelled under the old rule and now count as failures.

**What each way of reading would plan for**, against the date the supplier meant (messages with a known date; current reader, so blind sets 1-2 are no longer blind here):

| Set | Reader | Too early | Exact | Too late | Mean error (days) |
|---|---|---|---|---|---|
| development | Sentinel (latest reading) | 0 | 74 | 39 | 1.05 |
| development | Plain parser (earliest reading) | 41 | 72 | 0 | 0.73 |
| development | Middle of the range | 4 | 82 | 27 | 0.45 |
| audit | Sentinel (latest reading) | 0 | 49 | 17 | 0.79 |
| audit | Plain parser (earliest reading) | 19 | 47 | 0 | 0.70 |
| audit | Middle of the range | 3 | 54 | 9 | 0.24 |
| blind 1 | Sentinel (latest reading) | 0 | 65 | 42 | 1.67 |
| blind 1 | Plain parser (earliest reading) | 37 | 69 | 1 | 1.16 |
| blind 1 | Middle of the range | 3 | 78 | 26 | 1.11 |
| blind 2 | Sentinel (latest reading) | 0 | 68 | 32 | 0.68 |
| blind 2 | Plain parser (earliest reading) | 41 | 59 | 0 | 0.73 |
| blind 2 | Middle of the range | 6 | 80 | 14 | 0.32 |
| blind 3 | Sentinel (latest reading) | 3 | 81 | 37 | 0.94 |
| blind 3 | Plain parser (earliest reading) | 42 | 78 | 1 | 0.60 |
| blind 3 | Middle of the range | 7 | 93 | 21 | 0.50 |

On clear messages all three agree. On ambiguous ones the plain parser is too early about as often as Sentinel is too late, and the middle of the range has the smallest average error. Sentinel accepts the larger error on purpose: being too early makes an order miss its date by surprise, being too late only makes the planner act sooner than needed, and the question to the supplier then removes the error.

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

<details><summary>blind 3: the 15 first-run failures</summary>

- material reach day aftr tmrw pls note -> clear
- Jan 5 dispatch & reach Jan 6 -> ambiguous
- Earlier we said Monday, now it will be Wednesday -> ambiguous
- machine down since 6th; we will ship and deliver on 11 Jan -> ambiguous
- Not tomorrow - day after tomorrow for sure -> ambiguous
- Sir kal delivery hoga, tension mat lo -> none
- 25 Nov - sorry, 26 Nov -> ambiguous
- Hello, The order will be ready and delivered next Tuesday. Regards -> none
- Hello, We will hand over the consignment to the courier on Thursday. Kind regards -> clear
- pls send updated drawings by tmrw -> ambiguous
- Hello, Please find attached our revised price list effective 1 January 2027. Thanks, Sales -> clear
- Can we do the vendor audit on 3rd Dec? -> clear
- Dear Sir, The TDS certificate for Q2 will be sent on Monday. Regards -> ambiguous
- Dear Purchase Team, Please confirm the PO amendment for the revised rate of Rs 45/kg by 08-01-2027. Regards -> clear
- ledger statement till 30/11 sent on mail -> ambiguous

</details>

## B. Every message the simulated suppliers wrote (a sanity check)

2549 messages across 30 plants (1020 delay notices, 926 'on track' notes, 603 answers to questions). Ambiguous: 606/606 caught, range contains the real date in 606/606. Clear: 1943/1943 read exactly. **This is close to 100% by construction:** the simulated suppliers only use phrasings from the same definitions the reader implements. It shows the plumbing works at scale, not that the reader is accurate; section A is the accuracy evidence.

## C. Does it change what ships on time?

Same 30 plants, faults and demand in every arm (60 days each); every delay notice is written as text (60 % vaguely when a truthful vague phrasing exists), 20 % of on-time POs get an 'on track' note. The agent's purchase proposals are approved in every arm; only the handling of supplier text differs. 'Planned too early / too late' is measured on the arrival day the engine actually plans with, on every day a delayed PO is open after its notice.

| How supplier text is handled | On-time % | Days late (total) | Stock-out material-days | PO spend | Planned too early | Planned too late | Mean error of planned arrival (days) | Questions sent |
|---|---|---|---|---|---|---|---|---|
| Reference: supplier sends an exact date (no text) | 96.66 | 13.6 | 8.4 | 766004 | 1.4% | 0.2% | 0.05 | 0.0 |
| Ignore the text | 94.96 | 22.8 | 12.5 | 764457 | 63.7% | 19.8% | 2.12 | 0.0 |
| Plain parser: earliest reading, never asks | 96.11 | 18.7 | 10.2 | 765188 | 39.0% | 7.7% | 1.07 | 0.0 |
| Middle of the range, never asks | 96.21 | 17.8 | 9.9 | 766004 | 21.9% | 17.1% | 0.74 | 0.0 |
| Sentinel: latest reading, no questions | 96.62 | 15.2 | 8.9 | 766229 | 1.3% | 38.3% | 0.85 | 0.0 |
| **Sentinel: latest reading + approved question** | 96.72 | 14.1 | 8.4 | 765770 | 1.4% | 5.4% | 0.18 | 20.1 |

| Sentinel (latest + ask) vs | Metric | Mean difference [95% CI] | p | Seeds better / worse |
|---|---|---|---|---|
| Plain parser: earliest reading, never asks | on_time_pct | +0.61 [+0.05, +1.19] | 0.0495 | 17 / 4 |
| Plain parser: earliest reading, never asks | late_days_total | -4.57 [-8.83, -0.87] | 0.0285 | 19 / 4 |
| Plain parser: earliest reading, never asks | stockout_material_days | -1.80 [-3.07, -0.50] | 0.0137 | 15 / 6 |
| Plain parser: earliest reading, never asks | po_spend | +581.99 [-439.70, +1634.41] | 0.2812 | 10 / 18 |
| Plain parser: earliest reading, never asks | po_days_planned_too_early | -0.38 [-0.41, -0.35] | 0.0000 | 30 / 0 |
| Plain parser: earliest reading, never asks | po_abs_err | -0.90 [-1.00, -0.80] | 0.0000 | 30 / 0 |
| Middle of the range, never asks | on_time_pct | +0.51 [-0.03, +1.05] | 0.0782 | 15 / 5 |
| Middle of the range, never asks | late_days_total | -3.63 [-7.60, -0.10] | 0.0680 | 16 / 7 |
| Middle of the range, never asks | stockout_material_days | -1.53 [-2.87, -0.27] | 0.0342 | 15 / 7 |
| Middle of the range, never asks | po_spend | -233.08 [-1640.63, +915.86] | 0.7815 | 14 / 13 |
| Middle of the range, never asks | po_days_planned_too_early | -0.21 [-0.23, -0.18] | 0.0000 | 30 / 0 |
| Middle of the range, never asks | po_abs_err | -0.57 [-0.66, -0.48] | 0.0000 | 30 / 0 |
| Ignore the text | on_time_pct | +1.76 [+1.03, +2.59] | 0.0001 | 22 / 4 |
| Ignore the text | late_days_total | -8.67 [-13.03, -4.57] | 0.0005 | 21 / 5 |
| Ignore the text | stockout_material_days | -4.13 [-5.80, -2.57] | 0.0001 | 22 / 4 |
| Ignore the text | po_spend | +1313.75 [-341.91, +3000.27] | 0.1429 | 8 / 21 |
| Ignore the text | po_days_planned_too_early | -0.62 [-0.64, -0.61] | 0.0000 | 30 / 0 |
| Ignore the text | po_abs_err | -1.94 [-2.03, -1.85] | 0.0000 | 30 / 0 |
| Sentinel: latest reading, no questions | on_time_pct | +0.10 [-0.38, +0.64] | 0.7154 | 10 / 11 |
| Sentinel: latest reading, no questions | late_days_total | -1.03 [-4.97, +2.87] | 0.6421 | 12 / 12 |
| Sentinel: latest reading, no questions | stockout_material_days | -0.53 [-1.90, +0.73] | 0.4684 | 11 / 10 |
| Sentinel: latest reading, no questions | po_spend | -458.28 [-2010.22, +831.95] | 0.5465 | 14 / 13 |
| Sentinel: latest reading, no questions | po_days_planned_too_early | +0.00 [-0.00, +0.00] | 0.4448 | 10 / 17 |
| Sentinel: latest reading, no questions | po_abs_err | -0.67 [-0.76, -0.59] | 0.0000 | 30 / 0 |

**Reading the table.** With vague supplier text, Sentinel ships 96.72% on time vs 96.11% for the earliest-reading parser (+0.61 points, 95% CI +0.05 to +1.19, p = 0.050) and 96.21% for the middle-of-range parser (+0.51, p = 0.078); with exact dates from the supplier it is 96.66%. Asking the supplier, on top of planning for the latest reading, makes the planned arrival more accurate (0.85 → 0.18 days of error) but changes on-time by +0.10 points (p = 0.72): its value is a correct date for the planner, not extra on-time orders in this simulator.

In the simulator a supplier answers an approved question in 7.1 hours on average (drawn from 4-11 h); real suppliers may be slower.

## Limits

- English only, rule-based, a fixed list of phrases (docs/AMBIGUITY.md). A few hundred labelled messages cannot cover how every supplier writes.
- Blind authors were separate AI agents given only the definitions page, not real suppliers, and one labeller per message: we did not measure agreement between labellers, so the definitions themselves are ours.
- Uncertainty is expressed as a range of days, not a probability.
- Outcome results come from our simulator, where suppliers tell the truth and answer questions.