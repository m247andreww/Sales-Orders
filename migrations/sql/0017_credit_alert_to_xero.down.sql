-- Reverse 0017: alert filings and their PDFs are removed; superseded folder copies become pending again;
-- the 0016 queue function is restored.
DELETE FROM sales.credit_filing_task WHERE task_code = 'xero_alert';
DROP TABLE sales.credit_alert_snapshot;
DROP INDEX sales.credit_filing_xero_alert_uq;
UPDATE sales.credit_filing_task SET status_code = 'pending', last_error = NULL
 WHERE task_code = 'mail_copy' AND status_code = 'superseded';
ALTER TABLE sales.credit_filing_task DROP CONSTRAINT credit_filing_task_check1;
ALTER TABLE sales.credit_filing_task ADD CONSTRAINT credit_filing_task_check1
    CHECK ((task_code = 'mail_copy') = (credit_alert_email_id IS NOT NULL));
ALTER TABLE sales.credit_filing_task DROP CONSTRAINT credit_filing_task_task_code_check;
ALTER TABLE sales.credit_filing_task ADD CONSTRAINT credit_filing_task_task_code_check
    CHECK (task_code IN ('xero_snapshot', 'mail_copy'));

CREATE OR REPLACE FUNCTION sales.queue_credit_filing() RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_xero integer;
    v_mail integer;
BEGIN
    INSERT INTO sales.credit_filing_task (task_code, credit_subject_id, credit_assessment_id)
    SELECT 'xero_snapshot', a.credit_subject_id, a.credit_assessment_id
      FROM sales.credit_assessment a
      JOIN sales.credit_subject s USING (credit_subject_id)
      JOIN sales.credit_relationship rel USING (relationship_code)
     WHERE rel.is_client
       AND (a.outcome_code IN ('applied', 'unchanged', 'override_in_force')
            OR EXISTS (SELECT 1 FROM sales.customer_credit_limit l
                        WHERE l.credit_assessment_id = a.credit_assessment_id))
    ON CONFLICT (credit_assessment_id) WHERE task_code = 'xero_snapshot' DO NOTHING;
    GET DIAGNOSTICS v_xero = ROW_COUNT;

    UPDATE sales.credit_filing_task t
       SET status_code = 'superseded',
           last_error = 'not filed: a later assessment of this client is filed instead'
     WHERE t.task_code = 'xero_snapshot'
       AND t.status_code IN ('pending', 'failed')
       AND EXISTS (SELECT 1 FROM sales.credit_filing_task later
                    WHERE later.task_code = 'xero_snapshot'
                      AND later.credit_subject_id = t.credit_subject_id
                      AND later.credit_assessment_id > t.credit_assessment_id);

    INSERT INTO sales.credit_filing_task (task_code, credit_subject_id, credit_alert_email_id)
    SELECT DISTINCT 'mail_copy', r.credit_subject_id, r.credit_alert_email_id
      FROM sales.v_credit_report r
      JOIN sales.credit_subject s USING (credit_subject_id)
      JOIN sales.credit_relationship rel USING (relationship_code)
     WHERE r.credit_alert_email_id IS NOT NULL AND rel.is_client AND s.is_active
    ON CONFLICT (credit_alert_email_id, credit_subject_id) WHERE task_code = 'mail_copy' DO NOTHING;
    GET DIAGNOSTICS v_mail = ROW_COUNT;
    RETURN v_xero + v_mail;
END
$$;

