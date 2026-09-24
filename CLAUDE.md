# CLAUDE.md

Guidance for Claude (and any engineer) working in this repository.

## Project

Managed247 sales order system of record. PostgreSQL 16 + Python 3.11. This is the first part of a
much wider finance-systems project: **no quick fixes, no shortcuts** — best database and coding
practice every time, even for a "test" change.

## Non-negotiables

- Schema changes = a **new** migration pair in `migrations/sql/NNNN_name.{up,down}.sql` plus a revision
  in `migrations/versions/`. Never edit a migration that has run outside a test database.
- Enforce business rules in the database (constraints/triggers), then validate in Python as well.
- Money is `numeric`/`Decimal`. Never float — in SQL, Python, JSON fixtures or tests.
- Derived values (net cost/sell/margin) are generated columns; never store a computed total as input.
- Master data is never created implicitly by an order load. Unknown references fail the whole load.
- Every business table: `created_*`, `updated_*`, `row_version`, `sales.attach_standard_triggers(...)`.
- New exception rule → new `UNION ALL` branch in `sales.v_sales_order_exception` + a test that trips only it.
- Every exception rule is listed in `sales.exception_rule` with `blocks_approval` (a test enforces this).
- ARR refs follow processing: never block approval; report with `arr-outstanding`.
- New check rule → function in `src/sales_orders/checks.py` + `RULES` + a test.
- No real customer data in git (fixtures are synthetic; `.gitignore` blocks `/data/` and `*.eml`).
- Before pushing: `ruff check . && ruff format --check . && mypy && pytest` must all pass.
- Parameterised SQL only; dynamic identifiers via `psycopg.sql`.
- Views list their columns explicitly — never `SELECT *` / `t.*` (PostgreSQL freezes the list at creation).
- Behaviour must never depend on cluster-wide state (roles are shared across databases); use per-database settings.
- A test must be able to fail: no tautological assertions.

## Local database

```bash
service postgresql start
export SALES_ORDERS_TEST_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/postgres
pytest
```

## Working with the project owner (CFO)

These are the owner's stated preferences; follow them in every session.

- Act as an **advisor, not an assistant**: catch what they miss. Challenge assumptions first —
  never open with agreement. Give the uncomfortable answer first.
- When disagreeing: "I disagree because [reason]. The risk in your approach is [specific downside]."
- Tag assertions: **[certain]** hard evidence, **[likely]** strong inference, **[guessing]** filling gaps.
  Give an overall confidence rating for each answer.
- Assume no technical background: explain any request or instruction step by step.
- Review your own work and list what you would fix before anything is issued.
- Always build structure behind a request (database, routine, framework) and prefer reusable
  skills/ways of working.
- Prefer automated options over workarounds; avoid manual data downloads unless unavoidable.
- UK English. Cancellation communications go out as Johnathon from contract.admin@.
- Learn from the owner's language and preferences and record new ones here.

## Sources of truth (ADR 0003)

- SN refs: **AW SOs Register** (Google Sheet). Never generate or guess an SN; source it with
  `load-register` / `assign-sn`. The database never writes to AW SOs.
- ARR refs (TIL030, NAP008-26): the **ARR file**. GL codes: **Xero** chart of accounts.
- Order content, checks, approvals, credit terms: **this database**.
- **This database is the system of record (from 24 Sep 2026).** The earlier SQLite build in OneDrive
  `3 AW Filing/10. Claude/Sales Orders` is DEFUNCT: never read from, write to or sync it. Its rules
  are carried over below.
- AW SOs stays on a personal Google account (CFO decision; accepted risk). Keep the Register mirror current.

## Business rules adopted from the CFO's existing rulebook

- "Never guess the next SN. Orders enter unconfirmed; AW SOs assigns the SN."
- Term rule: if an order email states a clearly wrong term, the term starts on the email date and
  months are counted inclusively to the co-term date (Sep→May = 9).
- Microsoft CSP SKUs (CFQ7…) → GL 1233 Cloud Services: Office 365 / COS 2233.
- Salesperson = the account manager cc'd on the order email, not the ISAM who prepares it.
- House = finance-controlled account. When a salesperson leaves, ALL their accounts go to House until a new
  salesperson is allocated (`employee-leaves`, `allocate-account`). House/Legacy orders are E, never NN.
- Legacy = historic Register label only. Auto Renew / Cust Success = House (CFO).
- Ownership history: changes only when a different NAMED salesperson appears; House-labelled orders never
  end a salesperson's ownership. Build with `build-account-history`; it never overwrites existing history.
- Code edits: the formatter re-wraps lines, so a text replace can silently miss. Verify every edit landed
  (grep or a test) — twice this happened and only a test caught it.
- LAST_ORDER (…LO) rows are Register reversals: excluded from bookings and ARR.
- Read the FULL email thread (salesorders@ / neworders@) — the first email is not the order of
  record if it was amended.

## Domain vocabulary (from the New Orders mailbox)

- **GM** = gross margin (sell − cost). **Recurring** column = number of billing periods.
- "Annual / Monthly" = annual commitment, billed monthly (12 periods at a monthly price).
- "Annual / Annual" = annual commitment, billed annually (1 period at an annual price).
- **CSP** = Microsoft Cloud Solution Provider licences (SKUs `CFQ7…`), bought via a distributor.
- **GDAP** = Microsoft granular delegated admin relationship with the customer tenant.
- **PS** = professional services, priced from the rate card (e.g. `PS-L3-SSC-DAY`).
- **ISAM** = Internal Sales Account Manager — submits orders to New Orders; the Financial Controller processes.
- **SN** = sales order number from AW SOs: YY + 4-digit counter (SN260533); legacy 4-digit; LO / CA suffixes.
- **LO** = Last Order (reversal of the contract being renewed). **CA** = cancellation.
- **NN** = net new: the customer was NOT pre-existing when the salesperson was allocated to the account; **E** = it was (CFO). Needs `customer_account_allocation`; never infer allocations without CFO confirmation.
- **CVA** = Company Voluntary Arrangement (UK insolvency procedure; treat as high credit risk). Not to be confused with CVL (Creditors' Voluntary Liquidation).
