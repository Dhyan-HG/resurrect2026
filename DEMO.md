# 3-minute demo

Start: `.venv/Scripts/python -m uvicorn sentinel.server:app --port 8000`, open http://localhost:8000,
press **Reset** (seed 7). Rehearsed on seed 7; the step-3 proposal appeared in 5 of 5 tries.

| Time | Do | Say |
|---|---|---|
| 0:00 | Point at the KPI row and the planner briefing | "A planner usually finds out an order is late on the day it ships late. Sentinel projects every order's ship date against its CRD, continuously." |
| 0:20 | Press **+1 day** a few times (to about day 14) | "Every number comes from the event stream. Duplicates and bad events are ignored or quarantined: see the counts top right." |
| 0:40 | **Inject → Supplier delay** | "A supplier just moved a delivery back a week." |
| 0:50 | Point at **Projected late** and the red rows | "Six orders that looked fine now miss their CRD, two weeks ahead. Click one…" (click a red row) "…the exact cause: waiting for Switch assembly on that PO." |
| 1:20 | Point at the **Approval queue** card | "The agent has already tested a fix: an expedited order of 90 pieces. Its what-if shows late orders going from 6 to 0. It costs this much. Here is the supplier email, as a draft." |
| 1:45 | Switch **View as → Manager**, try **Approve** | "Managers see the summary but cannot buy." (button disabled; the API also refuses) |
| 1:55 | Switch back to **Planner**, press **Approve PO** | "The planner approves. Late orders drop to zero. Nothing was sent until a human said yes." |
| 2:00 | **Simulate a problem → A supplier writes something vague** | "Real suppliers write 'next week' or '2-3 days late'. A normal system guesses one date. Sentinel shows what it could mean, plans for the latest day, and drafts one question." Press **Send question**, then **+6h**: "The supplier answered; the plan now has the exact date. That's Problem 6, ambiguity, inside our inventory system." Optional: **Write one yourself** and let a judge type a message. |
| 2:10 | **Inject → Feed outage**, then **+1 day** | "If the warehouse feed dies, Sentinel says so, and warns that numbers relying on it may be stale, instead of quietly showing old stock." |
| 2:30 | Open `results/RESULTS.md` / the results slide | "30 seeded runs: 0 calculation errors in 172,800 checks, every late order caught 3 days ahead with a median 8 days of warning, and on-time delivery from 92.6% to 96.3% with no extra spend (p < 0.001). One command reproduces all of it." |
| 2:50 | | "And the limits: it's a simulated plant, and no real planner has used it yet. Both are written up." |
