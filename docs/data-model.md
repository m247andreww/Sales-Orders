# Data model

The system of record for Managed247 sales orders. PostgreSQL 16, schema `sales`
(business data) and `audit` (change log). Source of truth is the SQL in
[`migrations/sql/`](../migrations/sql/); this page explains it.

## Entity relationships

```mermaid
erDiagram
    customer ||--o{ customer_credit_terms : "terms over time"
    customer ||--o{ customer_contact : has
    customer ||--o{ customer_m365_tenant : owns
    customer ||--o{ sales_order : places
    employee ||--o{ sales_order : "submits / manages"
    source_email ||--o{ sales_order : "arrived in"
    source_email ||--o{ document : attached
    sales_order ||--|{ sales_order_line : contains
    sales_order ||--o{ sales_order_document : evidenced_by
    document ||--o{ sales_order_document : ""
    sales_order ||--o{ sales_order_check : "must pass"
    sales_order ||--o{ sales_order_status_history : "moved through"
    supplier ||--o{ sales_order_line : supplies
    supplier ||--o{ supplier_quote : issues
    supplier_quote ||--o{ sales_order_line : prices
    fx_rate ||--o{ sales_order_line : converts
    product ||--o{ sales_order_line : "catalogue item"
    price_list ||--o{ price_list_item : lists
    product ||--o{ price_list_item : ""
```

## How the New Orders email maps to the database

Observed from real submissions to `neworders@managed.co.uk` (July–September 2026).

| In the email | Where it goes | Notes |
|---|---|---|
| Message-ID header | `source_email.internet_message_id` | **Idempotency key**: the same email can never create two orders |
| Subject, sender, received time | `source_email` | |
| "Please process this new order for – *X*" | `sales_order.title` | |
| "new" / "(additional)" | `sales_order.order_type_code` | `new`, `additional`, `renewal`, `amendment` |
| Signed Order – Attached (1) | `document` (type `signed_order`) + `sales_order_document` | File stays in SharePoint; DB holds URI + SHA-256 fingerprint |
| Customer PO ref / attachment | `sales_order.customer_po_reference` + `document` (`customer_po`) | Raises a `customer_po_received` check |
| Supplier Quote – Attached | `supplier_quote` + `document` (`supplier_quote`) | Linked per line |
| "Pricing as per Managed Service Catalogue 2026" | `sales_order.price_list_id` | Catalogue held in `price_list` / `price_list_item` |
| Billing Cycle – "Annual / Monthly" | per line: `billing_frequency_code` + `billing_periods` | See "Recurring" below |
| Direct Debit signed / GDAP completed | `sales_order_check` rows | Evidence document optional |
| Tenant ID, primary domain | `customer_m365_tenant` | Owned by the customer, referenced by the order |
| Admin contact | `customer_contact` | |
| USD rate + XE screenshot | `fx_rate` (+ `fx_evidence` document) | Lines reference the rate; nobody types a rate onto a line |
| Pricing table: SKU, Description | `sales_order_line.sku`, `.description` | SKU matched to `product` if it exists |
| QTY | `.quantity` | |
| **Recurring** | `.billing_periods` | 1 = one-off *or* a single annual period; 12 = annual commit billed monthly; 36 = 36-month term |
| Cost USD / Unit Cost | `.unit_cost_in_cost_currency` (+ `.cost_currency_code`) | GBP unit cost is **computed**: `round(cost × fx, 4)` |
| Unit Sell | `.unit_sell` | Per billing period, order currency |
| Net Cost / Net Sell / Net GM | **computed** columns `net_cost`, `net_sell`, `gross_margin` | Cannot be typed in. The emailed totals go to `stated_*` and are reconciled |
| Supplier (+ quote ref) | `.supplier_id`, `.supplier_quote_id` | Internal PS days use the `is_internal` supplier |
| "Please invoice ASAP" | `sales_order.is_expedited` | |
| "0% margin on CSP" | `sales_order.margin_exception_reason` | Required for below-policy margin |

### Arithmetic

```
unit_cost    = round(unit_cost_in_cost_currency × fx_rate, 4)
net_cost     = round(quantity × billing_periods × unit_cost, 2)
net_sell     = round(quantity × billing_periods × unit_sell, 2)
gross_margin = net_sell − net_cost
monthly recurring (views) = quantity × unit price ÷ months_per_period
```
All amounts are **net of UK VAT**; VAT is applied at invoicing (Xero).

## Workflow

```mermaid
stateDiagram-v2
    [*] --> received
    received --> validated
    validated --> received : returned for correction
    validated --> approved : gate — no error exceptions, all checks resolved
    approved --> provisioning
    provisioning --> invoiced
    invoiced --> closed
    received --> on_hold
    validated --> on_hold
    approved --> on_hold
    provisioning --> on_hold
    on_hold --> received : must be re-validated
    received --> cancelled
    validated --> cancelled
    approved --> cancelled
    on_hold --> cancelled
```

From `approved` onwards, lines and commercial header fields are **locked by the database**.
Every transition is recorded in `sales_order_status_history` with who and why.

