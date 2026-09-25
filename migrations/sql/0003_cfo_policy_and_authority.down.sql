-- Reverses 0003: restores the 0001 definitions verbatim (generated from 0001).
DROP VIEW sales.v_sales_order_exception;
CREATE VIEW sales.v_sales_order_exception AS
WITH s AS (SELECT * FROM sales.v_sales_order_summary),
     p AS (SELECT
             (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'min_order_gm_pct')  AS min_gm_pct,
             (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'max_fx_rate_age_days') AS max_fx_age,
             (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'stated_total_tolerance') AS tolerance)
SELECT s.sales_order_id, s.order_number, 'NO_LINES' AS rule_code, 'error' AS severity,
       'Order has no lines' AS message
  FROM s WHERE s.line_count = 0
UNION ALL
SELECT l.sales_order_id, o.order_number, 'NEGATIVE_LINE_MARGIN', 'error',
       format('Line %s (%s) sells below cost: margin %s', l.line_number, l.description, l.gross_margin)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.gross_margin < 0
UNION ALL
SELECT s.sales_order_id, s.order_number, 'LOW_ORDER_MARGIN', 'warning',
       format('Order GM %s%% is below policy minimum %s%%', s.gross_margin_pct, p.min_gm_pct)
  FROM s CROSS JOIN p
  JOIN sales.sales_order o USING (sales_order_id)
 WHERE s.gross_margin_pct < p.min_gm_pct AND o.margin_exception_reason IS NULL
UNION ALL
SELECT l.sales_order_id, o.order_number, 'TAX_LINE_MARKED_UP', 'error',
       format('Line %s (%s) is a tax pass-through but sell %s <> cost %s',
              l.line_number, l.description, l.net_sell, l.net_cost)
  FROM sales.sales_order_line l
  JOIN sales.sales_order o USING (sales_order_id)
  JOIN sales.line_category lc USING (line_category_code)
 WHERE lc.is_tax_pass_through AND l.net_sell <> l.net_cost
UNION ALL
SELECT s.sales_order_id, s.order_number, 'STATED_TOTAL_MISMATCH', 'error',
       format('Submitted totals (cost %s / sell %s / GM %s) do not match computed (cost %s / sell %s / GM %s)',
              s.stated_net_cost, s.stated_net_sell, s.stated_gross_margin,
              s.net_cost, s.net_sell, s.gross_margin)
  FROM s CROSS JOIN p
 WHERE abs(COALESCE(s.stated_net_cost, s.net_cost) - s.net_cost) > p.tolerance
    OR abs(COALESCE(s.stated_net_sell, s.net_sell) - s.net_sell) > p.tolerance
    OR abs(COALESCE(s.stated_gross_margin, s.gross_margin) - s.gross_margin) > p.tolerance
UNION ALL
SELECT o.sales_order_id, o.order_number, 'MISSING_SIGNED_ORDER', 'error',
       'No signed order document is attached'
  FROM sales.sales_order o
 WHERE NOT EXISTS (SELECT 1 FROM sales.sales_order_document sod
                     JOIN sales.document d USING (document_id)
                    WHERE sod.sales_order_id = o.sales_order_id
                      AND d.document_type_code = 'signed_order')
UNION ALL
SELECT o.sales_order_id, o.order_number, 'NO_CREDIT_TERMS', 'error',
       'Customer has no credit terms in force on the order date'
  FROM sales.sales_order o
 WHERE NOT EXISTS (SELECT 1 FROM sales.customer_credit_terms t
                    WHERE t.customer_id = o.customer_id
                      AND t.effective_from <= o.received_at::date
                      AND (t.effective_to IS NULL OR t.effective_to >= o.received_at::date))
UNION ALL
SELECT o.sales_order_id, o.order_number, 'CUSTOMER_CREDIT_RISK', 'warning',
       format('Customer risk rating is %s; terms: recurring %s days by %s, one-off %s',
              t.risk_rating_code, t.recurring_terms_days, t.recurring_payment_method_code,
              CASE WHEN t.one_off_prepayment_required THEN 'payment on order'
                   ELSE t.one_off_terms_days || ' days' END)
  FROM sales.sales_order o
  JOIN sales.customer_credit_terms t
    ON t.customer_id = o.customer_id
   AND t.effective_from <= o.received_at::date
   AND (t.effective_to IS NULL OR t.effective_to >= o.received_at::date)
  JOIN sales.risk_rating r USING (risk_rating_code)
 WHERE r.severity >= 2
