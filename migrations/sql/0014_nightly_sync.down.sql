-- Reverses 0014.
DROP FUNCTION sales.move_accounts(text, text, date, text);
ALTER TABLE sales.customer DROP COLUMN xero_archived;
DROP VIEW sales.v_job_run_latest;
DROP TABLE sales.job_run_step;
DROP TABLE sales.job_run;
