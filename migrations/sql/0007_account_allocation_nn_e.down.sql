-- Reverses 0007: restores the 0006 NN/E rule (12-month window).
DROP VIEW sales.v_register_salesperson_history;
DELETE FROM sales.exception_rule WHERE rule_code IN ('NO_ACCOUNT_ALLOCATION', 'SALESPERSON_NOT_ACCOUNT_OWNER');
UPDATE sales.exception_rule SET description = 'NN/E suffix disagrees with customer history (warning)', introduced_in = '0006'
 WHERE rule_code = 'REPORTING_CATEGORY_MISMATCH';
UPDATE sales.reporting_category SET description = CASE reporting_category_code
        WHEN 'EXPANSION_NN' THEN 'Expansion - net new customer'
        WHEN 'EXPANSION_E'  THEN 'Expansion - existing customer'
        WHEN 'CHURN_NN'     THEN 'Churn - net new customer'
        WHEN 'CHURN_E'      THEN 'Churn - existing customer'
        ELSE description END
 WHERE reporting_category_code IN ('EXPANSION_NN', 'EXPANSION_E', 'CHURN_NN', 'CHURN_E');
INSERT INTO sales.policy_setting (setting_key, numeric_value, description) VALUES
    ('new_customer_window_months', 12,
     'A customer counts as net new (NN) until this many months after its first order. 0 = only the first order is NN.');
DROP VIEW sales.v_sales_order_exception_all;
DROP VIEW sales.v_sales_order_exception_0006;
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


CREATE VIEW sales.v_sales_order_exception_all AS
SELECT * FROM sales.v_sales_order_exception
UNION ALL
SELECT * FROM sales.v_sales_order_exception_0004
UNION ALL
SELECT * FROM sales.v_sales_order_exception_0006;
DROP FUNCTION sales.customer_pre_existing_at(bigint, date);
DROP FUNCTION sales.account_allocation_on(bigint, date);
DROP TABLE sales.customer_account_allocation;
