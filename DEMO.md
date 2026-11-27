# 3-minute demo

Open https://chatu.in (or run locally: `.venv/Scripts/python -m uvicorn sentinel.server:app --port 8000`).
Each visitor gets their own plant, so nothing a judge clicks affects anyone else. Start from
**Simulate a problem → Start over** (seed 7, day 4). Numbers below are what seed 7 shows.

| Time | Do | Say |
|---|---|---|
| 0:00 | Point at the headline and the four figures | "A planner usually finds out an order is late on the day it ships late. Sentinel projects every order's ship date against its customer date, continuously, from the event stream." |
| 0:20 | Press **+1 day** ten times (to day 14) | "Every figure comes from events. Duplicates and malformed events are ignored or set aside; the counts are under Signals." |
| 0:40 | **Simulate a problem → A supplier slips a week** | "A supplier just moved the delivery most orders are waiting on, a week back." |
| 0:50 | Point at the headline (7 orders late) and click a red row | "Seven orders that looked fine now miss their date, about two weeks ahead. Click one: the exact cause, Switch assembly on PO0042." |
| 1:15 | Point at the decision card | "The agent has already tested a fix: rush-order 95 Switch assemblies. Its what-if takes late orders from 7 to 0, and here is the cost. The supplier email is a draft." |
| 1:35 | **View as → Manager**, try **Approve** | "Managers see everything but cannot buy; the server refuses too." |
| 1:45 | **Planner**, **Approve purchase** | "The planner approves. Nothing was ordered until a person said yes." |
| 2:00 | **Simulate a problem → A supplier writes something vague** | "Real suppliers write 'running 11-13 days late' or 'next week'. A normal system takes one date and hopes. Sentinel shows what it could mean, plans for the latest day and drafts one question." Press **Send question**, then **+1 day**: "The supplier answered; the plan has the exact date. That's Problem 6, ambiguity, inside the inventory system." Optional: **Write one yourself** and let a judge type a message. |
| 2:25 | **Evidence** | "30 simulated plants with identical demand in every comparison: on-time from 93.4% to 96.7% (p < 0.001) for 0.75% more purchase spend. Simple rules reach a similar on-time rate but spend 1.5-9% more and hold more stock. A week ahead, 73% of our late flags are right." |
| 2:45 | | "And the limits: it's a simulated plant, the approvals were simulated, and no real planner has used it yet. All of it is written up, including every failure." |

## Questions judges are likely to ask

- **"If I flag every unstarted order 3 days out I also get 100% recall."** Yes, which is why we report 5, 7 and
  10 days. A week out, flagging every unstarted order is right 20% of the time; Sentinel's flags are right 73%.
- **"Is the gain just expediting?"** Yes, all of it (the agent without expediting equals the baseline). The point
  is *targeted* expediting: a blanket "rush every late PO" rule gets the same on-time rate for 9% more spend.
- **"Who wrote the blind test sets?"** Separate AI agents given only the definitions page, never the code. The
  first run on each set is frozen; set 3 was committed to git before it was scored (see results/AMBIGUITY.md).
- **"Planning for the latest day means you're usually too late?"** Until the supplier answers, yes, on purpose:
  a plan that is too late makes the planner act early; a plan that is too early makes an order miss. After the
  answer the planned date is within 0.18 days on average.
- **"Can I approve from curl?"** In the demo, yes, in your own sandbox: the role is a page switch. A real
  deployment takes the role from the company sign-in.
