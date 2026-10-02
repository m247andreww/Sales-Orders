-- =============================================================================
-- 0020 A customer the CFO has decided not to monitor (e.g. Interserve: in administration, CFO dealing
-- with the administrators, 2026-10-02) is excluded from the daily "no credit limit" list, with the
-- reason kept. The scan reports how many are excluded so none is forgotten.
-- =============================================================================

ALTER TABLE sales.credit_customer_match
    ADD COLUMN excluded_reason text CHECK (excluded_reason IS NULL OR btrim(excluded_reason) <> '');
ALTER TABLE sales.credit_customer_match DROP CONSTRAINT credit_customer_match_check;
ALTER TABLE sales.credit_customer_match ADD CONSTRAINT credit_customer_match_check
    CHECK (xero_contact_id IS NOT NULL OR company_number IS NOT NULL OR note IS NOT NULL OR excluded_reason IS NOT NULL);
