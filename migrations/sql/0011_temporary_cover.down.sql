-- Reverses 0011: restores the 0010 rules and the 0004 lock.
CREATE OR REPLACE FUNCTION sales.take_account_for_order(p_sales_order_id bigint) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    o          record;
    v_cur      sales.customer_account_allocation;
    v_ref      text;
BEGIN
    SELECT so.customer_id, so.account_manager_employee_id AS sp, e.is_active, e.full_name,
           COALESCE(so.signed_date, so.received_at::date) AS d, COALESCE(so.sn_ref, so.order_number) AS ref
      INTO o
      FROM sales.sales_order so LEFT JOIN sales.employee e ON e.employee_id = so.account_manager_employee_id
     WHERE so.sales_order_id = p_sales_order_id;
    IF o.sp IS NULL THEN
        RETURN 'no salesperson on order';
    END IF;
    v_ref := o.ref;
    IF NOT o.is_active THEN
        INSERT INTO sales.account_history_review (customer_id, on_date, note)
        VALUES (o.customer_id, o.d, format('%s led by %s, who has left: ownership not changed', v_ref, o.full_name));
        RETURN 'salesperson inactive: logged';
    END IF;
    IF EXISTS (SELECT 1 FROM sales.customer_account_allocation
                WHERE customer_id = o.customer_id AND allocated_from > o.d) THEN
        INSERT INTO sales.account_history_review (customer_id, on_date, note)
        VALUES (o.customer_id, o.d, format('%s led by %s is dated before a later ownership change: ownership not changed',
                                           v_ref, o.full_name));
        RETURN 'later ownership change exists: logged';
    END IF;
    v_cur := sales.account_allocation_on(o.customer_id, o.d);
    IF v_cur.customer_account_allocation_id IS NOT NULL AND v_cur.employee_id = o.sp THEN
        RETURN 'already owner';
    END IF;
    IF v_cur.customer_account_allocation_id IS NOT NULL AND v_cur.allocated_from = o.d THEN
        UPDATE sales.customer_account_allocation
           SET employee_id = o.sp, house_account = NULL, source = format('Order %s (salesperson led the opportunity)', v_ref)
         WHERE customer_account_allocation_id = v_cur.customer_account_allocation_id;
        INSERT INTO sales.account_history_review (customer_id, on_date, note)
        VALUES (o.customer_id, o.d, format('Two salespeople on %s; %s (%s) taken as owner', o.d, o.full_name, v_ref));
        RETURN 'same-day owner replaced: logged';
    END IF;
    IF v_cur.customer_account_allocation_id IS NOT NULL THEN
        UPDATE sales.customer_account_allocation SET allocated_to = o.d - 1
         WHERE customer_account_allocation_id = v_cur.customer_account_allocation_id;
    END IF;
    INSERT INTO sales.customer_account_allocation (customer_id, employee_id, allocated_from, source)
    VALUES (o.customer_id, o.sp, o.d, format('Order %s (salesperson led the opportunity)', v_ref));
    RETURN 'ownership transferred';
END
$$;


