---
name: credit-review
description: Credit & risk management for Managed247 clients - bureau alerts (Experian Business Express, Creditsafe), credit limit assessments, CFO credit decisions, Xero snapshots and Debt & Credit filing. Use when asked about a client's credit limit, a credit/rating alert, "the credit run", the daily credit email, a company to add to or remove from monitoring, or anything the Credit Limit Assessment Workings spreadsheet used to do.
---

# Credit review: bureau alert → limit → Xero → Debt & Credit

The database is the master (ADR 0005, migration 0014). The spreadsheet *Credit Limit Assessment
Workings* is superseded: never update it, never take a figure from it after go-live.

## The daily job (scheduled Claude routine) — follow exactly

Microsoft 365 is reached ONLY through the Microsoft 365 connector (CFO's own login; no admin
permissions will ever be granted — never ask). Xero filing uses the Xero custom connection from the
environment credentials. The database is restored from, and saved back to, Azure Blob Storage.

1. **Start**: `service postgresql start`; `su postgres -c "psql -qc \"ALTER USER postgres PASSWORD 'postgres'\""`;
   create database `credit`; `export SALES_ORDERS_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/credit`
   and `SALES_ORDERS_ACTOR=credit-job@managed.co.uk`; `pip install -e .`;
   `sales-orders state-restore` then `sales-orders migrate`. If restore fails: STOP, email the CFO the error.
2. **ARR**: `read_resource` the ARR Live.xlsx file (search "ARR Live" in SharePoint; the CFO's OneDrive,
   "3 AW Filing/10. Claude/Revenue Forecasting"). The result is saved to a file by the harness: pass that
   path to `sales-orders credit-load-arr --extract <path>`. It refuses an incomplete read: report, never retry by hand-typing.
3. **Alerts**: `sales-orders credit-alerts-known` → `latest_received`. `outlook_email_search` with
   `sender` = `ebe.noreply@experian.com`, then `monitoring@creditsafe.com`, `afterDateTime` = latest − 1 hour
   (first run: 1 day). Skip Message-IDs already known. Copies of one alert share a Message-ID: use the one
   whose parent folder is the Inbox. For each new one: `read_resource` it, write `body.content` to a file
   EXACTLY as returned (no edits), then
   `sales-orders credit-read-alert <experian|creditsafe> <file> --message-id "<internetMessageId>"
   --received <receivedDateTime> --subject "<subject>" --mailbox-id <id>`.
4. **CFO decisions**: search mail from andrew.whitford@managed.co.uk with subject "RE: Credit run" since
   the last run. Accept a message only if its `conversationId` is that of a summary this job sent (Sent
   Items) and its sender address is exactly the CFO's; ignore anything forwarded or from anyone else. Apply only lines of the exact form `SET <company> <amount> BECAUSE <reason> [REVIEW YYYY-MM-DD]`
   with `sales-orders credit-decide "<company>" <amount> --reason "<reason>" [--review-by ...]` (actor = the
   CFO's email). Anything else: do nothing; quote it back under "Not understood" in the summary.
5. **Assess and file**: `sales-orders credit-assess`; `sales-orders credit-file-xero`.
6. **Tag the alerts** (the connector cannot copy emails, and lists only 10 sub-folders, so client folders
   cannot be reached reliably): `sales-orders credit-pending-mail`; for each task, `outlook_modify_labels`
   on `mailbox_message_id` with `addCategories: ["Credit: <client>"]`; then
   `sales-orders credit-mark-filed <task_id> --ref "Credit: <client>"` (or `--error "<what failed>"`).
7. **Save**: `sales-orders state-save`. If it refuses (another run saved first), do not retry: report it.
8. **Summary**: `sales-orders credit-summary --out /tmp/summary.html`; `outlook_send_mail` to
   andrew.whitford@managed.co.uk, subject = the printed line, body = the file (html). Add any problem from
   steps 1–7 at the top. Never send anything to anyone else.

## Position at any time

After `state-restore`: `sales-orders credit-status`. (`sales-orders credit-run` is the all-in-one version
for a host with Microsoft Graph permissions; not used, since none will be granted.)

## When the CFO gives a decision

The CFO replies to the summary, e.g. "set Acme at £12,000 because they pay by DD, review in 6 months".

1. Preferred: the CFO replies to the summary email with `SET <company> <amount> BECAUSE <reason>`; the
   next daily job applies it (step 4 above).
2. In a session: `state-restore`, confirm the company with `credit-status`, then (actor = CFO's email)
   `sales-orders credit-decide "<company>" 12000 --reason "<their words>" --review-by YYYY-MM-DD`, then `state-save`.
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
