-- =============================================================================
-- 0006 CFO decisions of 2026-09-24 (second round)
--   1. This database is the system of record from today; the SQLite build is defunct.
--   3. Reporting category suffixes: NN = net new customer, E = existing customer.
--      Historic Register coding does not follow this consistently (see ADR 0003), so the
--      "new customer" window is a policy setting and a mismatch is a WARNING, never a block.
--   Also: sales.exception_rule catalogue; the approval gate blocks only on rules flagged blocks_approval.
--   4. The ARR ref follows AFTER the order is processed: a missing ARR ref no longer blocks
--      approval; it is a warning before processing and an ERROR (monitored) afterwards.
--      Linking the ARR contract is the one change allowed on a locked line, and posts ARR.
-- =============================================================================

-- ---------------------------------------------------------------- rule catalogue
-- Every exception rule, what it means, and whether an ERROR from it blocks approval.
-- The approval gate reads blocks_approval, so a rule's effect is data, not buried in code.
CREATE TABLE sales.exception_rule (
    rule_code       text PRIMARY KEY CHECK (rule_code ~ '^[A-Z][A-Z_]+$'),
    description     text    NOT NULL,
    blocks_approval boolean NOT NULL,
    introduced_in   text    NOT NULL
);
INSERT INTO sales.exception_rule (rule_code, description, blocks_approval, introduced_in) VALUES
    ('NO_LINES',                    'Order has no lines',                                                true,  '0001'),
    ('TAX_LINE_MARKED_UP',          'Tax pass-through line not sold at cost',                            true,  '0001'),
    ('STATED_TOTAL_MISMATCH',       'Emailed totals differ from computed totals beyond tolerance',       true,  '0001'),
    ('MISSING_SIGNED_ORDER',        'No signed order document attached',                                 true,  '0001'),
    ('NO_CREDIT_TERMS',             'Customer has no credit terms in force',                             true,  '0001'),
    ('CUSTOMER_CREDIT_RISK',        'Customer risk rating elevated/high (warning)',                      false, '0001'),
    ('SUPPLIER_NOT_APPROVED',       'Supplier has no approved account',                                  true,  '0001'),
    ('STALE_FX_RATE',               'FX rate older than policy (warning)',                               false, '0001'),
    ('CHECK_FAILED',                'A pre-processing check failed',                                     true,  '0001'),
    ('LOSS_LINE_NO_RATIONALE',      'Line sold below cost with no rationale',                            true,  '0003'),
    ('ORDER_LOSS_NO_RATIONALE',     'Order sold below cost with no order-level rationale',               true,  '0003'),
    ('SN_NOT_ASSIGNED',             'No SN reference sourced from the AW SOs Register',                  true,  '0004'),
    ('NO_GL_CODE',                  'Line missing revenue or cost GL code',                              true,  '0004'),
    ('NO_SERVICE_CATEGORY',         'Line missing service category',                                     true,  '0004'),
    ('ARR_LINE_NO_ARR_REF',         'Recurring line has no ARR ref. Follows processing (CFO 2026-09-24): warning before, monitored error after; never blocks', false, '0006'),
    ('ARR_CONTRACT_OTHER_CUSTOMER', 'ARR contract belongs to another customer',                          true,  '0004'),
    ('RECURRING_LINE_NO_DATES',     'Recurring line has no service dates (warning)',                     false, '0004'),
    ('REPORTING_CATEGORY_MISMATCH', 'NN/E suffix disagrees with customer history (warning)',             false, '0006');

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
                         JOIN sales.exception_rule r USING (rule_code)
                        WHERE e.sales_order_id = NEW.sales_order_id AND e.severity = 'error'
                          AND r.blocks_approval) THEN
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

