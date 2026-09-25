-- =============================================================================
-- 0013 Leaving date for account ownership (CFO, 2026-09-25): a salesperson leaves on their LAST
-- WORKING DAY. Gardening leave counts as having left; the contractual leave date does not matter.
-- Orders credited to a leaver after that day keep their sales credit (the Register is not changed)
-- but never give the leaver an account: the account stays with its previous owner, or goes to House.
--
-- Also fixes employee_leaves(): when a leaver's period was followed by later periods, the House
-- period it inserted was open-ended and overlapped them.
-- =============================================================================

ALTER TABLE sales.employee
    ADD COLUMN left_on date,
    ADD CONSTRAINT employee_left_is_inactive CHECK (left_on IS NULL OR NOT is_active);
COMMENT ON COLUMN sales.employee.left_on IS
    'Last working day (gardening leave counts as left). Accounts cannot pass to them after it.';

-- Apply a leaver's last working day to every account they hold, idempotently. Returns periods changed.
CREATE FUNCTION sales.apply_leaver(p_employee_id bigint) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_last    date;
    v_name    text;
    v_email   text;
    v_changed integer := 0;
    p         record;
    q         record;
BEGIN
    SELECT left_on, full_name, email INTO v_last, v_name, v_email FROM sales.employee WHERE employee_id = p_employee_id;
    IF v_last IS NULL THEN
        RAISE EXCEPTION 'employee % has no leaving date', p_employee_id USING ERRCODE = 'no_data_found';
    END IF;

    -- 1. Periods that started after the last day (orders credited to the leaver afterwards): the
    --    account stays with the previous owner, or goes to House if that was the leaver or nobody.
    FOR p IN SELECT customer_account_allocation_id AS id, customer_id, allocated_from
               FROM sales.customer_account_allocation
              WHERE employee_id = p_employee_id AND allocated_from > v_last
              ORDER BY customer_id, allocated_from
    LOOP
        SELECT employee_id, house_account INTO q FROM sales.customer_account_allocation
         WHERE customer_id = p.customer_id AND allocated_to = p.allocated_from - 1;
        IF q.employee_id IS NULL OR q.employee_id = p_employee_id THEN
            q.employee_id := NULL;
            q.house_account := 'House';
        END IF;
        UPDATE sales.customer_account_allocation
           SET employee_id = q.employee_id, house_account = q.house_account,
               source = format('%s; %s had left (last day %s)', source, v_name, v_last)
         WHERE customer_account_allocation_id = p.id;
        INSERT INTO sales.account_history_review (customer_id, on_date, note)
        VALUES (p.customer_id, p.allocated_from,
                format('Order credited to %s after their last day (%s): ownership not passed to them', v_name, v_last));
        v_changed := v_changed + 1;
    END LOOP;

    -- 2. The period running past the last day: close it, House from the next day until it would have ended.
    FOR p IN SELECT customer_account_allocation_id AS id, customer_id, allocated_to
               FROM sales.customer_account_allocation
              WHERE employee_id = p_employee_id AND allocated_from <= v_last
                AND (allocated_to IS NULL OR allocated_to > v_last)
    LOOP
        UPDATE sales.customer_account_allocation SET allocated_to = v_last
         WHERE customer_account_allocation_id = p.id;
        INSERT INTO sales.customer_account_allocation (customer_id, house_account, allocated_from, allocated_to, source)
        VALUES (p.customer_id, 'House', v_last + 1, p.allocated_to,
                format('Leaver: %s (last day %s)', v_email, v_last));
        v_changed := v_changed + 1;
    END LOOP;
    RETURN v_changed;
END
$$;

CREATE OR REPLACE FUNCTION sales.employee_leaves(p_email text, p_last_day date) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_employee bigint;
    v_left     date;
BEGIN
    SELECT employee_id, left_on INTO v_employee, v_left FROM sales.employee WHERE lower(email) = lower(p_email);
    IF v_employee IS NULL THEN
        RAISE EXCEPTION 'unknown employee %', p_email USING ERRCODE = 'no_data_found';
    END IF;
    IF v_left IS NOT NULL AND v_left <> p_last_day THEN
        RAISE EXCEPTION '% is already recorded as leaving on %', p_email, v_left USING ERRCODE = 'check_violation';
    END IF;
    UPDATE sales.employee SET is_active = false, left_on = p_last_day
     WHERE employee_id = v_employee AND (is_active OR left_on IS NULL);
    RETURN sales.apply_leaver(v_employee);
END
$$;

-- Register rows credited to someone after their last day count as House labels for ownership
-- (no change of owner); the credit itself is untouched.
CREATE OR REPLACE FUNCTION sales.register_rows_for_customer(p_customer_id bigint)
RETURNS TABLE (sn_ref text, date_issued date, new_logo text, salesperson text,
               employee_id bigint, house_account text)
LANGUAGE sql STABLE AS $$
    SELECT r.sn_ref, r.date_issued, r.new_logo, r.salesperson,
           CASE WHEN e.left_on < r.date_issued THEN NULL ELSE a.employee_id END,
           CASE WHEN e.left_on < r.date_issued THEN 'House' ELSE a.house_account END
      FROM sales.register_entry r
      JOIN sales.customer c ON c.customer_id = p_customer_id
      LEFT JOIN sales.register_owner_alias a ON a.register_label = lower(btrim(r.salesperson))
      LEFT JOIN sales.employee e ON e.employee_id = a.employee_id
     WHERE r.sn_ref !~ '(LO|CA)$' AND r.date_issued IS NOT NULL
       AND sales.normalised_name(r.client) IN (sales.normalised_name(c.legal_name),
                                               sales.normalised_name(c.trading_name),
                                               sales.normalised_name(c.xero_tracking_customer))
     ORDER BY r.date_issued, r.sn_ref
$$;

-- For CFO review: Register orders credited to a salesperson after their last working day.
CREATE VIEW sales.v_register_credit_after_leaving AS
SELECT r.sn_ref, r.date_issued, r.client, r.salesperson, e.left_on
  FROM sales.register_entry r
  JOIN sales.register_owner_alias a ON a.register_label = lower(btrim(r.salesperson))
  JOIN sales.employee e ON e.employee_id = a.employee_id
 WHERE e.left_on < r.date_issued AND r.sn_ref !~ '(LO|CA)$';

-- The history build now also applies every recorded leaving date to the periods it creates.
ALTER FUNCTION sales.build_account_history(text) RENAME TO build_account_history_0009;
CREATE FUNCTION sales.build_account_history(p_source text) RETURNS TABLE (customers_built integer,
                                                                          periods_created integer,
                                                                          customers_skipped integer)
LANGUAGE plpgsql AS $$
DECLARE
    v_leaver bigint;
BEGIN
    RETURN QUERY SELECT b.customers_built, b.periods_created, b.customers_skipped
                   FROM sales.build_account_history_0009(p_source) b;
    FOR v_leaver IN SELECT employee_id FROM sales.employee WHERE left_on IS NOT NULL LOOP
        PERFORM sales.apply_leaver(v_leaver);
    END LOOP;
END
$$;
