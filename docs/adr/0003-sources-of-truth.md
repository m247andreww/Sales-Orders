# ADR 0003 — Sources of truth and the existing AW SOs process

Status: proposed (needs CFO confirmation of the migration path, see README).

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
