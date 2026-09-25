-- Reverses 0013: restores the 0008 leaver routine, the 0009 Register rows and history build.
DROP FUNCTION sales.build_account_history(text);
ALTER FUNCTION sales.build_account_history_0009(text) RENAME TO build_account_history;
DROP VIEW sales.v_register_credit_after_leaving;
CREATE OR REPLACE FUNCTION sales.register_rows_for_customer(p_customer_id bigint)
RETURNS TABLE (sn_ref text, date_issued date, new_logo text, salesperson text,
               employee_id bigint, house_account text)
LANGUAGE sql STABLE AS $$
    SELECT r.sn_ref, r.date_issued, r.new_logo, r.salesperson, a.employee_id, a.house_account
      FROM sales.register_entry r
      JOIN sales.customer c ON c.customer_id = p_customer_id
      LEFT JOIN sales.register_owner_alias a ON a.register_label = lower(btrim(r.salesperson))
     WHERE r.sn_ref !~ '(LO|CA)$' AND r.date_issued IS NOT NULL
       AND sales.normalised_name(r.client) IN (sales.normalised_name(c.legal_name),
                                               sales.normalised_name(c.trading_name),
                                               sales.normalised_name(c.xero_tracking_customer))
     ORDER BY r.date_issued, r.sn_ref
$$;

CREATE OR REPLACE FUNCTION sales.employee_leaves(p_email text, p_last_day date) RETURNS integer
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

DROP FUNCTION sales.apply_leaver(bigint);
ALTER TABLE sales.employee DROP CONSTRAINT employee_left_is_inactive, DROP COLUMN left_on;
