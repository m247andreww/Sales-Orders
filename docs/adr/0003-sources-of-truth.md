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

## Reporting categories: NN = net new customer, E = existing customer (CFO, 2026-09-24)

Tested against the Register (Jan 2024 to Sep 2026):
- Strictly ("NN only if no earlier order"), 171 of 172 "Expansion NN" and 18 of 18 "Churn NN" rows are miscoded.
- As practised, NN tracks "first 12 months as a customer" far better (91 of 116 measurable "Expansion NN").
- The Register's own "New Logo" column contradicts the suffix on 91 rows (73 "Existing" coded NN, 18 "New Logo" coded E).

So the window is `policy_setting.new_customer_window_months` (default 12; 0 = strict first order), and a
mismatch is a warning (`REPORTING_CATEGORY_MISMATCH`), never a block. The CFO should confirm the window.

## ARR ref follows processing (CFO, 2026-09-24)

A missing ARR ref never blocks approval. It is a warning before processing and an **error** after,
listed daily by `sales-orders arr-outstanding` (`sales.v_arr_ref_outstanding`, with MRR missing from ARR
and days outstanding). Linking the ARR ref (`sales-orders link-arr`) is the one change allowed on a
locked line; it checks the contract's customer and posts the ARR movement.

## Carried over from the defunct SQLite build

Rules and vocabulary (above). Orders in flight there at switch-over must be entered here:
SN260524, SN260518, SN260529, plus unloaded SN260523, SN260525, SN260526 (as listed in that build).
