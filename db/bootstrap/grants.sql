-- =============================================================================
-- Privileges. Run as sales_orders_owner AFTER migrations (and after any migration
-- that adds tables or views). Idempotent.
-- =============================================================================

\set ON_ERROR_STOP on

GRANT USAGE ON SCHEMA sales, audit TO sales_orders_app, sales_orders_readonly;

-- Application: no DELETE anywhere. Orders are cancelled by status, never removed.
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA sales TO sales_orders_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA sales TO sales_orders_app;
GRANT SELECT ON ALL TABLES IN SCHEMA audit TO sales_orders_app;
-- Lookup tables and the transition map are changed by migration only.
REVOKE INSERT, UPDATE ON sales.currency, sales.order_status, sales.order_status_transition,
    sales.order_type, sales.line_category, sales.billing_frequency, sales.payment_method,
    sales.risk_rating, sales.document_type, sales.check_type, sales.check_status,
    sales.supplier_account_status, sales.permission, sales.employee_permission,
    sales.service_category, sales.reporting_category, sales.order_document_type, sales.arr_treatment,
    sales.arr_contract_status, sales.arr_movement_type, sales.order_type_arr_movement,
    sales.policy_setting
    FROM sales_orders_app;
-- Policy values (margin, FX age, tolerance, require_personal_login) change by migration or owner only.
-- The one DELETE the application needs: replacing the read-only AW SOs Register mirror on each sync.
GRANT DELETE ON sales.register_entry TO sales_orders_app;
-- Privileges are granted by migration only: the application cannot grant itself rights.

GRANT SELECT ON ALL TABLES IN SCHEMA sales, audit TO sales_orders_readonly;

-- Production identity (Microsoft Entra ID). Each person logs in as themselves; membership of
-- sales_orders_person makes the login the audited actor and is REQUIRED for privileged actions
-- (approve, waive, approve losses, non-standard credit terms). Example, run by the Entra admin:
--   SELECT * FROM pgaadauth_create_principal('andrew.whitford@managed.co.uk', false, false);
--   GRANT sales_orders_person TO "andrew.whitford@managed.co.uk";
-- (sales_orders_person itself is created by db/bootstrap/roles.sql.)
GRANT USAGE ON SCHEMA sales, audit TO sales_orders_person;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA sales TO sales_orders_person;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA sales TO sales_orders_person;
GRANT SELECT ON ALL TABLES IN SCHEMA audit TO sales_orders_person;
REVOKE INSERT, UPDATE ON sales.permission, sales.employee_permission, sales.policy_setting
    FROM sales_orders_person;

-- PRODUCTION ONLY (run once, as sales_orders_owner, after Entra logins exist):
--   UPDATE sales.policy_setting SET numeric_value = 1 WHERE setting_key = 'require_personal_login';
