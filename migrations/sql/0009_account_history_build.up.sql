-- =============================================================================
-- 0009 Build account-ownership history from the AW SOs Register (CFO, 2026-09-24).
--   * Auto Renew and Cust Success are House accounts (CFO). Legacy is treated as House too,
--     because leavers' accounts go to House.
--   * Rule: ownership changes ONLY when a different NAMED salesperson appears. House-labelled
--     orders (central renewals) do not end a salesperson's ownership. Validated on the real
--     Register: removes 102 of 121 apparent flip-flops; the rest are listed for CFO review.
--   * A customer whose first Register order is marked "Existing" pre-dates the Register:
--     recorded in customer.existed_before so NN/E treats it as pre-existing.
-- The build only fills customers that have NO allocation yet, so confirmed or corrected history is
-- never overwritten.
-- =============================================================================

-- How Register "Salesperson" labels map to owners. Person rows are master data (loaded, not seeded).
CREATE TABLE sales.register_owner_alias (
    register_label text PRIMARY KEY CHECK (register_label = lower(btrim(register_label))),
    employee_id    bigint REFERENCES sales.employee,
    house_account  text   REFERENCES sales.house_account_type,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text        NOT NULL DEFAULT audit.current_actor(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text        NOT NULL DEFAULT audit.current_actor(),
    row_version    integer     NOT NULL DEFAULT 1,
    CHECK ((employee_id IS NULL) <> (house_account IS NULL))
);
SELECT sales.attach_standard_triggers('sales.register_owner_alias');
INSERT INTO sales.register_owner_alias (register_label, house_account) VALUES
    ('house', 'House'), ('auto renew', 'House'), ('cust success', 'House'), ('legacy', 'House');

ALTER TABLE sales.customer
    ADD COLUMN existed_before date,           -- customer is known to have existed on this date
    ADD COLUMN existed_before_source text,
    ADD CONSTRAINT customer_existed_before_has_source CHECK ((existed_before IS NULL) = (existed_before_source IS NULL));

CREATE OR REPLACE FUNCTION sales.customer_pre_existing_at(p_customer_id bigint, p_at date) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(sales.customer_first_order_date(p_customer_id) < p_at, false)
        OR COALESCE((SELECT existed_before <= p_at FROM sales.customer WHERE customer_id = p_customer_id), false)
$$;

-- Register rows for a customer, oldest first, with the mapped owner.
CREATE FUNCTION sales.register_rows_for_customer(p_customer_id bigint)
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

-- Register salesperson labels with no owner mapping: the build refuses customers that use them.
CREATE VIEW sales.v_register_unmapped_owner AS
SELECT r.salesperson, count(*) AS orders, min(r.date_issued) AS first_seen, max(r.date_issued) AS last_seen
  FROM sales.register_entry r
  LEFT JOIN sales.register_owner_alias a ON a.register_label = lower(btrim(r.salesperson))
 WHERE r.salesperson IS NOT NULL AND a.register_label IS NULL AND r.sn_ref !~ '(LO|CA)$'
 GROUP BY r.salesperson;

-- Cases the build had to decide and a person should confirm.
CREATE TABLE sales.account_history_review (
    account_history_review_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES sales.customer,
    on_date     date   NOT NULL,
    note        text   NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text        NOT NULL DEFAULT audit.current_actor()
);
CREATE TRIGGER audit_row AFTER INSERT OR UPDATE OR DELETE ON sales.account_history_review
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();

CREATE FUNCTION sales.build_account_history(p_source text) RETURNS TABLE (customers_built integer,
                                                                          periods_created integer,
                                                                          customers_skipped integer)
LANGUAGE plpgsql AS $$
DECLARE
    c          record;
    r          record;
    v_cur_emp  bigint;
    v_cur_house text;
    v_prev_date date;
    v_prev_emp  bigint;
    v_started  boolean;
    v_from     date;
    v_existed  date;
    v_unmapped boolean;
    v_built    integer := 0;
    v_periods  integer := 0;
    v_skipped  integer := 0;
    v_periods_c integer;
    v_emps     bigint[];
    v_houses   text[];
    v_froms    date[];
    i          integer;
BEGIN
    FOR c IN SELECT cu.customer_id FROM sales.customer cu
              WHERE NOT EXISTS (SELECT 1 FROM sales.customer_account_allocation a WHERE a.customer_id = cu.customer_id)
              ORDER BY cu.customer_id
    LOOP
        SELECT bool_or(x.employee_id IS NULL AND x.house_account IS NULL AND x.salesperson IS NOT NULL)
          INTO v_unmapped FROM sales.register_rows_for_customer(c.customer_id) x;
        IF v_unmapped THEN
            v_skipped := v_skipped + 1;   -- fix the alias (see v_register_unmapped_owner), then re-run
            CONTINUE;
        END IF;

        v_started := false; v_emps := '{}'; v_houses := '{}'; v_froms := '{}'; v_existed := NULL;
        v_prev_date := NULL; v_prev_emp := NULL;
        FOR r IN SELECT * FROM sales.register_rows_for_customer(c.customer_id) x WHERE x.salesperson IS NOT NULL LOOP
            IF v_existed IS NULL AND NOT v_started AND lower(COALESCE(r.new_logo, '')) LIKE 'existing%' THEN
                v_existed := r.date_issued;
            END IF;
            IF NOT v_started THEN
                v_cur_emp := r.employee_id; v_cur_house := r.house_account; v_started := true;
                v_emps := v_emps || r.employee_id; v_houses := v_houses || r.house_account; v_froms := v_froms || r.date_issued;
            ELSIF r.employee_id IS NOT NULL AND r.employee_id IS DISTINCT FROM v_cur_emp THEN
                IF r.date_issued = v_prev_date AND v_prev_emp IS NOT NULL THEN
                    -- two named salespeople on the same day: the later order (by SN) wins; logged for review
                    INSERT INTO sales.account_history_review (customer_id, on_date, note)
                    VALUES (c.customer_id, r.date_issued,
                            format('Two salespeople on %s; %s (%s) taken as owner', r.date_issued, r.salesperson, r.sn_ref));
                END IF;
                IF r.date_issued = v_froms[array_length(v_froms, 1)] THEN
                    -- a period starting today already exists: replace its owner rather than create a zero-day period
                    v_emps[array_length(v_emps, 1)] := r.employee_id;
                    v_houses[array_length(v_houses, 1)] := NULL;
                ELSE
                    -- a different named salesperson takes over
                    v_emps := v_emps || r.employee_id; v_houses := v_houses || NULL::text; v_froms := v_froms || r.date_issued;
                END IF;
                v_cur_emp := r.employee_id; v_cur_house := NULL;
            END IF;
            -- House-labelled rows while a salesperson owns the account: central renewals, no change.
            IF r.employee_id IS NOT NULL THEN
                v_prev_date := r.date_issued; v_prev_emp := r.employee_id;
            END IF;
        END LOOP;
        IF NOT v_started THEN
            CONTINUE;  -- customer not on the Register
        END IF;

        v_periods_c := array_length(v_froms, 1);
        FOR i IN 1 .. v_periods_c LOOP
            INSERT INTO sales.customer_account_allocation
                (customer_id, employee_id, house_account, allocated_from, allocated_to, source)
            VALUES (c.customer_id, v_emps[i], v_houses[i], v_froms[i],
                    CASE WHEN i < v_periods_c THEN v_froms[i + 1] - 1 END, p_source);
        END LOOP;
        IF v_existed IS NOT NULL THEN
            UPDATE sales.customer SET existed_before = v_existed,
                   existed_before_source = 'AW SOs Register: first order marked Existing'
             WHERE customer_id = c.customer_id AND existed_before IS NULL;
        END IF;
        v_built := v_built + 1; v_periods := v_periods + v_periods_c;
    END LOOP;
    RETURN QUERY SELECT v_built, v_periods, v_skipped;
END
$$;

-- For CFO review: accounts where named salespeople alternate (A -> B -> A).
CREATE VIEW sales.v_account_history_conflict AS
WITH p AS (
    SELECT a.customer_id, a.employee_id, a.allocated_from,
           lag(a.employee_id, 2) OVER (PARTITION BY a.customer_id ORDER BY a.allocated_from) AS two_back
      FROM sales.customer_account_allocation a
)
SELECT c.legal_name AS customer, e.full_name AS returns_to, p.allocated_from
  FROM p JOIN sales.customer c USING (customer_id) JOIN sales.employee e ON e.employee_id = p.employee_id
 WHERE p.employee_id IS NOT NULL AND p.employee_id = p.two_back;
