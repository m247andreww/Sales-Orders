-- =============================================================================
-- 0011 Temporary cover (CFO, 2026-09-25): ownership follows the salesperson on each approved
-- order, EXCEPT when a colleague steps in temporarily (e.g. the owner is on holiday).
-- Cover is recognised two ways:
--   * explicitly: sales_order.covering_for_employee_id (the absent owner), or
--   * automatically: the account owner has a recorded absence covering the order date and a
--     different salesperson is named on the order.
-- A cover order leaves the account with its owner and NN/E is judged for the owner.
-- =============================================================================

CREATE TABLE sales.employee_absence (
    employee_absence_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    employee_id  bigint NOT NULL REFERENCES sales.employee,
    absent_from  date   NOT NULL,
    absent_to    date   NOT NULL,
    reason       text   NOT NULL DEFAULT 'holiday',
    source       text   NOT NULL,        -- 'HR', 'Outlook calendar', 'CFO', ...
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text        NOT NULL DEFAULT audit.current_actor(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text        NOT NULL DEFAULT audit.current_actor(),
    row_version  integer     NOT NULL DEFAULT 1,
    CHECK (absent_to >= absent_from),
    CONSTRAINT employee_absence_no_overlap EXCLUDE USING gist (
        employee_id WITH =, daterange(absent_from, absent_to, '[]') WITH &&)
);
COMMENT ON TABLE sales.employee_absence IS 'Salesperson absences, so orders led by a covering colleague are recognised.';
SELECT sales.attach_standard_triggers('sales.employee_absence');

ALTER TABLE sales.sales_order
    ADD COLUMN covering_for_employee_id bigint REFERENCES sales.employee,
    ADD CONSTRAINT sales_order_cover_not_self
        CHECK (covering_for_employee_id IS NULL OR covering_for_employee_id IS DISTINCT FROM account_manager_employee_id);
COMMENT ON COLUMN sales.sales_order.covering_for_employee_id IS
    'The absent salesperson this order was covered for (temporary cover: ownership does not transfer).';

-- The salesperson a cover order was covered for, or NULL if the order is not cover.
CREATE FUNCTION sales.order_cover_for(p_sales_order_id bigint) RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(
        o.covering_for_employee_id,
        (SELECT al.employee_id
           FROM sales.account_allocation_on(o.customer_id, COALESCE(o.signed_date, o.received_at::date)) al
          WHERE al.employee_id IS NOT NULL
            AND o.account_manager_employee_id IS NOT NULL
            AND al.employee_id IS DISTINCT FROM o.account_manager_employee_id
            AND EXISTS (SELECT 1 FROM sales.employee_absence ab
                         WHERE ab.employee_id = al.employee_id
                           AND COALESCE(o.signed_date, o.received_at::date) BETWEEN ab.absent_from AND ab.absent_to)))
      FROM sales.sales_order o
     WHERE o.sales_order_id = p_sales_order_id
$$;

CREATE OR REPLACE VIEW sales.v_sales_order_exception_0006 AS
WITH o AS (
    SELECT o.sales_order_id, o.order_number, o.customer_id, o.reporting_category_code,
           o.account_manager_employee_id,
           COALESCE(o.signed_date, o.received_at::date) AS order_date,
           sales.order_cover_for(o.sales_order_id) AS cover_for_employee_id
      FROM sales.sales_order o
     WHERE o.status_code <> 'cancelled'
), a AS (
    SELECT o.*, al.customer_account_allocation_id AS allocation_id, al.employee_id AS owner_employee_id,
           al.house_account, al.allocated_from,
           -- judged for: the absent colleague being covered, else the order's salesperson, else the owner
           COALESCE(o.cover_for_employee_id, o.account_manager_employee_id, al.employee_id) AS judged_employee_id
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
   AND n.cover_for_employee_id IS NULL
UNION ALL
SELECT n.sales_order_id, n.order_number, 'COVER_ORDER', 'warning',
       format('%s led this order covering for %s: the account stays with %s and NN/E is judged for them',
              sp.full_name, cv.full_name, cv.full_name)
  FROM n
  JOIN sales.employee sp ON sp.employee_id = n.account_manager_employee_id
  JOIN sales.employee cv ON cv.employee_id = n.cover_for_employee_id
UNION ALL
SELECT n.sales_order_id, n.order_number, 'OWNER_HAS_LEFT', 'warning',
       format('Account owner %s has left: run the leaver routine to move the account to House (sales-orders employee-leaves)', ow.full_name)
  FROM n
  JOIN sales.employee ow ON ow.employee_id = n.owner_employee_id
 WHERE NOT ow.is_active;


INSERT INTO sales.exception_rule (rule_code, description, blocks_approval, introduced_in) VALUES
    ('COVER_ORDER', 'Order led by a colleague covering for the account owner: ownership unchanged (warning)', false, '0011');

-- Salesperson and cover are commercial content: frozen on approval.
CREATE OR REPLACE FUNCTION sales.tg_sales_order_lock() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT lines_locked FROM sales.order_status WHERE status_code = OLD.status_code)
       AND (NEW.customer_id, NEW.currency_code, NEW.order_type_code, NEW.price_list_id,
            NEW.margin_exception_reason, NEW.sn_ref, NEW.reporting_category_code,
            NEW.account_manager_employee_id, NEW.covering_for_employee_id)
           IS DISTINCT FROM
           (OLD.customer_id, OLD.currency_code, OLD.order_type_code, OLD.price_list_id,
            OLD.margin_exception_reason, OLD.sn_ref, OLD.reporting_category_code,
            OLD.account_manager_employee_id, OLD.covering_for_employee_id)
    THEN
        RAISE EXCEPTION 'order %: commercial fields are locked in status %', OLD.order_number, OLD.status_code
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

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
    -- Temporary cover (CFO, 2026-09-25): the colleague stepping in does not take the account.
    IF sales.order_cover_for(p_sales_order_id) IS NOT NULL THEN
        RETURN 'cover order: ownership unchanged';
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

