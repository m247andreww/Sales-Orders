-- Reverses 0006.
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
        IF NEW.status_code = 'approved' THEN
            PERFORM sales.require_permission('approve_order', format('approving order %s', NEW.order_number));
            IF EXISTS (SELECT 1 FROM sales.v_sales_order_exception_all e
                        WHERE e.sales_order_id = NEW.sales_order_id AND e.severity = 'error') THEN
                RAISE EXCEPTION 'order % cannot be approved: it has error-level exceptions (see sales.v_sales_order_exception_all)',
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
DROP TABLE sales.exception_rule;
DROP VIEW sales.v_arr_ref_outstanding;
DROP TRIGGER post_arr_on_link ON sales.sales_order_line;
DROP FUNCTION sales.tg_line_post_arr_on_link();
CREATE OR REPLACE FUNCTION sales.tg_sales_order_line_validate() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_order_ccy   char(3);
    v_order_no    text;
    v_status      text;
    v_locked      boolean;
    v_fx          sales.fx_rate%ROWTYPE;
    v_quote_supp  bigint;
    v_order_id    bigint;
BEGIN
    v_order_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.sales_order_id ELSE NEW.sales_order_id END;

    SELECT o.currency_code, o.order_number, o.status_code, s.lines_locked
      INTO v_order_ccy, v_order_no, v_status, v_locked
      FROM sales.sales_order o JOIN sales.order_status s USING (status_code)
     WHERE o.sales_order_id = v_order_id;

    IF v_locked THEN
        RAISE EXCEPTION 'order % is in status % and its lines are locked', v_order_no, v_status
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.sales_order_id <> OLD.sales_order_id THEN
        RAISE EXCEPTION 'a line cannot be moved to a different order' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    IF NEW.cost_currency_code = v_order_ccy THEN
        IF NEW.fx_rate_id IS NOT NULL THEN
            RAISE EXCEPTION 'line %: fx_rate_id given but cost currency equals order currency (%)',
                NEW.line_number, v_order_ccy USING ERRCODE = 'check_violation';
        END IF;
        NEW.fx_rate := 1;
    ELSE
        IF NEW.fx_rate_id IS NULL THEN
            RAISE EXCEPTION 'line %: cost in % needs an FX rate to %', NEW.line_number,
                NEW.cost_currency_code, v_order_ccy USING ERRCODE = 'check_violation';
        END IF;
        SELECT * INTO v_fx FROM sales.fx_rate WHERE fx_rate_id = NEW.fx_rate_id;
        IF v_fx.from_currency_code <> NEW.cost_currency_code OR v_fx.to_currency_code <> v_order_ccy THEN
            RAISE EXCEPTION 'line %: FX rate % is %->%, expected %->%', NEW.line_number, v_fx.fx_rate_id,
                v_fx.from_currency_code, v_fx.to_currency_code, NEW.cost_currency_code, v_order_ccy
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.fx_rate := v_fx.rate;
    END IF;

    IF NEW.supplier_quote_id IS NOT NULL THEN
        SELECT supplier_id INTO v_quote_supp FROM sales.supplier_quote
         WHERE supplier_quote_id = NEW.supplier_quote_id;
        IF v_quote_supp <> NEW.supplier_id THEN
            RAISE EXCEPTION 'line %: supplier quote % belongs to a different supplier',
                NEW.line_number, NEW.supplier_quote_id USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END
$$;

CREATE OR REPLACE VIEW sales.v_sales_order_exception_all AS
SELECT * FROM sales.v_sales_order_exception
UNION ALL
SELECT * FROM sales.v_sales_order_exception_0004;
CREATE OR REPLACE VIEW sales.v_sales_order_exception_0004 AS
SELECT o.sales_order_id, o.order_number, 'SN_NOT_ASSIGNED' AS rule_code, 'error' AS severity,
       'No SN reference yet: source it from the AW SOs Register before approval' AS message
  FROM sales.sales_order o
 WHERE o.sn_ref IS NULL AND o.status_code NOT IN ('cancelled')
UNION ALL
SELECT l.sales_order_id, o.order_number, 'NO_GL_CODE', 'error',
       format('Line %s (%s) has no %s GL code', l.line_number, l.description,
              concat_ws(' or ', CASE WHEN l.revenue_gl_code IS NULL THEN 'revenue' END,
                                CASE WHEN l.cost_gl_code IS NULL THEN 'cost' END))
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.revenue_gl_code IS NULL OR l.cost_gl_code IS NULL
UNION ALL
SELECT l.sales_order_id, o.order_number, 'NO_SERVICE_CATEGORY', 'error',
       format('Line %s (%s) has no service category', l.line_number, l.description)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.service_category_code IS NULL
UNION ALL
SELECT l.sales_order_id, o.order_number, 'ARR_LINE_NO_ARR_REF', 'error',
       format('Recurring line %s (%s) has no ARR reference', l.line_number, l.description)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.arr_treatment_code = 'arr' AND l.arr_contract_id IS NULL
   AND o.order_type_code <> 'LAST_ORDER'
UNION ALL
SELECT l.sales_order_id, o.order_number, 'ARR_CONTRACT_OTHER_CUSTOMER', 'error',
       format('Line %s: ARR ref %s belongs to a different customer', l.line_number, c.arr_ref)
  FROM sales.sales_order_line l
  JOIN sales.sales_order o USING (sales_order_id)
  JOIN sales.arr_contract c USING (arr_contract_id)
 WHERE c.customer_id <> o.customer_id
UNION ALL
SELECT l.sales_order_id, o.order_number, 'RECURRING_LINE_NO_DATES', 'warning',
       format('Recurring line %s (%s) has no service start/end date', l.line_number, l.description)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.arr_treatment_code IN ('arr', 'stub') AND (l.service_start_date IS NULL OR l.service_end_date IS NULL);


DROP VIEW sales.v_sales_order_exception_0006;
DROP FUNCTION sales.customer_first_order_date(bigint);
DELETE FROM sales.policy_setting WHERE setting_key = 'new_customer_window_months';
UPDATE sales.reporting_category SET definition_confirmed = false, description = CASE reporting_category_code
        WHEN 'EXPANSION_NN' THEN 'Expansion (NN - definition to confirm)'
        WHEN 'EXPANSION_E'  THEN 'Expansion (E - definition to confirm)'
        WHEN 'CHURN_NN'     THEN 'Churn (NN - definition to confirm)'
        WHEN 'CHURN_E'      THEN 'Churn (E - definition to confirm)'
        ELSE description END;
