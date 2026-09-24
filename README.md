# Sales Orders

Managed247's system of record for sales orders — replacing "signed PDF + email into
`neworders@managed.co.uk`" with a database that checks every order before it is processed.

**Stage: foundation.** The database, rules and a test order are built and tested.
Nothing is connected to the live mailbox, Xero or SharePoint yet (see [Roadmap](#roadmap)).

---

## What it does (plain English)

Every order becomes one record with its lines, documents and checks. The database itself:

1. **Calculates** cost, sell and margin for every line — nobody types a total, so totals can't be wrong.
2. **Reconciles** the totals typed in the submission email against its own calculation.
3. **Flags exceptions** — below-cost lines, low margin, sales tax marked up, stale FX rates,
   suppliers without an account, customers with restricted credit terms, missing signed order.
4. **Raises checks** the order needs — Direct Debit mandate, GDAP, customer PO, supplier account,
   margin approval — and **blocks approval** until every check is resolved and no errors remain.
5. **Locks** the commercial content once approved. Changes after that need the order put on hold
   and re-validated.
6. **Keeps a register of credit terms** per customer over time, including non-standard terms
   with the reason and who approved them.
7. **Records who did what, when** — every change, permanently.

## Try the test order

Needs PostgreSQL 16 and Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # then edit the URL; export it, e.g.:
export SALES_ORDERS_DATABASE_URL=postgresql://user:pass@localhost:5432/sales_orders

sales-orders migrate                                        # build the schema
sales-orders load-master-data fixtures/test_master_data.json
sales-orders load-order fixtures/test_order.json            # prints the order, checks, exceptions
sales-orders status SO-000001 validated --reason "finance review"
sales-orders check SO-000001 direct_debit_mandate passed --by test.finance@example.com --notes "mandate ref TEST"
sales-orders show SO-000001
```

The test order is **synthetic** (no real customer data). It deliberately covers every pattern found in
real submissions: rate-card PS days, Microsoft CSP annual-commit/monthly-billed licences, USD supplier
costs with an FX rate, a 36-month connectivity service and a US sales-tax pass-through.

Expected result: cost £10,770.99, sell £15,611.61, GM £4,840.62 (31.01%), MRR £605.33, no exceptions,
five checks pending.

## Development

```bash
ruff check . && ruff format --check .   # lint + format
mypy                                    # strict typing
pytest                                  # real PostgreSQL; set SALES_ORDERS_TEST_ADMIN_URL if not local default
```

CI runs all three on every push (`.github/workflows/ci.yml`).

| Path | What |
|---|---|
| `migrations/sql/` | The schema, as reviewed SQL (source of truth) |
| `src/sales_orders/models.py` | Validated input formats |
| `src/sales_orders/service.py` | The only code that writes business data |
| `src/sales_orders/checks.py` | Rules deciding which checks an order needs |
| `db/bootstrap/` | Database roles and least-privilege grants |
| `docs/data-model.md` | ERD, email-to-database mapping, controls, exception rules |
| `docs/adr/` | Architecture decisions and why |

## Decisions needed (CFO)

| # | Decision | Current placeholder |
|---|---|---|
| 1 | Minimum order gross margin % before approval is needed | 20% |
| 2 | Maximum FX rate age | 7 days |
| 3 | Tolerance between emailed and computed totals | £0.05 |
| 4 | Production hosting | Proposed: Azure Database for PostgreSQL (UK) |
| 5 | Who may approve orders / waive checks / set non-standard terms | Not yet enforced by role |

Policy values live in `sales.policy_setting` and change by migration (audited), not by code.

## Roadmap

1. **Ingestion** — read `neworders@` via Microsoft Graph, parse the pricing table, store PDFs'
   SharePoint links + fingerprints, and load orders automatically (idempotent on Message-ID).
2. **Master data sync** — customers and suppliers from Xero (`xero_contact_id` already in the schema);
   load the Managed Service Catalogue as a price list.
3. **Credit terms register** — load current standard and non-standard terms for all customers.
4. **Invoicing** — push approved orders to Xero as draft invoices / repeating invoices; record invoice IDs.
5. **Reporting** — Power BI (read-only role) over the summary and exception views; MRR/ARR.
6. **Approval roles** — restrict who can approve, waive and set terms.
