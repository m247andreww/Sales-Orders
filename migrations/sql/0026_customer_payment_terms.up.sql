-- =============================================================================
-- 0026 Payment terms join the credit process (CFO, 9 Oct 2026: "add payment terms to this process ... default is
-- 30 days from date of invoice. Stopford to be 14 days unless payable on DD").
-- The terms register already exists (customer_credit_terms, 0001: recurring and one-off terms, effective-dated, no
-- overlaps; non-standard terms need approve_credit_terms, 0003). This adds:
--   * the standard (default) terms as a policy setting: 30 days from the invoice date;
--   * sales.set_customer_payment_terms(): the one way the credit process changes terms. It classifies standard vs
--     non-standard itself, needs approve_credit_terms, closes the period in force and opens a new one from today
--     (or amends today's), and requires a reason for non-standard terms;
--   * the authority trigger no longer re-stamps the approver when a period is only being closed (history stays
--     true to who approved it);
--   * sales.v_customer_payment_terms: every customer's terms today, falling back to the standard terms.
-- =============================================================================

INSERT INTO sales.policy_setting (setting_key, numeric_value, description) VALUES
    ('credit_standard_terms_days', 30, 'Standard payment terms: days from the invoice date (CFO, 9 Oct 2026)');

CREATE OR REPLACE FUNCTION sales.tg_credit_terms_authority() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.is_non_standard AND (TG_OP = 'INSERT' OR ROW(NEW.*) IS DISTINCT FROM ROW(OLD.*)) THEN
        PERFORM sales.require_permission('approve_credit_terms', 'setting non-standard credit terms');
        -- Closing a period (only effective_to changes) keeps the original approver; anything else is a new approval.
        IF TG_OP = 'INSERT'
           OR (NEW.customer_id, NEW.effective_from, NEW.recurring_terms_days, NEW.recurring_payment_method_code,
               NEW.one_off_terms_days, NEW.one_off_prepayment_required, NEW.risk_rating_code, NEW.is_non_standard,
               NEW.reason)
              IS DISTINCT FROM
              (OLD.customer_id, OLD.effective_from, OLD.recurring_terms_days, OLD.recurring_payment_method_code,
               OLD.one_off_terms_days, OLD.one_off_prepayment_required, OLD.risk_rating_code, OLD.is_non_standard,
               OLD.reason) THEN
            NEW.approved_by_employee_id := sales.current_employee_id();
            NEW.approved_at := now();
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION sales.set_customer_payment_terms(
    p_customer_id bigint, p_recurring_days integer, p_recurring_method text, p_one_off_days integer,
    p_one_off_prepay boolean, p_reason text) RETURNS bigint
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
    v_non := p_recurring_days <> v_std OR p_one_off_days <> v_std OR p_one_off_prepay;
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
            one_off_prepayment_required, risk_rating_code, is_non_standard, reason)
    VALUES (p_customer_id, current_date, p_recurring_days, p_recurring_method, p_one_off_days, p_one_off_prepay,
            COALESCE(v_current.risk_rating_code, 'standard'), v_non, NULLIF(btrim(p_reason), ''))
    RETURNING customer_credit_terms_id INTO v_id;
    RETURN v_id;
END
$$;

-- Every customer's payment terms today; customers with no register entry are on the standard terms.
CREATE VIEW sales.v_customer_payment_terms AS
WITH std AS (SELECT numeric_value::integer AS days FROM sales.policy_setting
              WHERE setting_key = 'credit_standard_terms_days')
SELECT c.customer_id, c.legal_name,
       COALESCE(t.recurring_terms_days, std.days)         AS recurring_terms_days,
       t.recurring_payment_method_code,
       COALESCE(t.one_off_terms_days, std.days)           AS one_off_terms_days,
       COALESCE(t.one_off_prepayment_required, false)     AS one_off_prepayment_required,
       COALESCE(t.is_non_standard, false)                 AS is_non_standard,
       t.reason, t.effective_from,
       t.customer_credit_terms_id IS NULL                 AS is_default,
       std.days                                           AS standard_terms_days
  FROM sales.customer c
 CROSS JOIN std
  LEFT JOIN sales.customer_credit_terms t
         ON t.customer_id = c.customer_id AND t.effective_from <= current_date
        AND (t.effective_to IS NULL OR t.effective_to >= current_date);
