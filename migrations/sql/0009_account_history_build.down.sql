DROP VIEW sales.v_account_history_conflict;
DROP FUNCTION sales.build_account_history(text);
DROP TABLE sales.account_history_review;
DROP VIEW sales.v_register_unmapped_owner;
DROP FUNCTION sales.register_rows_for_customer(bigint);
CREATE OR REPLACE FUNCTION sales.customer_pre_existing_at(p_customer_id bigint, p_at date) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT sales.customer_first_order_date(p_customer_id) < p_at
$$;
ALTER TABLE sales.customer DROP CONSTRAINT customer_existed_before_has_source,
    DROP COLUMN existed_before, DROP COLUMN existed_before_source;
DROP TABLE sales.register_owner_alias;
