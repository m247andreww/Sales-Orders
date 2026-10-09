-- Restore the 0003 view (with NO_CREDIT_TERMS) and its catalogue entry.
CREATE OR REPLACE VIEW sales.v_sales_order_exception AS
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

INSERT INTO sales.exception_rule (rule_code, description, blocks_approval, introduced_in) VALUES
    ('NO_CREDIT_TERMS', 'Customer has no credit terms in force', true, '0001');
