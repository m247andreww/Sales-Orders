---
name: credit-review
description: Credit & risk management for Managed247 clients - bureau alerts (Experian Business Express, Creditsafe), credit limit assessments, CFO credit decisions, Xero snapshots and Debt & Credit filing. Use when asked about a client's credit limit, a credit/rating alert, "the credit run", the daily credit email, a company to add to or remove from monitoring, or anything the Credit Limit Assessment Workings spreadsheet used to do.
---

# Credit review: bureau alert → limit → Xero → Debt & Credit

The database is the master (ADR 0005, migration 0014). The spreadsheet *Credit Limit Assessment
Workings* is superseded: never update it, never take a figure from it after go-live.

## The daily run does everything

`sales-orders credit-run` (scheduled daily) reads the ARR file and every new bureau alert, assesses
each company whose inputs changed, files the PDF + note in Xero and the email in Debt & Credit, and
emails the CFO. Re-running is always safe. To see the position: `sales-orders credit-status`.

## When the CFO gives a decision

The CFO replies to the summary, e.g. "set Acme at £12,000 because they pay by DD, review in 6 months".

1. Confirm the company exists: `sales-orders credit-status` (match the name exactly as listed).
2. The CFO must run it from their own login (approvals need it):
   `sales-orders credit-decide "<company>" 12000 --reason "<their words>" --review-by YYYY-MM-DD`
3. Never set a limit the CFO did not give; never invent a reason or a review date. If they gave no
   reason, ask for one (the database refuses a decision without it).

## Rules that never change

- A limit is applied automatically **only** inside risk appetite; everything else is the CFO's
  (`CREDIT_REVIEW_NEEDED`). Do not "fix" a review case by changing the policy or the allowance.
- Master data is never created from an alert. A company in an alert but not monitored is reported as
  `UNKNOWN_COMPANY`: ask the CFO "customer, prospect, supplier, or for information?" then add it to the
  `credit_subjects` master data (with `company_number`) and load it with `load-master-data`.
- Customers are matched to the ARR file by **ARR prefix** (first 3 letters of the ARR ref), never by
  name. A customer with ARR lines but no prefix is understated: fix the prefix.
- Experian's figure is the **Credit Limit** (not the Credit Rating).
- Xero's API cannot set the Xero credit-limit box; the summary lists limits to mirror by hand until
  the CFO decides otherwise (ADR 0005, D2).
- Real company data stays in `data/` (git-ignored). Fixtures and docs are synthetic or anonymised.

## Exceptions and what to do

`sales-orders credit-status` lists each one with its action. Errors need action now
(`CREDIT_REVIEW_NEEDED`, `ALERT_NOT_READ`, `FILING_FAILED`, `CUSTOMER_NO_XERO_CONTACT`); warnings are
reported in the summary. `ALERT_NOT_READ` means a bureau changed its email layout: send the email to
the developer and add a fixture + parser test before anything else.

## Changing a rule

Exposure per frequency, counted ARR statuses, adverse bands, VAT, rounding and appetite are data in
migration 0014. A change is a **new migration** plus a test, and an entry in ADR 0005; never an
UPDATE in production.
