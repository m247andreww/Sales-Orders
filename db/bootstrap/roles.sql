-- =============================================================================
-- One-off, per-environment role setup. Run by a DBA as a superuser BEFORE migrations.
-- Roles are cluster-wide, so they are not created by migrations.
--
--   sales_orders_owner    owns the schemas; used ONLY to run migrations
--   sales_orders_app      the application: read/insert/update, never DELETE
--   sales_orders_readonly reporting / Power BI / auditors: SELECT only
--   sales_orders_person   group for named people signing in with Entra ID (approvals etc.)
--
-- Passwords are set out-of-band (e.g. \password in psql or a secrets manager), never here.
-- Usage: psql -v db=sales_orders -f db/bootstrap/roles.sql
-- =============================================================================

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sales_orders_owner') THEN
        CREATE ROLE sales_orders_owner LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sales_orders_app') THEN
        CREATE ROLE sales_orders_app LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sales_orders_readonly') THEN
        CREATE ROLE sales_orders_readonly LOGIN;
    END IF;
    -- Group for named people (Entra ID logins). Its existence switches on production identity mode:
    -- privileged actions must then come from a personal login (migration 0003).
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sales_orders_person') THEN
        CREATE ROLE sales_orders_person NOLOGIN;
    END IF;
END
$$;

SELECT format('CREATE DATABASE %I OWNER sales_orders_owner', :'db')
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec

\connect :db

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO sales_orders_owner;   -- alembic_version lives in public
CREATE EXTENSION IF NOT EXISTS btree_gist;
