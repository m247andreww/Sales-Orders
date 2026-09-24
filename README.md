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
4. **Takes the SN from AW SOs**, never invents one, and codes every line to a Xero GL account.
5. **Raises checks** the order needs — Direct Debit mandate, GDAP, customer PO, supplier account,
   margin approval — and **blocks approval** until every check is resolved and no errors remain.
6. **Locks** the commercial content once approved. Changes after that need the order put on hold
   and re-validated.
7. **Keeps a register of credit terms** per customer over time, including non-standard terms
   with the reason and who approved them.
8. **Records who did what, when** — every change, permanently.
9. **Keeps ARR as a ledger** — every new, expansion, churn is a dated movement, so ARR on any date and
   the bridge between two dates are exact.

## Try the test order

Needs PostgreSQL 16 and Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # then edit the URL; export it, e.g.:
export SALES_ORDERS_DATABASE_URL=postgresql://user:pass@localhost:5432/sales_orders

sales-orders migrate                                        # build the schema
sales-orders load-master-data fixtures/test_master_data.json   # GL, products, ARR contracts, customers...
sales-orders load-register fixtures/test_register.csv --source fixture   # AW SOs Register -> SN refs
sales-orders load-order fixtures/test_order.json            # prints the order, SN, GL, checks, exceptions
SALES_ORDERS_ACTOR=test.finance@example.com sales-orders status SO-000001 validated --reason "finance review"
SALES_ORDERS_ACTOR=test.finance@example.com sales-orders check SO-000001 direct_debit_mandate passed --notes "mandate ref TEST"
sales-orders show SO-000001
sales-orders arr --as-of 2026-10-31                         # ARR by contract (after approval)
sales-orders arr-outstanding                                # processed recurring lines missing an ARR ref
sales-orders link-arr SN269001 3 TST001-26                  # attach ARR ref after processing
```

The test order is **synthetic** (no real customer data). It deliberately covers every pattern found in
real submissions: rate-card PS days, Microsoft CSP annual-commit/monthly-billed licences, USD supplier
costs with an FX rate, a 36-month connectivity service and a US sales-tax pass-through.

Expected result: SN269001 sourced from the Register, cost £10,770.99, sell £15,611.61, GM £4,840.62
(31.01%), MRR £605.33, every line GL-coded, no exceptions, five checks pending. After CFO approval the
ARR ledger shows £7,263.96.

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
| `src/sales_orders/register.py` | AW SOs Register importer (SN source) |
| `infra/` | Azure infrastructure as code (Bicep) |
| `docs/deployment.md` | Step-by-step production deployment |
| `docs/adr/` | Architecture decisions and why |

## Decisions

**Made (CFO, 2026-09-24)**

| # | Decision | Where enforced |
|---|---|---|
| 1 | No minimum GM. Loss-making lines/orders allowed **with a rationale** and CFO approval | migration 0003, `margin_approval` check |
| 1 | FX rates up to **28 days** old; emailed-vs-computed totals tolerance **±£0.05** | `sales.policy_setting` |
| 2 | Host on **Azure PostgreSQL, UK South** | `infra/main.bicep`, `docs/deployment.md` |
| 3 | **Only the CFO** approves orders, waives checks, approves losses, sets non-standard terms | migration 0003; personal Entra login required in production |
| 4 | SN refs sourced from **AW SOs**; GL codes, product database, ARR database | migration 0004 |

| A | This database is the system of record **from 24 Sep 2026**; the SQLite build is defunct | ADR 0003 |
| B | AW SOs stays on its current account: **accepted risk**, mitigated by the Register mirror | ADR 0003 |
| C | NN = net new customer, E = existing customer; mismatches are warnings | migration 0006 |
| D | ARR ref follows processing: never blocks; error after processing, reported daily | migration 0006, `arr-outstanding` |

**Still needed**

| # | Decision |
|---|---|
| 1 | Confirm the net-new window: 12 months (current practice) or strictly the first order (0) |
| 2 | Private networking vs office IP allow-list; where scheduled syncs run |

## Roadmap

1. **Ingestion** — read `neworders@` via Microsoft Graph, parse the pricing table, store PDFs'
   SharePoint links + fingerprints, and load orders automatically (idempotent on Message-ID).
2. **Master data sync** — customers and suppliers from Xero (`xero_contact_id` already in the schema);
   load the Managed Service Catalogue as a price list.
3. **Credit terms register** — load current standard and non-standard terms for all customers.
4. **Invoicing** — push approved orders to Xero as draft invoices / repeating invoices; record invoice IDs.
5. **Reporting** — Power BI (read-only role) over the summary and exception views; MRR/ARR.
6. **Approval roles** — restrict who can approve, waive and set terms.
