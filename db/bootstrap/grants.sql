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
    sales.supplier_account_status
    FROM sales_orders_app;

GRANT SELECT ON ALL TABLES IN SCHEMA sales, audit TO sales_orders_readonly;
