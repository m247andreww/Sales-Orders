---
name: new-sales-order
description: Turn a New Orders mailbox submission (neworders@managed.co.uk) into a validated sales order in the Sales Orders database. Use when asked to process, load, check or review a new order email, a signed PandaDoc proposal, or "the latest orders".
---

# New sales order: mailbox → database

Until automated ingestion exists (README roadmap step 1), this is the standard routine.
Follow every step; do not skip checks because an order looks routine.

## 1. Find and read the whole email

- Search `neworders@managed.co.uk` (Microsoft 365 connector, `mailboxOwnerEmail`).
- Read the **entire thread** including forwards and replies: later messages often change terms
  (e.g. a finance reply restricting credit terms). Note the RFC Message-ID — it is the idempotency key.
- List every attachment and what it is (signed order, customer PO, supplier quote, DD mandate,
  application form, FX evidence).

## 2. Map it to the submission format

Use `docs/data-model.md` → "How the New Orders email maps to the database".
- Column order in pricing tables is **not consistent** between submitters (Unit Sell and Unit Cost
  have been swapped). Map by header name, never by position.
- "Recurring" = billing periods. "Annual / Monthly" → `monthly` × 12. "Annual / Annual" → `annual` × 1.
  One-off → `one_off` × 1.
- Foreign-currency costs: record the rate in master data (`fx_rates`) with its source and date;
  the line references it. Never convert by hand.
- Sales tax recharged from a supplier → category `tax_pass_through`, sell = cost.
- Put the emailed totals in `stated_totals` exactly as typed.
- Money values as **strings** ("1195.00"), never bare numbers.

## 3. Keep real data out of git

Write real submissions to `data/` (git-ignored). Never commit them. Fixtures in `fixtures/` are synthetic.

## 4. Master data first

Customer, employees, suppliers and FX rates must exist. If any is missing, load it via
`sales-orders load-master-data` — confirm spelling against Xero before creating a customer or supplier.
Check the customer's credit terms in `sales.customer_credit_terms`; if the thread contains
non-standard terms, record them with reason and approver.

## 5. Load and review

```bash
sales-orders load-order data/<file>.json
```
Report to the CFO, in this order:
1. Every **exception** (errors first) and what it means commercially.
2. Every **pending check** and what evidence would pass it.
3. Anything in the email that the schema could not capture — this is a gap to fix, not to ignore.

Never move an order to `approved` on the CFO's behalf without explicit instruction.