CREATE OR REPLACE FUNCTION sales.tg_sales_order_lock() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT lines_locked FROM sales.order_status WHERE status_code = OLD.status_code)
       AND (NEW.customer_id, NEW.currency_code, NEW.order_type_code, NEW.price_list_id,
            NEW.margin_exception_reason, NEW.sn_ref, NEW.reporting_category_code)
           IS DISTINCT FROM
           (OLD.customer_id, OLD.currency_code, OLD.order_type_code, OLD.price_list_id,
            OLD.margin_exception_reason, OLD.sn_ref, OLD.reporting_category_code)
    THEN
        RAISE EXCEPTION 'order %: commercial fields are locked in status %', OLD.order_number, OLD.status_code
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
DELETE FROM sales.exception_rule WHERE rule_code = 'COVER_ORDER';
DROP VIEW sales.v_sales_order_exception_all;
DROP VIEW sales.v_sales_order_exception_0006;
CREATE VIEW sales.v_sales_order_exception_0006 AS
WITH o AS (
    SELECT o.sales_order_id, o.order_number, o.customer_id, o.reporting_category_code,
           o.account_manager_employee_id,
           COALESCE(o.signed_date, o.received_at::date) AS order_date
      FROM sales.sales_order o
     WHERE o.status_code <> 'cancelled'
), a AS (
    SELECT o.*, al.customer_account_allocation_id AS allocation_id, al.employee_id AS owner_employee_id,
           al.house_account, al.allocated_from,
           -- the salesperson this order is judged for: named on the order, else the account owner
           COALESCE(o.account_manager_employee_id, al.employee_id) AS judged_employee_id
      FROM o LEFT JOIN LATERAL sales.account_allocation_on(o.customer_id, o.order_date) al ON true
), j AS (
    SELECT a.*,
           CASE WHEN a.judged_employee_id IS NULL THEN NULL
                ELSE COALESCE((SELECT min(x.allocated_from) FROM sales.customer_account_allocation x
                                WHERE x.customer_id = a.customer_id AND x.employee_id = a.judged_employee_id),
                              a.order_date) END AS salesperson_from
      FROM a
), n AS (
    SELECT j.*,
           CASE WHEN j.judged_employee_id IS NOT NULL
                     THEN NOT COALESCE(sales.customer_pre_existing_at(j.customer_id, j.salesperson_from), false)
                -- no salesperson: House (finance) and Legacy accounts cannot be net new
                WHEN j.house_account IS NOT NULL THEN false
                ELSE NULL END AS is_net_new
      FROM j
)
SELECT n.sales_order_id, n.order_number, 'REPORTING_CATEGORY_MISMATCH' AS rule_code, 'warning' AS severity,
       format('Reporting category %s but %s: expected %s',
              n.reporting_category_code,
              CASE WHEN n.judged_employee_id IS NULL
                   THEN format('the account is %s (not allocated to a salesperson), so it cannot be net new', n.house_account)
                   ELSE format('the customer %s pre-existing when %s took the account on %s',
                               CASE WHEN n.is_net_new THEN 'was not' ELSE 'was' END,
                               (SELECT e.full_name FROM sales.employee e WHERE e.employee_id = n.judged_employee_id),
                               n.salesperson_from) END,
              regexp_replace(n.reporting_category_code, '_(NN|E)$', CASE WHEN n.is_net_new THEN '_NN' ELSE '_E' END)) AS message
  FROM n
 WHERE n.reporting_category_code ~ '_(NN|E)$' AND n.is_net_new IS NOT NULL
   AND (n.reporting_category_code ~ '_NN$') <> n.is_net_new
UNION ALL
SELECT n.sales_order_id, n.order_number, 'NO_ACCOUNT_ALLOCATION', 'warning',
       format('No salesperson on the order and no account owner recorded on %s: NN/E (%s) cannot be verified',
              n.order_date, n.reporting_category_code)
  FROM n
 WHERE n.reporting_category_code ~ '_(NN|E)$' AND n.is_net_new IS NULL
UNION ALL
SELECT n.sales_order_id, n.order_number, 'SALESPERSON_NOT_ACCOUNT_OWNER', 'warning',
       format('Account owner on %s is %s; on approval the account passes to %s, who led this order',
              n.order_date, COALESCE(ow.full_name, n.house_account), sp.full_name)
  FROM n
  JOIN sales.employee sp ON sp.employee_id = n.account_manager_employee_id
  LEFT JOIN sales.employee ow ON ow.employee_id = n.owner_employee_id
 WHERE n.allocation_id IS NOT NULL AND n.owner_employee_id IS DISTINCT FROM n.account_manager_employee_id
UNION ALL
SELECT n.sales_order_id, n.order_number, 'OWNER_HAS_LEFT', 'warning',
       format('Account owner %s has left: run the leaver routine to move the account to House (sales-orders employee-leaves)', ow.full_name)
  FROM n
  JOIN sales.employee ow ON ow.employee_id = n.owner_employee_id
 WHERE NOT ow.is_active;


CREATE VIEW sales.v_sales_order_exception_all AS
SELECT * FROM sales.v_sales_order_exception
UNION ALL
SELECT * FROM sales.v_sales_order_exception_0004
UNION ALL
SELECT * FROM sales.v_sales_order_exception_0006;
DROP FUNCTION sales.order_cover_for(bigint);
ALTER TABLE sales.sales_order DROP CONSTRAINT sales_order_cover_not_self, DROP COLUMN covering_for_employee_id;
DROP TABLE sales.employee_absence;
