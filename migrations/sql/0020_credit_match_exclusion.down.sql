-- Reverse 0020 (an exclusion-only row would break the old rule, so give it its reason as a note first).
UPDATE sales.credit_customer_match SET note = excluded_reason
 WHERE note IS NULL AND xero_contact_id IS NULL AND company_number IS NULL;
ALTER TABLE sales.credit_customer_match DROP CONSTRAINT credit_customer_match_check;
ALTER TABLE sales.credit_customer_match ADD CONSTRAINT credit_customer_match_check
    CHECK (xero_contact_id IS NOT NULL OR company_number IS NOT NULL OR note IS NOT NULL);
ALTER TABLE sales.credit_customer_match DROP COLUMN excluded_reason;
