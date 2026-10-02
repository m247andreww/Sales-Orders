# ADR 0005 — Credit & risk management replaces the Credit Limit Assessment Workings file

Status: **proposed** (built 2026-10-02; open CFO decisions listed at the end).

## Context

The CFO's routine today: a bureau alert arrives (Experian Business Express or Creditsafe), the CFO
looks the company up, updates its sheet in *Credit Limit Assessment Workings.xlsx*, exports the
summary page to PDF, attaches it to the client in Xero, updates the client's credit limit, and copies
the alert email into the client's **Debt & Credit** mail folder. Some monitored companies are not
clients (suppliers, or for information). The CFO asked for this to be wholly automated.

Discovery (2026-10-02, CFO mailbox read-only and the two files supplied):

- Both bureaus email the CFO: Experian "New Experian Business Express Alerts"
  (`ebe.noreply@experian.com`, one email listing several companies by company number, with old and new
  Credit Rating, Credit Limit, Risk Score and Risk Band) and Creditsafe "Connect monitoring alert"
  (`monitoring@creditsafe.com`, portfolio "Live Customers", a table with company number, Safe No. and
  old/new values). The alerts carry everything the assessment needs, so no bureau login or API
  subscription is required.
- Each client has a mail folder named "Debt & Credit"; one alert covering several companies is copied
  into each client's folder (copies keep the same Message-ID).
- The workbook's Experian figure is the bureau's **Credit Limit**, not its Credit Rating [likely: the
  workbook values track the alerts' Credit Limit figures].
- Xero's public API has **no credit-limit field** on a contact [certain: Xero OpenAPI spec]; it does
  allow file attachments and history notes on a contact.

### Faults found in the workbook (each fixed by the new rules)

| Fault | Effect | Example |
|---|---|---|
| Recurring revenue looked up by customer *name* (`SUMIFS` on the sheet name) | A name that differs from the ARR file finds nothing: exposure £0 | two clients with £159k and £232k a year of commitments assessed on one-off spend only |
| No status filter | Cancelled lines and both halves of a renewal counted | one client: £1.15m a year of cancelled ARR counted; another assessed at £25k with every line cancelled |
| `F16 = SUM(F12:F15)` | Quint- and tri-annual rows (F10:F11) never added | three clients understated |
| `F24 = IF(D21="TRUE", …_FV(…))` | Compares TRUE with the text "TRUE" (never equal) and contains a corrupted `_FV(` call: the 50% rule never ran | every v2 sheet |
| Frequencies missing | Bi-annual, dec-annual, 6/20/52 months, "annual and half" ignored | 26 ARR lines |
| Two templates, typed-over results, ROUNDUP to 100 or 1,000 | Not comparable; history of who decided what lost | Board-approved £100k typed over the formula |

## Decision

1. **Database is the master** for monitored companies, bureau readings, assessments and credit limits
   (migration 0014). Readings and assessments are append-only evidence.
2. **Inputs arrive by themselves**: the daily `credit-run` reads every bureau alert from the mailbox
   (Microsoft Graph, app-only, restricted to that mailbox) and the ARR file from SharePoint.
3. **The workbook's rules are data**, not formulas: exposure per frequency (`credit_exposure_rule`),
   which ARR statuses count (`credit_arr_status`), VAT, rounding and appetite (`policy_setting`).
   Customers are matched to the ARR file by their **ARR prefix** (e.g. `TST001` → the customer with prefix TST), never by
   name.
4. **Recommended limit = trading requirement**, applied automatically only when it is inside risk
   appetite: requirement ≤ 50% of the lower bureau limit (when that 50% is at most £100k) and ≤ the
   lower bureau limit, with a bureau limit present and no adverse Experian band. Anything else waits
   for a CFO decision (`credit-decide`, permission `approve_credit_terms`, reason required, optional
   review date). This automates the cases where both readings of the workbook's broken formula agree,
   and sends every ambiguous case to the CFO.
5. **Reassess only on change**: a bureau limit or Experian band change, an ARR requirement change, or
   an allowance change (`v_credit_assessment_due`). A CFO decision in force is never overwritten by an
   automatic assessment.
6. **Filing is an outbox** (`credit_filing_task`): the snapshot PDF and a history note on the Xero
   contact (with an idempotency key), and a copy of each alert email in each client's Debt & Credit
   folder. Failures retry on the next run; after three, they are reported (`FILING_FAILED`).
7. **Credit limits have one home**: `sales.customer_credit_limit` (effective-dated, no overlaps).
   `customer_credit_terms.credit_limit` is retired (always NULL).
8. Non-clients (supplier, information) are monitored and reported (e.g. `ADVERSE_RISK_BAND` for
   supply risk) but get no limit and nothing is filed.

## Consequences

- The CFO's only routine input is a decision on the cases flagged `CREDIT_REVIEW_NEEDED`, by reply to
  the daily summary email.
- Xero's own credit-limit box cannot be set by the API. Until decision D2 below, the daily summary
  lists each changed limit to mirror in Xero.
- A company only appears once it is in the monitored list (master data). Bureau alerts about
  unlisted companies are kept and reported (`UNKNOWN_COMPANY`), never turned into master data.

## Open CFO decisions

| # | Question | Default built |
|---|---|---|
| D1 | Multi-year invoices (tri-/quint-annual): exposure at one year's value (workbook) or the full invoice (3 or 5 years)? | One year's value (workbook) |
| D2 | Xero credit-limit box: keep mirroring by hand from the daily list, or stop using it and rely on this database (exposure checks to be built from Xero aged receivables)? | Daily list to mirror by hand |
| D3 | Which ARR statuses are a commitment? "Order" (signed, not billing) counts; "Renewal - Old" and "Cancelled" do not | As stated |
| D4 | Which Experian bands always need review? | High Risk, Maximum Risk, Serious Adverse Information |
| D5 | One-off & project allowance for a newly monitored customer (workbook used £0–£50k by hand) | £5,000 |
| D6 | Go-live: carry the workbook's typed-over limits (three £100k figures) as CFO decisions? | Not carried: reviewed from the reconciliation first |
