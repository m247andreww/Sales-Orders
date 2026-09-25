-- =============================================================================
-- 0005 Production identity mode becomes a per-database, audited setting.
--
-- 0003 switched "personal login required" on whenever the cluster-wide role
-- sales_orders_person existed. Roles are shared by every database on a server, so a test
-- database next to production would silently change behaviour. The switch is now
-- sales.policy_setting 'require_personal_login' (1 = on), which the application cannot change
-- (see db/bootstrap/grants.sql) and whose every change is audited.
-- Fail-safe: when on and the caller is not a named person, privileged actions are refused.
-- =============================================================================

INSERT INTO sales.policy_setting (setting_key, numeric_value, description)
VALUES ('require_personal_login', 0,
        '1 = approvals, waivers, loss approvals and non-standard terms need a personal (Entra) login. Set to 1 in production.');

ALTER TABLE sales.policy_setting
    ADD CONSTRAINT policy_setting_require_personal_login_is_flag
    CHECK (setting_key <> 'require_personal_login' OR numeric_value IN (0, 1));

CREATE OR REPLACE FUNCTION audit.personal_logins_enforced() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT COALESCE((SELECT numeric_value = 1 FROM sales.policy_setting
                      WHERE setting_key = 'require_personal_login'), false)
$$;
