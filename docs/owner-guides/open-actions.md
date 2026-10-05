# Open actions and requirements (credit automation)

Kept current by Claude every turn. Newest state wins.

## Your actions (now)

1. **Decide Motive** on the Credit Desk (owes £199,158, all overdue), then the other 7.
2. **Tilbury Douglas**: its committed ARR fell from £1.72m to £1.21m a year on 5 Oct (a £508,800 line is now
   Cancelled). Worth knowing given it owes £156k with no limit yet.

## Still open

| Item | Owner | Status |
|---|---|---|
| Azure storage, Xero link, ARR "Credit Extract" tab, Claude settings | CFO | Done 2 Oct 2026 |
| First live run (saved; ARR 1,225 lines; 4 alerts; 10 decisions waiting; 27 limits applied) | Claude | Done 2 Oct 2026 13:51 |
| Credit Desk page, attention list grouped by type | Claude | Done 2 Oct 2026 |
| Weekday 07:45 run switched on (first run Mon 5 Oct; Claude checks it at 07:55) | Claude | Done 2 Oct 2026 |
| 46 clients linked to Xero contacts and company numbers (from Xero, PandaDoc, alerts, web) | Claude | Done 2 Oct 2026 |
| 30 credit PDFs and notes filed on Xero contacts | Claude | Done 2 Oct 2026 |
| Newly added clients in Experian and Creditsafe | CFO | Done 2 Oct 2026 |
| First figures for newly monitored clients: check Monday's run; if none, add a one-off "first figures" box to the Credit Desk | Claude | Mon 5 Oct |
| Outlook rule: alerts to Inbox / Credit alerts, marked read | CFO | Done 2 Oct 2026 |
| Each client's alerts filed as PDFs on its Xero contact (9 filed; automatic from now on) | Claude | Done 2 Oct 2026 |
| PandaDoc values: alternatives, fee fields and monthly-vs-term totals make sums unreliable; tidy in PandaDoc or agree a rule | CFO + Claude | Open |
| Match 'BBA CSP Licensing' and 'Exchange Ilford' documents to their customers | Claude | Open |
| McGill and Riverside corrected in Creditsafe | CFO | Done 5 Oct 2026 |
| Monday run read Xero invoices and PandaDoc itself, no approvals needed | Claude | Done 5 Oct 2026 |
| Broadwick number corrected to 12136501; Matrix SCM added as client | Claude | Done 5 Oct 2026 |
| Daily job asked the CFO questions the systems answer: skill corrected (step 7b) | Claude | Done 5 Oct 2026 |
| McGill display name still says "Services" | Claude | Next change |
| Family BS: no Companies House number, so it needs its bureau reference to be set up | Claude | Open |
| Company numbers found for 22 of 27 unmonitored customers; 15 written into Xero (certain only) | Claude | Done 2 Oct 2026 |
| Numbers still open: PrimeSys (two numbers conflict), The Mall Maidstone, Vivantio, Clearlake (Irish), Family BS | Claude | Open |
| Review dates on Cardano and Pragmatic cleared | Claude | Done 2 Oct 2026 |
| Interserve excluded from monitoring (in administration; CFO dealing with administrators) | CFO | Done 2 Oct 2026 |
| Alert companies classified from Xero: FEI Foods, Weil Gotshal = customers; Westcon, Nuco, Shoreditch Design = suppliers; Nlighten = information | Claude | Done 2 Oct 2026 |
| First Steps Care: possible Companies House strike-off proposal | Claude to confirm | Open |
| Confirm client list and ARR matches (Analysis Mason sheets AMA/ANA, QPR Trust, Fronius) | CFO | Open |
| Which spreadsheet limits to keep as your decisions (e.g. three £100k, McGill £300k) | CFO | Open |
| First real decision on the page, checked within 5 minutes | CFO + Claude | After client list confirmed |
| D1 multi-year invoices: one year (built) or whole invoice | CFO | Open |
| D2 Xero credit-limit box: copy by hand from the page, or stop using it | CFO | Open |
| D3 "Order" counts as a commitment; cancelled/old renewals do not | CFO | Open (built as stated) |
| D4 Experian bands that always come to you: High, Maximum, Serious Adverse | CFO | Open (built as stated) |
| D5 One-off allowance for a new client: £5,000 | CFO | Open (built as stated) |

## Requirements recorded

- 2 Oct 2026 (CFO): daily report shows every customer: Xero invoices (not yet due, overdue) and in-progress
  PandaDoc, against the credit limit. Built: headroom on what is owed; "if it signs" adds the customer's
  largest unsigned PandaDoc (+VAT) only, because PandaDoc totals include alternatives and fee fields.

- 2 Oct 2026 (CFO): amounts in outputs are whole pounds (calculations keep pence).
- 2 Oct 2026 (CFO): the daily report scans for customers with no credit limit through monitoring
  (Credit Desk section; "I've added it" sets the client up).

- 2 Oct 2026 (CFO): sweep credit emails out of the inbox. Claude cannot (connector permissions are all
  read-only); the CFO's one Outlook rule does it. Per-client Debt & Credit folder copies are not possible
  without per-client rules; proposed replacement: attach the alert to the Xero contact.

- 2 Oct 2026 (CFO): a decision accepted or set on the Credit Desk is applied **immediately** (limit,
  Xero PDF and note), not at the next morning's run. Design: the page starts the credit job at once via
  the Claude Code Remote connector; done within about 5 minutes.