-- ---------------------------------------------------------------- 3. NN / E
UPDATE sales.reporting_category SET definition_confirmed = true, description = CASE reporting_category_code
        WHEN 'EXPANSION_NN' THEN 'Expansion - net new customer'
        WHEN 'EXPANSION_E'  THEN 'Expansion - existing customer'
        WHEN 'CHURN_NN'     THEN 'Churn - net new customer'
        WHEN 'CHURN_E'      THEN 'Churn - existing customer'
        ELSE description END
 WHERE reporting_category_code IN ('EXPANSION_NN', 'EXPANSION_E', 'CHURN_NN', 'CHURN_E');

INSERT INTO sales.policy_setting (setting_key, numeric_value, description) VALUES
    ('new_customer_window_months', 12,
     'A customer counts as net new (NN) until this many months after its first order. 0 = only the first order is NN.');

-- A customer's first order date: earliest of this database's orders and the AW SOs Register
-- (LO/CA reversal rows excluded), matched on normalised customer names.
CREATE FUNCTION sales.customer_first_order_date(p_customer_id bigint) RETURNS date
LANGUAGE sql STABLE AS $$
    SELECT min(d) FROM (
        SELECT COALESCE(o.signed_date, o.received_at::date) AS d
          FROM sales.sales_order o
         WHERE o.customer_id = p_customer_id AND o.status_code <> 'cancelled'
        UNION ALL
        SELECT r.date_issued
          FROM sales.register_entry r JOIN sales.customer c ON c.customer_id = p_customer_id
         WHERE r.sn_ref !~ '(LO|CA)$'
           AND sales.normalised_name(r.client) IN (sales.normalised_name(c.legal_name),
                                                   sales.normalised_name(c.trading_name),
                                                   sales.normalised_name(c.xero_tracking_customer))
    ) x
$$;

CREATE VIEW sales.v_sales_order_exception_0006 AS
WITH o AS (
    SELECT o.sales_order_id, o.order_number, o.reporting_category_code,
           COALESCE(o.signed_date, o.received_at::date) AS order_date,
           sales.customer_first_order_date(o.customer_id) AS first_order_date,
           (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'new_customer_window_months') AS window_months
      FROM sales.sales_order o
     WHERE o.reporting_category_code ~ '_(NN|E)$' AND o.status_code <> 'cancelled'
), c AS (
    SELECT o.*,
           (o.first_order_date IS NULL OR o.first_order_date >= o.order_date
            OR o.order_date < o.first_order_date + make_interval(months => o.window_months::integer)) AS is_net_new
      FROM o
)
SELECT c.sales_order_id, c.order_number, 'REPORTING_CATEGORY_MISMATCH' AS rule_code, 'warning' AS severity,
       format('Reporting category %s but customer is %s (first order %s, net-new window %s months): expected %s',
              c.reporting_category_code, CASE WHEN c.is_net_new THEN 'net new' ELSE 'existing' END,
              COALESCE(c.first_order_date::text, 'none'), c.window_months,
              regexp_replace(c.reporting_category_code, '_(NN|E)$', CASE WHEN c.is_net_new THEN '_NN' ELSE '_E' END)) AS message
  FROM c
 WHERE (c.reporting_category_code ~ '_NN$') <> c.is_net_new;

-- ---------------------------------------------------------------- 4. ARR ref follows processing
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
SELECT l.sales_order_id, o.order_number, 'ARR_LINE_NO_ARR_REF',
       -- Not blocking at approval (the ARR ref follows processing); an ERROR once processed.
       CASE WHEN st.lines_locked THEN 'error' ELSE 'warning' END,
       format('Recurring line %s (%s) has no ARR reference', l.line_number, l.description)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
  JOIN sales.order_status st ON st.status_code = o.status_code
 WHERE l.arr_treatment_code = 'arr' AND l.arr_contract_id IS NULL
   AND o.order_type_code <> 'LAST_ORDER' AND o.status_code <> 'cancelled'
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


CREATE OR REPLACE VIEW sales.v_sales_order_exception_all AS
SELECT * FROM sales.v_sales_order_exception
UNION ALL
SELECT * FROM sales.v_sales_order_exception_0004
UNION ALL
SELECT * FROM sales.v_sales_order_exception_0006;

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
    v_customer    bigint;
    v_arr_owner   bigint;
