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
- New check rule → function in `src/sales_orders/checks.py` + `RULES` + a test.
- No real customer data in git (fixtures are synthetic; `.gitignore` blocks `/data/` and `*.eml`).
- Before pushing: `ruff check . && ruff format --check . && mypy && pytest` must all pass.
- Parameterised SQL only; dynamic identifiers via `psycopg.sql`.

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

## Domain vocabulary (from the New Orders mailbox)

- **GM** = gross margin (sell − cost). **Recurring** column = number of billing periods.
- "Annual / Monthly" = annual commitment, billed monthly (12 periods at a monthly price).
- "Annual / Annual" = annual commitment, billed annually (1 period at an annual price).
- **CSP** = Microsoft Cloud Solution Provider licences (SKUs `CFQ7…`), bought via a distributor.
- **GDAP** = Microsoft granular delegated admin relationship with the customer tenant.
- **PS** = professional services, priced from the rate card (e.g. `PS-L3-SSC-DAY`).
- **ISAM** = Internal Sales Account Manager — submits orders to New Orders; the Financial Controller processes.
- **CVA** = Company Voluntary Arrangement (UK insolvency procedure; treat as high credit risk). Not to be confused with CVL (Creditors' Voluntary Liquidation).
