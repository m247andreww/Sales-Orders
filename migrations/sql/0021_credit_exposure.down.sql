-- Reverse 0021.
DROP VIEW sales.v_credit_exposure;
DROP TABLE sales.credit_pipeline_document;
DROP TABLE sales.credit_pipeline_snapshot;
DROP TABLE sales.credit_receivable_line;
DROP TABLE sales.credit_receivable_snapshot;
DELETE FROM sales.policy_setting WHERE setting_key = 'credit_pipeline_add_vat';
