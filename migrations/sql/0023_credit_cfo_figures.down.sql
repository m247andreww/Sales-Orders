DROP TRIGGER credit_report_cfo_entry ON sales.credit_report;
DROP FUNCTION sales.tg_credit_report_cfo_entry();

ALTER TABLE sales.credit_report DROP CONSTRAINT credit_report_cfo_entry_limit_check;
ALTER TABLE sales.credit_report DROP CONSTRAINT credit_report_entered_by_check;
ALTER TABLE sales.credit_report DROP CONSTRAINT credit_report_subject_link_check;
ALTER TABLE sales.credit_report ADD CONSTRAINT credit_report_check3
    CHECK ((source_code = 'workbook_import') = (credit_subject_id IS NOT NULL));
ALTER TABLE sales.credit_report DROP CONSTRAINT credit_report_source_code_check;
ALTER TABLE sales.credit_report ADD CONSTRAINT credit_report_source_code_check
    CHECK (source_code IN ('alert_email', 'workbook_import'));

ALTER TABLE sales.credit_report DROP COLUMN entered_by;
