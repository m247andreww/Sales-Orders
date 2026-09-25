-- =============================================================================
-- 0007 NN / E definition (CFO, 2026-09-24):
--   "Net new = the customer was NOT pre-existing at the time the salesperson was allocated
--    to the account."  NN if the customer had no order before the allocation date, else E.
-- Replaces the 12-month window of 0006. Requires the account-ownership history, held here in
-- sales.customer_account_allocation (one owner at a time per customer; periods cannot overlap).
-- Tested against the Register using first-order-per-salesperson as a proxy for the allocation
-- date: 284 of 302 NN/E rows agree (94%), versus under 40% for the earlier definitions.
-- =============================================================================

CREATE TABLE sales.customer_account_allocation (
    customer_account_allocation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id        bigint NOT NULL REFERENCES sales.customer,
    employee_id        bigint REFERENCES sales.employee,   -- the salesperson / account manager
    house_account      text,                               -- or a non-person owner: House, Legacy, Auto Renew...
    allocated_from     date   NOT NULL,
    allocated_to       date,                               -- NULL = current owner
    source             text   NOT NULL,                    -- 'CRM', 'CFO', 'inferred from Register (confirmed)', ...
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         text        NOT NULL DEFAULT audit.current_actor(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    updated_by         text        NOT NULL DEFAULT audit.current_actor(),
    row_version        integer     NOT NULL DEFAULT 1,
    CHECK ((employee_id IS NULL) <> (house_account IS NULL)),
    CHECK (house_account IS NULL OR btrim(house_account) <> ''),
    CHECK (allocated_to IS NULL OR allocated_to >= allocated_from),
    CONSTRAINT customer_account_allocation_no_overlap EXCLUDE USING gist (
        customer_id WITH =,
        daterange(allocated_from, allocated_to, '[]') WITH &&
    )
);
COMMENT ON TABLE sales.customer_account_allocation IS
    'Who owns each customer account and from when. Drives NN/E (net new vs existing) and salesperson checks.';
SELECT sales.attach_standard_triggers('sales.customer_account_allocation');

-- Account owner in force on a date.
CREATE FUNCTION sales.account_allocation_on(p_customer_id bigint, p_on date)
RETURNS sales.customer_account_allocation
LANGUAGE sql STABLE AS $$
    SELECT a.* FROM sales.customer_account_allocation a
     WHERE a.customer_id = p_customer_id
       AND a.allocated_from <= p_on AND (a.allocated_to IS NULL OR a.allocated_to >= p_on)
$$;

-- Did the customer have any order (this database or the Register, excluding LO/CA reversals)
-- strictly before the given date?
CREATE FUNCTION sales.customer_pre_existing_at(p_customer_id bigint, p_at date) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT sales.customer_first_order_date(p_customer_id) < p_at
$$;

DELETE FROM sales.policy_setting WHERE setting_key = 'new_customer_window_months';

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
                ELSE NOT COALESCE(sales.customer_pre_existing_at(o.customer_id, al.allocated_from), false) END AS is_net_new
      FROM o LEFT JOIN LATERAL sales.account_allocation_on(o.customer_id, o.order_date) al ON true
)
SELECT a.sales_order_id, a.order_number, 'REPORTING_CATEGORY_MISMATCH' AS rule_code, 'warning' AS severity,
       format('Reporting category %s but the customer %s pre-existing when the account was allocated on %s: expected %s',
              a.reporting_category_code, CASE WHEN a.is_net_new THEN 'was not' ELSE 'was' END, a.allocated_from,
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
 WHERE a.allocation_id IS NOT NULL AND a.owner_employee_id IS DISTINCT FROM a.account_manager_employee_id;


UPDATE sales.exception_rule SET description = 'NN/E suffix disagrees with whether the customer pre-existed the account allocation (warning)',
       introduced_in = '0007'
 WHERE rule_code = 'REPORTING_CATEGORY_MISMATCH';
INSERT INTO sales.exception_rule (rule_code, description, blocks_approval, introduced_in) VALUES
    ('NO_ACCOUNT_ALLOCATION',         'NN/E-coded order but no account owner recorded, so it cannot be verified (warning)', false, '0007'),
    ('SALESPERSON_NOT_ACCOUNT_OWNER', 'Order salesperson differs from the account owner on the order date (warning)',       false, '0007');

UPDATE sales.reporting_category SET description = CASE reporting_category_code
        WHEN 'EXPANSION_NN' THEN 'Expansion - net new: customer not pre-existing when the salesperson was allocated'
        WHEN 'EXPANSION_E'  THEN 'Expansion - existing: customer pre-existed the salesperson allocation'
        WHEN 'CHURN_NN'     THEN 'Churn - net new: customer not pre-existing when the salesperson was allocated'
        WHEN 'CHURN_E'      THEN 'Churn - existing: customer pre-existed the salesperson allocation'
        ELSE description END
 WHERE reporting_category_code IN ('EXPANSION_NN', 'EXPANSION_E', 'CHURN_NN', 'CHURN_E');

-- Evidence to help build the allocation history from the Register: first and last order each
-- salesperson handled per customer. A PROPOSAL for a person to confirm, never loaded automatically.
CREATE VIEW sales.v_register_salesperson_history AS
SELECT sales.normalised_name(r.client) AS client_key, min(r.client) AS client, r.salesperson,
       min(r.date_issued) AS first_order, max(r.date_issued) AS last_order, count(*) AS orders
  FROM sales.register_entry r
 WHERE r.sn_ref !~ '(LO|CA)$' AND r.salesperson IS NOT NULL AND r.date_issued IS NOT NULL
 GROUP BY sales.normalised_name(r.client), r.salesperson;
