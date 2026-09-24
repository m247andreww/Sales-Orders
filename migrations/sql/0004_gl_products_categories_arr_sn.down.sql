-- Reverses 0004. Destroys GL mirror, ARR ledger and Register mirror data.
DROP TRIGGER sn_must_be_on_register ON sales.sales_order;
DROP FUNCTION sales.tg_sn_must_be_on_register();
DROP FUNCTION sales.assign_sn_from_register(bigint);
DROP VIEW sales.v_sn_candidate;
DROP TABLE sales.register_entry;
DROP TABLE sales.register_sync_rejection;
DROP TABLE sales.register_sync;
DROP FUNCTION sales.normalised_name(text);

-- Restore the 0003 status trigger (reads the 0003 exception view only) and 0001 lock trigger.
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

CREATE OR REPLACE FUNCTION sales.tg_sales_order_lock() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT lines_locked FROM sales.order_status WHERE status_code = OLD.status_code)
       AND (NEW.customer_id, NEW.currency_code, NEW.order_type_code, NEW.price_list_id, NEW.margin_exception_reason)
           IS DISTINCT FROM
           (OLD.customer_id, OLD.currency_code, OLD.order_type_code, OLD.price_list_id, OLD.margin_exception_reason)
    THEN
        RAISE EXCEPTION 'order %: commercial fields are locked in status %', OLD.order_number, OLD.status_code
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DROP VIEW sales.v_sales_order_exception_all;
DROP VIEW sales.v_sales_order_exception_0004;
DROP VIEW sales.v_arr_current;
DROP FUNCTION sales.arr_bridge(date, date);
DROP FUNCTION sales.arr_at(date);
DROP TRIGGER post_arr_on_approval ON sales.sales_order;
DROP FUNCTION sales.tg_sales_order_post_arr();
DROP FUNCTION sales.post_arr_movements(bigint);
DROP TABLE sales.order_type_arr_movement;
DROP TABLE sales.arr_movement;
DROP FUNCTION audit.tg_block_modification_named();
DROP TABLE sales.arr_movement_type;

-- Views depending on the dropped line columns must go first; rebuilt as in 0001 below.
DROP VIEW sales.v_sales_order_exception;
DROP VIEW sales.v_sales_order_summary;
DROP VIEW sales.v_sales_order_line;

DROP TRIGGER line_defaults_and_gl ON sales.sales_order_line;
DROP FUNCTION sales.tg_line_defaults_and_gl();
ALTER TABLE sales.sales_order_line
    DROP COLUMN service_category_code, DROP COLUMN revenue_gl_code, DROP COLUMN cost_gl_code,
    DROP COLUMN service_start_date, DROP COLUMN service_end_date, DROP COLUMN arr_treatment_code,
    DROP COLUMN arr_contract_id, DROP COLUMN supplier_po_number;
DROP TABLE sales.arr_contract;
-- (views are recreated at the end of this file)
DROP TABLE sales.arr_contract_status;
DROP TABLE sales.arr_treatment;

ALTER TABLE sales.product
    DROP COLUMN description, DROP COLUMN vendor_part_number, DROP COLUMN service_category_code,
    DROP COLUMN default_billing_frequency_code, DROP COLUMN default_revenue_gl_code,
    DROP COLUMN default_cost_gl_code, DROP COLUMN list_price, DROP COLUMN list_cost,
    DROP COLUMN pandadoc_catalog_item_id;
ALTER TABLE sales.customer DROP COLUMN xero_tracking_customer, DROP COLUMN arr_prefix;
ALTER TABLE sales.sales_order
    DROP CONSTRAINT sales_order_sn_has_source,
    DROP COLUMN sn_ref, DROP COLUMN sn_source, DROP COLUMN sn_assigned_at,
    DROP COLUMN order_document_type_code, DROP COLUMN reporting_category_code,
    DROP COLUMN previous_sales_order_id, DROP COLUMN ticket_reference, DROP COLUMN project,
    DROP COLUMN signed_by_customer, DROP COLUMN signed_by_managed247;

INSERT INTO sales.order_type (order_type_code, description) VALUES
    ('new', 'New customer or new service'),
    ('additional', 'Additional quantity / services for an existing customer'),
    ('renewal', 'Renewal of an existing term'),
    ('amendment', 'Change to a previously processed order');
UPDATE sales.sales_order SET order_type_code = CASE order_type_code
        WHEN 'NEW_ORDER' THEN 'new' WHEN 'VOLUME' THEN 'additional' WHEN 'RENEWAL' THEN 'renewal'
        ELSE 'amendment' END;