BEGIN
    v_order_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.sales_order_id ELSE NEW.sales_order_id END;

    SELECT o.currency_code, o.order_number, o.status_code, s.lines_locked, o.customer_id
      INTO v_order_ccy, v_order_no, v_status, v_locked, v_customer
      FROM sales.sales_order o JOIN sales.order_status s USING (status_code)
     WHERE o.sales_order_id = v_order_id;

    -- CFO decision 4 (2026-09-24): the ARR ref follows AFTER processing, so linking an ARR contract
    -- to a line that has none is the ONE change allowed on a locked line. Everything else stays frozen.
    IF v_locked AND TG_OP = 'UPDATE'
       AND OLD.arr_contract_id IS NULL AND NEW.arr_contract_id IS NOT NULL
       -- Generated money columns are excluded: they are not yet computed in a BEFORE trigger, and
       -- they derive entirely from inputs that ARE compared (quantity, periods, prices, fx_rate).
       AND (to_jsonb(NEW) - ARRAY['arr_contract_id', 'updated_at', 'updated_by', 'row_version',
                                  'unit_cost', 'net_cost', 'net_sell', 'gross_margin'])
           = (to_jsonb(OLD) - ARRAY['arr_contract_id', 'updated_at', 'updated_by', 'row_version',
                                    'unit_cost', 'net_cost', 'net_sell', 'gross_margin']) THEN
        v_locked := false;
    END IF;
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

    IF NEW.arr_contract_id IS NOT NULL THEN
        SELECT customer_id INTO v_arr_owner FROM sales.arr_contract WHERE arr_contract_id = NEW.arr_contract_id;
        IF v_arr_owner <> v_customer THEN
            RAISE EXCEPTION 'line %: ARR contract belongs to a different customer', NEW.line_number
                USING ERRCODE = 'check_violation';
        END IF;
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

-- When an ARR contract is linked to a line of an already-processed order, post its ARR now.
CREATE FUNCTION sales.tg_line_post_arr_on_link() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM sales.sales_order o JOIN sales.order_status s USING (status_code)
                WHERE o.sales_order_id = NEW.sales_order_id AND s.lines_locked AND o.status_code <> 'cancelled') THEN
        PERFORM sales.post_arr_movements(NEW.sales_order_id);
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER post_arr_on_link
    AFTER UPDATE OF arr_contract_id ON sales.sales_order_line
    FOR EACH ROW WHEN (OLD.arr_contract_id IS NULL AND NEW.arr_contract_id IS NOT NULL)
    EXECUTE FUNCTION sales.tg_line_post_arr_on_link();

-- Monitoring report: processed recurring lines still waiting for their ARR ref.
CREATE VIEW sales.v_arr_ref_outstanding AS
SELECT o.sales_order_id, o.order_number, o.sn_ref, c.legal_name AS customer_name, l.line_number, l.description,
       l.sku, round(l.quantity * l.unit_sell / bf.months_per_period, 2) AS mrr_not_in_arr,
       ap.approved_at, (current_date - ap.approved_at::date) AS days_outstanding
  FROM sales.sales_order_line l
  JOIN sales.sales_order o USING (sales_order_id)
  JOIN sales.customer c USING (customer_id)
  JOIN sales.billing_frequency bf USING (billing_frequency_code)
  JOIN sales.order_status st ON st.status_code = o.status_code
  LEFT JOIN LATERAL (SELECT max(h.changed_at) AS approved_at FROM sales.sales_order_status_history h
                      WHERE h.sales_order_id = o.sales_order_id AND h.to_status = 'approved') ap ON true
 WHERE l.arr_treatment_code = 'arr' AND l.arr_contract_id IS NULL
   AND st.lines_locked AND o.status_code <> 'cancelled' AND o.order_type_code <> 'LAST_ORDER';
COMMENT ON VIEW sales.v_arr_ref_outstanding IS
    'Processed recurring lines with no ARR ref: MRR missing from the ARR ledger. Review daily.';
