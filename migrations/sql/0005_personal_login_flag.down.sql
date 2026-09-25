CREATE OR REPLACE FUNCTION audit.personal_logins_enforced() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sales_orders_person')
$$;
ALTER TABLE sales.policy_setting DROP CONSTRAINT policy_setting_require_personal_login_is_flag;
DELETE FROM sales.policy_setting WHERE setting_key = 'require_personal_login';
