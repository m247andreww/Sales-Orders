-- =============================================================================
-- 0008 House and Legacy accounts (CFO, 2026-09-24):
--   House  = controlled by finance; not allocated to a salesperson. When a salesperson leaves, ALL
--            their accounts go to House until a new salesperson is allocated (CFO policy).
--   Legacy = historic Register label for accounts of salespeople who left before that policy.
--   Neither is allocated to a salesperson, so orders on them are Existing (E), never Net New.
-- Non-person owners are now a controlled list. "Auto Renew" and "Cust Success" (which appear on
-- the Register) are deliberately NOT added until the CFO defines them.
-- Adds two routines, each one audited transaction:
--   sales.employee_leaves()   closes the leaver's accounts on their last day, moves them to House, deactivates them;
--   sales.allocate_account()  hands an account (e.g. from House) to a salesperson from a date.
-- =============================================================================

CREATE TABLE sales.house_account_type (
    house_account text PRIMARY KEY,
    description   text NOT NULL,
    responsible   text NOT NULL
);
INSERT INTO sales.house_account_type VALUES
    ('House',  'Controlled by finance; not allocated to a salesperson. Leavers'' accounts go here until reallocated', 'Finance'),
    ('Legacy', 'Historic only: accounts of salespeople who left before the House policy (24 Sep 2026)',              'Finance');

ALTER TABLE sales.customer_account_allocation
    ADD CONSTRAINT customer_account_allocation_house_fk FOREIGN KEY (house_account)
        REFERENCES sales.house_account_type;

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

INSERT INTO sales.exception_rule (rule_code, description, blocks_approval, introduced_in) VALUES
    ('OWNER_HAS_LEFT', 'Account owner has left and the account has not been moved to House (warning)', false, '0008');

-- Leaver routine: one call, one transaction, fully audited.
CREATE FUNCTION sales.employee_leaves(p_email text, p_last_day date) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_employee bigint;
    v_moved    integer := 0;
    r          record;
BEGIN
    SELECT employee_id INTO v_employee FROM sales.employee WHERE lower(email) = lower(p_email);
    IF v_employee IS NULL THEN
        RAISE EXCEPTION 'unknown employee %', p_email USING ERRCODE = 'no_data_found';
    END IF;
    FOR r IN SELECT customer_account_allocation_id, customer_id FROM sales.customer_account_allocation
              WHERE employee_id = v_employee AND (allocated_to IS NULL OR allocated_to > p_last_day)
                AND allocated_from <= p_last_day
    LOOP
        UPDATE sales.customer_account_allocation SET allocated_to = p_last_day
         WHERE customer_account_allocation_id = r.customer_account_allocation_id;
        INSERT INTO sales.customer_account_allocation (customer_id, house_account, allocated_from, source)
        VALUES (r.customer_id, 'House', p_last_day + 1, format('Leaver: %s (last day %s)', p_email, p_last_day));
        v_moved := v_moved + 1;
    END LOOP;
    UPDATE sales.employee SET is_active = false WHERE employee_id = v_employee AND is_active;
    RETURN v_moved;
END
$$;

-- Allocation routine: from p_from the account belongs to p_email; the current owner (House or a
-- salesperson) is closed the day before. Refuses a leaver or a date inside a closed period.
CREATE FUNCTION sales.allocate_account(p_customer_id bigint, p_email text, p_from date, p_source text)
RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_employee bigint;
BEGIN
    SELECT employee_id INTO v_employee FROM sales.employee WHERE lower(email) = lower(p_email) AND is_active;
    IF v_employee IS NULL THEN
        RAISE EXCEPTION 'unknown or inactive employee %', p_email USING ERRCODE = 'no_data_found';
    END IF;
    IF EXISTS (SELECT 1 FROM sales.customer_account_allocation
                WHERE customer_id = p_customer_id AND allocated_from >= p_from) THEN
        RAISE EXCEPTION 'account already has an allocation starting on or after %', p_from
            USING ERRCODE = 'check_violation';
    END IF;
    UPDATE sales.customer_account_allocation SET allocated_to = p_from - 1
     WHERE customer_id = p_customer_id AND allocated_from < p_from
       AND (allocated_to IS NULL OR allocated_to >= p_from);
    INSERT INTO sales.customer_account_allocation (customer_id, employee_id, allocated_from, source)
    VALUES (p_customer_id, v_employee, p_from, p_source);
END
$$;
