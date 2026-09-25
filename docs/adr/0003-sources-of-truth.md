# ADR 0003 — Sources of truth and the existing AW SOs process

Status: **accepted** (CFO, 2026-09-24): this database is the system of record **immediately**;
the SQLite build is **defunct** and must not be used or updated.

Discovery (2026-09-24) found an established process and an earlier Claude build:
the **AW SOs** Google Sheet (Register 1,567 SOs, Details 5,413 lines, Product catalogue, chart of
accounts), **ARR Live.xlsx** (1,241 rows), and a SQLite prototype in OneDrive
(`3 AW Filing/10. Claude/Sales Orders`) with a 66-rule knowledge base and twice-daily sync.

| Data | Master | This database |
|---|---|---|
| SN reference | AW SOs Register | Mirrors it; **never generates an SN**; approval blocked until sourced |
| ARR reference (e.g. TIL030, NAP008-26) | ARR file | Stores it on contracts; ARR *values* are a ledger here |
| GL codes | Xero chart of accounts | Mirrors it; validates account class/type on every line |
| Order content, checks, approvals, credit terms | **This database** | Master |
| Products | AW SOs Product catalogue (PandaDoc catalogue is effectively unused: 2 items) | Product database, loaded from the catalogue |

Rules adopted from the existing knowledge base: SNs never guessed; CSP SKUs (CFQ7…) → GL 1233;
LAST_ORDER (…LO) rows excluded from bookings and ARR; Register vocabulary for order category,
reporting category, service category.

Deliberate differences from the Register:
- ARR is posted from the billing period (MRR = qty × price ÷ months per period) rather than
  "Total Revenue × 12 ÷ Term", and part-period co-term stubs are marked `stub` and excluded.
  This avoids the known inflation from annualising monthly stub lines.
- Quantities are always positive; reversals (LO/CA) are ARR movements, not negative lines.

## Accepted risk: AW SOs remains on a personal Google account

AW SOs (the SN master) is owned by a personal Gmail account. The CFO has decided not to move it
(2026-09-24). Mitigation: every Register sync stores a complete, Managed247-owned copy in
`sales.register_entry` with a sync log (`sales.register_sync`), so the database always holds the
Register as at its last sync. Sync regularly; the copy is only as current as the last sync.

## Reporting categories NN / E (CFO, 2026-09-24, refined)

**Net new (NN) = the customer was not pre-existing at the time the salesperson was allocated to the
account; Existing (E) = it was.** It is about who won the customer, not the customer's age.

Evidence (Register, Jan 2024 to Sep 2026; allocation date approximated by the first order each salesperson
handled for the customer, because the Register does not record allocations):
- 284 of 302 NN/E rows agree with this rule (94%). The 18 disagreements are candidate miscodes.
- Earlier interpretations fitted far worse ("first order only": under 40%; "first 12 months": about 55%).
- 34 rows are owned by House / Legacy / Auto Renew / Cust Success, where the rule needs a decision.

Implementation (migration 0007): `sales.customer_account_allocation` holds who owned each account from
when (one owner at a time; non-person owners allowed). Warnings, never blocks:
`REPORTING_CATEGORY_MISMATCH`, `NO_ACCOUNT_ALLOCATION` (cannot verify), `SALESPERSON_NOT_ACCOUNT_OWNER`.
`sales-orders salesperson-history` proposes allocation history from the Register for confirmation; it is
never loaded automatically.

## House and Legacy accounts (CFO, 2026-09-24)

- **House** = controlled by finance, not allocated to a salesperson. **When a salesperson leaves, all of
  their accounts go to House until a new salesperson is allocated** (`sales-orders employee-leaves`,
  then `sales-orders allocate-account`). Both are single audited transactions.
- **Legacy** = historic Register label (salesperson left before this policy). Kept for history only.
- Orders on House/Legacy accounts are **E**, never NN. A named salesperson on a House account order is
  flagged (`SALESPERSON_NOT_ACCOUNT_OWNER`); an inactive owner still holding accounts is flagged
  (`OWNER_HAS_LEFT`).
- A salesperson taking over a House account inherits a pre-existing customer, so their orders on it are E.
- **Auto Renew** and **Cust Success** are **House** (CFO, 2026-09-24). Legacy is also treated as House
  when building history, because leavers' accounts now go to House.

## Account-ownership history (built from the Register, CFO instruction 2026-09-24)

Rule (migration 0009): ownership changes only when a different **named** salesperson appears on an order;
House-labelled orders (central renewals) never end a salesperson's ownership. A customer whose first
Register order is marked "Existing" pre-dates the Register (`customer.existed_before`), so it is Existing
for its first salesperson. Customers that already have history are never overwritten.

Dry run on the live Register (2026-09-24): 85 customers, 142 ownership periods, 36 customers pre-dating the
Register, 2 same-day clashes decided (later SN wins) and 8 alternations between named salespeople on 6
customers, all listed by `sales-orders build-account-history` for CFO review. Real names and results are
not stored in the repository.

## The order's salesperson leads it (CFO, 2026-09-25)

"It will be whichever salesperson is named on the Sales Order email, as they will have led the
opportunity." Consequences (migration 0010):
- The history build's same-day decisions (later order's salesperson) are confirmed, and accounts that
  alternate between named salespeople are genuine: each order belongs to whoever led it.
- NN/E is judged for **the order's salesperson**: NN if the customer did not pre-exist that salesperson
  first taking the account. A returning salesperson keeps their original start. No salesperson on the
  order: the account owner on the order date is used (House → E).
- On approval, ownership passes to the order's salesperson from the order date, so history keeps itself
  current. A back-dated order never rewrites a later change, and a leaver never takes an account; both
  are logged to `account_history_review` instead.

## ARR ref follows processing (CFO, 2026-09-24)

A missing ARR ref never blocks approval. It is a warning before processing and an **error** after,
listed daily by `sales-orders arr-outstanding` (`sales.v_arr_ref_outstanding`, with MRR missing from ARR
and days outstanding). Linking the ARR ref (`sales-orders link-arr`) is the one change allowed on a
locked line; it checks the contract's customer and posts the ARR movement.

## Carried over from the defunct SQLite build

Rules and vocabulary (above). Orders in flight there at switch-over must be entered here:
SN260524, SN260518, SN260529, plus unloaded SN260523, SN260525, SN260526 (as listed in that build).