DELETE FROM sales.order_type WHERE order_type_code IN ('NEW_ORDER', 'VOLUME', 'RENEWAL', 'LAST_ORDER', 'CHURN');

DROP TABLE sales.order_document_type;
DROP TABLE sales.reporting_category;
DROP TABLE sales.service_category;
DROP TABLE sales.gl_account;

-- Rebuild the pre-0004 views (0001 line & summary definitions, 0003 exception rules).
CREATE VIEW sales.v_sales_order_line AS
SELECT l.sales_order_line_id, l.sales_order_id, l.line_number, l.product_id, l.sku, l.description,
       l.line_category_code, l.supplier_id, l.supplier_quote_id, l.quantity, l.billing_frequency_code,
       l.billing_periods, l.cost_currency_code, l.unit_cost_in_cost_currency, l.fx_rate_id, l.fx_rate,
       l.unit_sell, l.unit_cost, l.net_cost, l.net_sell, l.gross_margin, l.notes, l.created_at, l.created_by,
       l.updated_at, l.updated_by, l.row_version,
       bf.months_per_period,
       (bf.months_per_period IS NOT NULL)                                           AS is_recurring,
       CASE WHEN bf.months_per_period IS NOT NULL
            THEN round(l.quantity * l.unit_sell / bf.months_per_period, 2) END      AS monthly_recurring_sell,
       CASE WHEN bf.months_per_period IS NOT NULL
            THEN round(l.quantity * l.unit_cost / bf.months_per_period, 2) END      AS monthly_recurring_cost,
       CASE WHEN l.net_sell = 0 THEN NULL
            ELSE round(l.gross_margin / l.net_sell * 100, 2) END                    AS gross_margin_pct
  FROM sales.sales_order_line l
  JOIN sales.billing_frequency bf USING (billing_frequency_code);

CREATE VIEW sales.v_sales_order_summary AS
SELECT o.sales_order_id,
       o.order_number,
       o.title,
       o.status_code,
       o.order_type_code,
       c.customer_id,
       c.legal_name                                               AS customer_name,
       o.received_at,
       o.signed_date,
       o.currency_code,
       count(l.sales_order_line_id)                               AS line_count,
       COALESCE(sum(l.net_cost), 0)                               AS net_cost,
       COALESCE(sum(l.net_sell), 0)                               AS net_sell,
       COALESCE(sum(l.gross_margin), 0)                           AS gross_margin,
       CASE WHEN COALESCE(sum(l.net_sell), 0) = 0 THEN NULL
            ELSE round(sum(l.gross_margin) / sum(l.net_sell) * 100, 2) END AS gross_margin_pct,
       COALESCE(sum(l.net_sell)     FILTER (WHERE NOT l.is_recurring), 0) AS one_off_sell,
       COALESCE(sum(l.net_sell)     FILTER (WHERE l.is_recurring), 0)     AS recurring_contract_sell,
       COALESCE(sum(l.monthly_recurring_sell), 0)                 AS monthly_recurring_sell,
       COALESCE(sum(l.monthly_recurring_sell), 0)
         - COALESCE(sum(l.monthly_recurring_cost), 0)             AS monthly_recurring_margin,
       o.stated_net_cost,
       o.stated_net_sell,
       o.stated_gross_margin
  FROM sales.sales_order o
  JOIN sales.customer c USING (customer_id)
  LEFT JOIN sales.v_sales_order_line l USING (sales_order_id)
 GROUP BY o.sales_order_id, c.customer_id;

CREATE VIEW sales.v_sales_order_exception AS
WITH s AS (SELECT * FROM sales.v_sales_order_summary),
     p AS (SELECT
             (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'max_fx_rate_age_days') AS max_fx_age,
             (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'stated_total_tolerance') AS tolerance)
SELECT s.sales_order_id, s.order_number, 'NO_LINES' AS rule_code, 'error' AS severity,
       'Order has no lines' AS message
  FROM s WHERE s.line_count = 0
UNION ALL
SELECT l.sales_order_id, o.order_number, 'LOSS_LINE_NO_RATIONALE', 'error',
       format('Line %s (%s) sells below cost (margin %s) with no rationale', l.line_number, l.description, l.gross_margin)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.gross_margin < 0 AND l.margin_rationale IS NULL AND o.margin_exception_reason IS NULL
UNION ALL
SELECT s.sales_order_id, s.order_number, 'ORDER_LOSS_NO_RATIONALE', 'error',
       format('Order sells below cost overall (margin %s) with no order-level rationale', s.gross_margin)
  FROM s JOIN sales.sales_order o USING (sales_order_id)
 WHERE s.gross_margin < 0 AND o.margin_exception_reason IS NULL
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
