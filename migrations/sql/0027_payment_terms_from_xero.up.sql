-- =============================================================================
-- 0027 Payment terms come from Xero (CFO, 9 Oct 2026: "invoices collected by DD start 'RD'. some clients are on
-- 60 day terms - refer to xero").
-- Xero holds the terms in two places, both read daily without any input:
--   * the contact's sales payment terms (custom connection, accounting.contacts): used for one-off invoices, e.g.
--     Princes 60 days, Stopford 14; a contact with none set uses the standard 30 days;
--   * the invoices themselves (Xero connector): monthly invoices numbered RD-… are collected by Direct Debit,
--     RI-… are paid by transfer; their due date minus invoice date is the recurring terms in force.
-- The register (customer_credit_terms) records where each period came from (source 'xero' or 'cfo') and the basis of
-- the one-off terms (Xero's four kinds). A CFO change made on the Credit Desk is written back to the Xero contact, so
-- Xero stays the master and invoices fall due as decided.
-- =============================================================================

ALTER TABLE sales.xero_contact_directory
    ADD COLUMN sales_terms_days integer CHECK (sales_terms_days BETWEEN 0 AND 366),
    ADD COLUMN sales_terms_type text CHECK (sales_terms_type IN
        ('DAYSAFTERBILLDATE', 'DAYSAFTERBILLMONTH', 'OFCURRENTMONTH', 'OFFOLLOWINGMONTH')),
    ADD CONSTRAINT xero_contact_directory_terms_pair CHECK ((sales_terms_days IS NULL) = (sales_terms_type IS NULL));

ALTER TABLE sales.customer_credit_terms
    ADD COLUMN one_off_terms_basis text NOT NULL DEFAULT 'DAYSAFTERBILLDATE' CHECK (one_off_terms_basis IN
        ('DAYSAFTERBILLDATE', 'DAYSAFTERBILLMONTH', 'OFCURRENTMONTH', 'OFFOLLOWINGMONTH')),
    ADD COLUMN source_code text NOT NULL DEFAULT 'cfo' CHECK (source_code IN ('cfo', 'xero'));
COMMENT ON COLUMN sales.customer_credit_terms.one_off_terms_basis IS
    'As Xero: days after the invoice date / after the invoice month end, or the day of this / the following month.';

-- ---------------------------------------------------------------- invoices observed in Xero (append-only)
CREATE TABLE sales.credit_invoice_snapshot (
    credit_invoice_snapshot_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    as_of         date NOT NULL,
    source        text NOT NULL CHECK (btrim(source) <> ''),
    source_sha256 char(64) NOT NULL UNIQUE CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    loaded_by     text        NOT NULL DEFAULT audit.current_actor()
);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_invoice_snapshot
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();

CREATE TABLE sales.credit_invoice_line (
    credit_invoice_snapshot_id bigint NOT NULL REFERENCES sales.credit_invoice_snapshot,
    invoice_number  text NOT NULL CHECK (btrim(invoice_number) <> ''),
    xero_contact_id uuid NOT NULL REFERENCES sales.xero_contact_directory,
    invoice_date    date NOT NULL,
    due_date        date NOT NULL CHECK (due_date >= invoice_date),
    series          text GENERATED ALWAYS AS (upper(split_part(invoice_number, '-', 1))) STORED,
    terms_days      integer GENERATED ALWAYS AS (due_date - invoice_date) STORED,
    PRIMARY KEY (credit_invoice_snapshot_id, invoice_number)
);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_invoice_line
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();
COMMENT ON COLUMN sales.credit_invoice_line.series IS
    'RD = recurring, collected by Direct Debit (CFO, 9 Oct 2026); RI = recurring, paid by transfer; PI = one-off.';

-- What Xero says each customer's terms are. Recurring: from the latest invoices (RD wins: Direct Debit); one-off:
-- the contact's terms, else the standard. NULL recurring_days = no recurring invoice seen in the latest snapshot.
CREATE VIEW sales.v_xero_customer_terms AS
WITH std AS (SELECT numeric_value::integer AS days FROM sales.policy_setting
              WHERE setting_key = 'credit_standard_terms_days'),
latest AS (SELECT max(credit_invoice_snapshot_id) AS id FROM sales.credit_invoice_snapshot),
seen AS (
    SELECT l.xero_contact_id,
           count(*) FILTER (WHERE l.series = 'RD')                                         AS rd_invoices,
           mode() WITHIN GROUP (ORDER BY l.terms_days) FILTER (WHERE l.series = 'RD')      AS rd_days,
           mode() WITHIN GROUP (ORDER BY l.terms_days) FILTER (WHERE l.series = 'RI')      AS ri_days
      FROM sales.credit_invoice_line l JOIN latest ON l.credit_invoice_snapshot_id = latest.id
     GROUP BY l.xero_contact_id
)
SELECT c.customer_id, c.xero_contact_id,
       d.sales_terms_days, d.sales_terms_type,
       COALESCE(s.rd_invoices, 0) > 0                                         AS collected_by_dd,
       CASE WHEN COALESCE(s.rd_invoices, 0) > 0 THEN s.rd_days ELSE s.ri_days END AS recurring_days,
       CASE WHEN COALESCE(s.rd_invoices, 0) > 0 THEN 'direct_debit'
            WHEN s.ri_days IS NOT NULL THEN 'bank_transfer' END               AS recurring_method,
       COALESCE(d.sales_terms_days, std.days)                                 AS one_off_days,
       COALESCE(d.sales_terms_type, 'DAYSAFTERBILLDATE')                      AS one_off_basis,
       std.days                                                               AS standard_terms_days
  FROM sales.customer c
 CROSS JOIN std
  JOIN sales.xero_contact_directory d ON d.xero_contact_id = c.xero_contact_id
  LEFT JOIN seen s ON s.xero_contact_id = c.xero_contact_id;

-- The setter now records the basis of the one-off terms and where the terms came from.
DROP VIEW sales.v_customer_payment_terms;
DROP FUNCTION sales.set_customer_payment_terms(bigint, integer, text, integer, boolean, text);

CREATE FUNCTION sales.set_customer_payment_terms(
    p_customer_id bigint, p_recurring_days integer, p_recurring_method text, p_one_off_days integer,
    p_one_off_prepay boolean, p_reason text, p_one_off_basis text, p_source text) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_std     integer;
    v_non     boolean;
    v_current sales.customer_credit_terms%ROWTYPE;
    v_id      bigint;
BEGIN
    PERFORM sales.require_permission('approve_credit_terms', 'setting payment terms');
    SELECT numeric_value::integer INTO STRICT v_std
      FROM sales.policy_setting WHERE setting_key = 'credit_standard_terms_days';
    v_non := p_recurring_days <> v_std OR p_one_off_days <> v_std OR p_one_off_prepay
             OR p_one_off_basis <> 'DAYSAFTERBILLDATE';
    IF v_non AND (p_reason IS NULL OR btrim(p_reason) = '') THEN
        RAISE EXCEPTION 'non-standard payment terms need a reason' USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM sales.customer_credit_terms
                WHERE customer_id = p_customer_id AND effective_from > current_date) THEN
        RAISE EXCEPTION 'customer % already has terms starting after today: change those first', p_customer_id
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT * INTO v_current FROM sales.customer_credit_terms
     WHERE customer_id = p_customer_id AND effective_from <= current_date
       AND (effective_to IS NULL OR effective_to >= current_date)
       FOR UPDATE;

    IF FOUND AND v_current.effective_from = current_date THEN
        UPDATE sales.customer_credit_terms
           SET recurring_terms_days = p_recurring_days, recurring_payment_method_code = p_recurring_method,
               one_off_terms_days = p_one_off_days, one_off_prepayment_required = p_one_off_prepay,
               one_off_terms_basis = p_one_off_basis, source_code = p_source,
               is_non_standard = v_non, reason = NULLIF(btrim(p_reason), ''),
               approved_by_employee_id = CASE WHEN v_non THEN approved_by_employee_id END,
               approved_at = CASE WHEN v_non THEN approved_at END
         WHERE customer_credit_terms_id = v_current.customer_credit_terms_id
        RETURNING customer_credit_terms_id INTO v_id;
        RETURN v_id;
    END IF;
    IF FOUND THEN
        UPDATE sales.customer_credit_terms SET effective_to = current_date - 1
         WHERE customer_credit_terms_id = v_current.customer_credit_terms_id;
    END IF;
    INSERT INTO sales.customer_credit_terms
           (customer_id, effective_from, recurring_terms_days, recurring_payment_method_code, one_off_terms_days,
            one_off_prepayment_required, one_off_terms_basis, source_code, risk_rating_code, is_non_standard, reason)
    VALUES (p_customer_id, current_date, p_recurring_days, p_recurring_method, p_one_off_days, p_one_off_prepay,
            p_one_off_basis, p_source, COALESCE(v_current.risk_rating_code, 'standard'), v_non,
            NULLIF(btrim(p_reason), ''))
    RETURNING customer_credit_terms_id INTO v_id;
    RETURN v_id;
END
$$;

CREATE VIEW sales.v_customer_payment_terms AS
WITH std AS (SELECT numeric_value::integer AS days FROM sales.policy_setting
              WHERE setting_key = 'credit_standard_terms_days')
SELECT c.customer_id, c.legal_name,
       COALESCE(t.recurring_terms_days, std.days)         AS recurring_terms_days,
       t.recurring_payment_method_code,
       COALESCE(t.one_off_terms_days, std.days)           AS one_off_terms_days,
       COALESCE(t.one_off_terms_basis, 'DAYSAFTERBILLDATE') AS one_off_terms_basis,
       COALESCE(t.one_off_prepayment_required, false)     AS one_off_prepayment_required,
       COALESCE(t.is_non_standard, false)                 AS is_non_standard,
       t.reason, t.effective_from, t.source_code,
       t.customer_credit_terms_id IS NULL                 AS is_default,
       std.days                                           AS standard_terms_days
  FROM sales.customer c
 CROSS JOIN std
  LEFT JOIN sales.customer_credit_terms t
         ON t.customer_id = c.customer_id AND t.effective_from <= current_date
        AND (t.effective_to IS NULL OR t.effective_to >= current_date);
