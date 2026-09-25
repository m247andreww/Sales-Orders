-- Reverses 0012: restores the 0006 union and drops the Xero owner-group objects.
DELETE FROM sales.exception_rule WHERE rule_code = 'XERO_OWNER_MISMATCH';

CREATE OR REPLACE VIEW sales.v_sales_order_exception_all AS
SELECT sales_order_id, order_number, rule_code, severity, message FROM sales.v_sales_order_exception
UNION ALL
SELECT sales_order_id, order_number, rule_code, severity, message FROM sales.v_sales_order_exception_0004
UNION ALL
SELECT sales_order_id, order_number, rule_code, severity, message FROM sales.v_sales_order_exception_0006;

DROP VIEW sales.v_sales_order_exception_0012;
DROP FUNCTION sales.apply_xero_owners(boolean);
DROP FUNCTION sales.set_account_owner(bigint, bigint, text, date, text);
DROP VIEW sales.v_xero_owner_unmatched_contact;
DROP VIEW sales.v_account_owner_reconciliation;
DROP FUNCTION sales.refresh_customer_xero_owner(bigint);
DROP TABLE sales.customer_xero_owner;
DROP TABLE sales.xero_group_membership;
DROP TABLE sales.xero_group_sync;
DROP TABLE sales.xero_owner_group;
