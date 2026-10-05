# Open actions and requirements (credit automation)

Kept current by Claude every turn. Newest state wins.

## Your actions (now)

1. **Decide the 4 waiting on the Credit Desk**: Tilbury Douglas, Rascal Solutions, QPR, Napier Parking. Creditsafe
   figures for Tilbury (£100,000) and QPR (£3,500) confirmed by the CFO from the portal, 5 Oct 2026. Tilbury's need is
   driven by one tri-annual ARR line (£664,667 a year): see decision D1.
2. **Cardano and Pragmatic** review dates: reply with the dates you want.

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
| First figures box on the Credit Desk (18 clients missing a bureau figure; saved figures reassess the client at once) | Claude | Done 5 Oct 2026 |
| First figures entered for all 18 clients (14 settled, 4 to decide) | CFO | Done 5 Oct 2026 |
| Overlapping decision jobs applied some presses 2-3 times (same values): each press now applied once (migration 0024) | Claude | Done 5 Oct 2026 |
| Creditsafe Safe numbers recorded: Tilbury UK00067419, QPR UK00005232 | Claude | Done 5 Oct 2026 |
| Matrix SCM limit £0 (former customer; owes £42k early-termination charges): collection, not credit | CFO | Open |
| Stephensons (MK) Trust: Experian figure still from Jan 2025 (only Creditsafe was entered) | CFO | Open |
| Outlook rule: alerts to Inbox / Credit alerts, marked read | CFO | Done 2 Oct 2026 |
| Each client's alerts filed as PDFs on its Xero contact (9 filed; automatic from now on) | Claude | Done 2 Oct 2026 |
| PandaDoc values: alternatives, fee fields and monthly-vs-term totals make sums unreliable; tidy in PandaDoc or agree a rule | CFO + Claude | Open |
| Match 'BBA CSP Licensing' and 'Exchange Ilford' documents to their customers | Claude | Open |
| McGill and Riverside corrected in Creditsafe | CFO | Done 5 Oct 2026 |
| Decisions 5 Oct: Motive £191,000; Metropolitan Gaming £100,000, Tinopolis £70,600, Fuelsoft £55,600 (applied by Claude after the job's save was blocked); all review 12 Oct | CFO / Claude | Done 5 Oct 2026 |
| Decision job reordered: save before Xero filing | Claude | Done 5 Oct 2026 |
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

- 5 Oct 2026 (CFO): "build first figures": the CFO types bureau limits read from the portals on the Credit Desk;
  saved as dated bureau readings that the next alert supersedes (migration 0023).

- 5 Oct 2026 (CFO): decision entry = accept the recommended amount OR choose another; either way a reason and a
  review / follow-up date are required (page, code and database).

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