UNION ALL
SELECT DISTINCT l.sales_order_id, o.order_number, 'SUPPLIER_NOT_APPROVED', 'error',
       format('Supplier %s account status is %s', sp.name, sp.account_status_code)
  FROM sales.sales_order_line l
  JOIN sales.sales_order o USING (sales_order_id)
  JOIN sales.supplier sp USING (supplier_id)
  JOIN sales.supplier_account_status sas ON sas.status_code = sp.account_status_code
 WHERE NOT sas.can_order
UNION ALL
SELECT DISTINCT l.sales_order_id, o.order_number, 'STALE_FX_RATE', 'warning',
       format('FX rate %s->%s dated %s is more than %s days before the order was received',
              fx.from_currency_code, fx.to_currency_code, fx.rate_date, p.max_fx_age)
  FROM sales.sales_order_line l
  JOIN sales.sales_order o USING (sales_order_id)
  JOIN sales.fx_rate fx USING (fx_rate_id)
  CROSS JOIN p
 WHERE o.received_at::date - fx.rate_date > p.max_fx_age
UNION ALL
SELECT c.sales_order_id, o.order_number, 'CHECK_FAILED', 'error',
       format('Pre-processing check %s failed', c.check_type_code)
  FROM sales.sales_order_check c JOIN sales.sales_order o USING (sales_order_id)
 WHERE c.check_status_code = 'failed';

CREATE OR REPLACE FUNCTION sales.tg_sales_order_status() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status_code <> 'received' THEN
            RAISE EXCEPTION 'new orders must start in status received, not %', NEW.status_code
                USING ERRCODE = 'check_violation';
        END IF;
        INSERT INTO sales.sales_order_status_history (sales_order_id, from_status, to_status, reason)
        VALUES (NEW.sales_order_id, NULL, NEW.status_code, 'created');
        RETURN NEW;
    END IF;

    IF NEW.status_code IS DISTINCT FROM OLD.status_code THEN
        IF NOT EXISTS (SELECT 1 FROM sales.order_status_transition
                        WHERE from_status = OLD.status_code AND to_status = NEW.status_code) THEN
            RAISE EXCEPTION 'order %: status change % -> % is not allowed',
                OLD.order_number, OLD.status_code, NEW.status_code USING ERRCODE = 'check_violation';
        END IF;
        -- Approval gate: no error-level exceptions, no unresolved pre-processing checks.
        IF NEW.status_code = 'approved' THEN
            IF EXISTS (SELECT 1 FROM sales.v_sales_order_exception e
                        WHERE e.sales_order_id = NEW.sales_order_id AND e.severity = 'error') THEN
                RAISE EXCEPTION 'order % cannot be approved: it has error-level exceptions (see sales.v_sales_order_exception)',
                    NEW.order_number USING ERRCODE = 'check_violation';
            END IF;
            IF EXISTS (SELECT 1 FROM sales.sales_order_check c
                         JOIN sales.check_status cs USING (check_status_code)
                        WHERE c.sales_order_id = NEW.sales_order_id AND NOT cs.is_resolved) THEN
                RAISE EXCEPTION 'order % cannot be approved: pre-processing checks are unresolved',
                    NEW.order_number USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        INSERT INTO sales.sales_order_status_history (sales_order_id, from_status, to_status, reason)
        VALUES (NEW.sales_order_id, OLD.status_code, NEW.status_code,
                NULLIF(current_setting('app.status_reason', true), ''));
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER credit_terms_authority ON sales.customer_credit_terms;
DROP FUNCTION sales.tg_credit_terms_authority();
DROP TRIGGER check_authority ON sales.sales_order_check;
DROP FUNCTION sales.tg_sales_order_check_authority();
DROP FUNCTION sales.require_permission(text, text);
DROP FUNCTION sales.current_employee_id();

CREATE OR REPLACE FUNCTION audit.current_actor() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(NULLIF(current_setting('app.current_user', true), ''), session_user::text)
$$;

DROP FUNCTION audit.is_personal_login();
DROP FUNCTION audit.personal_logins_enforced();

DROP TABLE sales.employee_permission;
DROP TABLE sales.permission;
-- The CFO employee row is left in place: it may be referenced by orders or audit history.

ALTER TABLE sales.sales_order DROP CONSTRAINT sales_order_margin_reason_not_blank;
ALTER TABLE sales.sales_order_line DROP COLUMN margin_rationale;

UPDATE sales.policy_setting SET numeric_value = 7 WHERE setting_key = 'max_fx_rate_age_days';
INSERT INTO sales.policy_setting (setting_key, numeric_value, description)
VALUES ('min_order_gm_pct', 20, 'Orders below this gross margin % need margin_exception_reason');
