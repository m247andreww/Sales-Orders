-- Reverses 0010: restores the 0008 NN/E rules (judged against the account owner).
DROP TRIGGER take_account_on_approval ON sales.sales_order;
DROP FUNCTION sales.tg_sales_order_take_account();
DROP FUNCTION sales.take_account_for_order(bigint);
UPDATE sales.exception_rule SET introduced_in = '0007',
       description = 'Order salesperson differs from the account owner on the order date (warning)'
 WHERE rule_code = 'SALESPERSON_NOT_ACCOUNT_OWNER';
UPDATE sales.exception_rule SET introduced_in = '0007',
       description = 'NN/E suffix disagrees with whether the customer pre-existed the account allocation (warning)'
 WHERE rule_code = 'REPORTING_CATEGORY_MISMATCH';
UPDATE sales.exception_rule SET introduced_in = '0007',
       description = 'NN/E-coded order but no account owner recorded, so it cannot be verified (warning)'
 WHERE rule_code = 'NO_ACCOUNT_ALLOCATION';
CREATE OR REPLACE VIEW sales.v_sales_order_exception_0006 AS
WITH o AS (
    SELECT o.sales_order_id, o.order_number, o.customer_id, o.reporting_category_code,
           o.account_manager_employee_id,
           COALESCE(o.signed_date, o.received_at::date) AS order_date
      FROM sales.sales_order o
     WHERE o.status_code <> 'cancelled'
), a AS (
    SELECT o.*, al.customer_account_allocation_id AS allocation_id, al.employee_id AS owner_employee_id,
           al.house_account, al.allocated_from,
           CASE WHEN al.customer_account_allocation_id IS NULL THEN NULL
                -- House (finance) and Legacy (leaver) accounts are not allocated to a salesperson,
                -- so nothing on them can be net new (CFO, 2026-09-24).
                WHEN al.house_account IS NOT NULL THEN false
                ELSE NOT COALESCE(sales.customer_pre_existing_at(o.customer_id, al.allocated_from), false) END AS is_net_new
      FROM o LEFT JOIN LATERAL sales.account_allocation_on(o.customer_id, o.order_date) al ON true
)
SELECT a.sales_order_id, a.order_number, 'REPORTING_CATEGORY_MISMATCH' AS rule_code, 'warning' AS severity,
       format('Reporting category %s but %s: expected %s',
              a.reporting_category_code,
              CASE WHEN a.house_account IS NOT NULL
                   THEN format('the account is %s (not allocated to a salesperson), so it cannot be net new', a.house_account)
                   ELSE format('the customer %s pre-existing when the account was allocated on %s',
                               CASE WHEN a.is_net_new THEN 'was not' ELSE 'was' END, a.allocated_from) END,
              regexp_replace(a.reporting_category_code, '_(NN|E)$', CASE WHEN a.is_net_new THEN '_NN' ELSE '_E' END)) AS message
  FROM a
 WHERE a.reporting_category_code ~ '_(NN|E)$' AND a.is_net_new IS NOT NULL
   AND (a.reporting_category_code ~ '_NN$') <> a.is_net_new
UNION ALL
SELECT a.sales_order_id, a.order_number, 'NO_ACCOUNT_ALLOCATION', 'warning',
       format('No account owner recorded for this customer on %s: NN/E (%s) cannot be verified',
              a.order_date, a.reporting_category_code)
  FROM a
 WHERE a.reporting_category_code ~ '_(NN|E)$' AND a.allocation_id IS NULL
UNION ALL
SELECT a.sales_order_id, a.order_number, 'SALESPERSON_NOT_ACCOUNT_OWNER', 'warning',
       format('Order salesperson %s is not the account owner on %s (%s)',
              sp.full_name, a.order_date, COALESCE(ow.full_name, a.house_account))
  FROM a
  JOIN sales.employee sp ON sp.employee_id = a.account_manager_employee_id
  LEFT JOIN sales.employee ow ON ow.employee_id = a.owner_employee_id
 WHERE a.allocation_id IS NOT NULL AND a.owner_employee_id IS DISTINCT FROM a.account_manager_employee_id
UNION ALL
SELECT a.sales_order_id, a.order_number, 'OWNER_HAS_LEFT', 'warning',
       format('Account owner %s has left: run the leaver routine to move the account to House (sales-orders employee-leaves)', ow.full_name)
  FROM a
  JOIN sales.employee ow ON ow.employee_id = a.owner_employee_id
 WHERE NOT ow.is_active;