## Controls enforced by the database

| Control | Mechanism |
|---|---|
| No duplicate orders from the same email | `UNIQUE (source_email_id, source_sequence)` |
| Derived money can't be edited | `GENERATED ALWAYS` columns |
| FX rate must exist, match currencies, and is snapshotted | trigger `validate_line` |
| One-off lines have exactly one period | `CHECK` |
| Supplier quote belongs to the line's supplier | trigger `validate_line` |
| Only permitted status transitions | `order_status_transition` + trigger |
| Approval gate | trigger: CFO only (personal login in production); blocks while error exceptions or unresolved checks exist |
| Frozen after approval | triggers `validate_line`, `lock_commercials` |
| Credit terms periods never overlap | `EXCLUDE USING gist` |
| Non-standard terms need reason + approver | `CHECK` |
| Waived checks need a note | `CHECK` |
| Full audit trail, append-only | `audit.change_log` + triggers; UPDATE/DELETE blocked |
| App cannot delete anything | role grants (`db/bootstrap/grants.sql`) |

## Exception rules (`sales.v_sales_order_exception_all`)

| Rule | Severity | Meaning |
|---|---|---|
| `NO_LINES` | error | Order has no lines |
| `LOSS_LINE_NO_RATIONALE` | error | A line sells below cost with no line or order rationale |
| `ORDER_LOSS_NO_RATIONALE` | error | The order sells below cost overall with no order-level rationale |
| `TAX_LINE_MARKED_UP` | error | A tax pass-through line is sold at a price other than cost |
| `STATED_TOTAL_MISMATCH` | error | Emailed totals differ from computed by more than `stated_total_tolerance` |
| `MISSING_SIGNED_ORDER` | error | No signed order document linked |
| `NO_CREDIT_TERMS` | error | Customer has no terms in force on the order date |
| `CUSTOMER_CREDIT_RISK` | warning | Customer risk rating elevated/high; message states the terms to apply |
| `SUPPLIER_NOT_APPROVED` | error | A line uses a supplier without an approved account |
| `STALE_FX_RATE` | warning | FX rate older than `max_fx_rate_age_days` at receipt |
| `CHECK_FAILED` | error | A pre-processing check was recorded as failed |

Errors block approval unless the rule's `blocks_approval` is false in `sales.exception_rule` (the catalogue of every rule); warnings inform. `ARR_LINE_NO_ARR_REF` is a warning before processing and an error after, and never blocks (CFO decision). `REPORTING_CATEGORY_MISMATCH` (warning): NN/E suffix disagrees with customer history. Add a rule = add one `UNION ALL` branch in a new migration.

## GL, products, ARR and SN (migration 0004)

| Table | Purpose |
|---|---|
| `gl_account` | Mirror of the Xero chart. Lines carry `revenue_gl_code` (must be class REVENUE) and `cost_gl_code` (must be type DIRECTCOSTS). Deferred-revenue 72xx accounts are rejected on lines. |
| `service_category` | The 14 Register categories; Cloud Services defaults to 1233/2233 |
| `product` | Product database: SKU, category, default GL pair, default supplier, list price/cost |
| `arr_contract` | One row per ARR ref, per customer, with term dates and notice period |
| `arr_movement` | **Append-only ledger**: new / expansion / contraction / price change / renewal / churn. Posted automatically when an order is approved |
| `register_entry` | Read-only mirror of the AW SOs Register (replaced each sync), the source of SN refs |

GL defaulting order for a line: stated on the line → product → service category (→ CSP rule for CFQ7 SKUs).

ARR: `sales.arr_at(date)` gives MRR/ARR by contract on any date; `sales.arr_bridge(from, to)` gives the
movement analysis between two dates. MRR per line = quantity × unit price ÷ months per billing period.

SN sourcing: `sales.v_sn_candidate` scores Register rows for the same customer (normalised name, trading
name or Xero tracking name) within 7 days: +3 project matches title, +2 same day (else +1), +3 revenue
within £1. Only a unique best match is assigned; LO/CA rows and SNs already used are never candidates.
A manually chosen SN must exist on the Register for that customer.

Additional exception rules (`v_sales_order_exception_all`): `SN_NOT_ASSIGNED`, `NO_GL_CODE`,
`NO_SERVICE_CATEGORY`, `ARR_LINE_NO_ARR_REF`, `ARR_CONTRACT_OTHER_CUSTOMER` (errors),
`RECURRING_LINE_NO_DATES` (warning). Margin rules (0003): `LOSS_LINE_NO_RATIONALE`,
`ORDER_LOSS_NO_RATIONALE` replace the old minimum-margin rule.

## Conventions

- Surrogate `bigint` identity keys; readable text codes for lookups.
- Money `numeric` — never floating point, in the database *or* in Python (the input models reject floats).
- Every table: `created_at/by`, `updated_at/by`, `row_version`; all covered by the audit trigger.
- Master data (customers, suppliers, employees, FX rates) is never created implicitly by an order.
  An unknown name fails the whole load — a typo must not create a duplicate customer.
- Once a migration has run in production it is immutable; changes go in a new migration.
