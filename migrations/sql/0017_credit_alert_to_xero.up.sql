-- =============================================================================
-- 0017 Bureau alerts filed on the client's Xero contact (CFO, 2026-10-02: "yes attach alerts").
-- The Microsoft 365 connector is read-only, so alert emails cannot be copied into Outlook "Debt & Credit"
-- folders. Instead each alert is filed as a one-page PDF on the client's Xero contact, beside the
-- assessment snapshot: the bureau, the source email (sender, subject, received, Message-ID) and that
-- company's lines from the alert exactly as read. One task per (alert email, client); suppliers and
-- "for information" companies are never filed. The Outlook folder copy (mail_copy) is retired: pending
-- copies are superseded and no more are queued.
-- =============================================================================

ALTER TABLE sales.credit_filing_task DROP CONSTRAINT credit_filing_task_task_code_check;
ALTER TABLE sales.credit_filing_task ADD CONSTRAINT credit_filing_task_task_code_check
    CHECK (task_code IN ('xero_snapshot', 'mail_copy', 'xero_alert'));
ALTER TABLE sales.credit_filing_task DROP CONSTRAINT credit_filing_task_check1;
ALTER TABLE sales.credit_filing_task ADD CONSTRAINT credit_filing_task_check1
    CHECK ((task_code IN ('mail_copy', 'xero_alert')) = (credit_alert_email_id IS NOT NULL));
CREATE UNIQUE INDEX credit_filing_xero_alert_uq ON sales.credit_filing_task (credit_alert_email_id, credit_subject_id)
    WHERE task_code = 'xero_alert';

-- The PDF of one client's part of one alert, exactly as filed.
CREATE TABLE sales.credit_alert_snapshot (
    credit_alert_email_id bigint   NOT NULL REFERENCES sales.credit_alert_email,
    credit_subject_id     bigint   NOT NULL REFERENCES sales.credit_subject,
    file_name             text     NOT NULL CHECK (file_name ~ '\.pdf$'),
    pdf                   bytea    NOT NULL CHECK (octet_length(pdf) > 0),
    sha256                char(64) GENERATED ALWAYS AS (encode(sha256(pdf), 'hex')) STORED,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text        NOT NULL DEFAULT audit.current_actor(),
    PRIMARY KEY (credit_alert_email_id, credit_subject_id)
);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_alert_snapshot
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();

UPDATE sales.credit_filing_task
   SET status_code = 'superseded',
       last_error = 'not filed: Outlook folder copies are retired; the alert is filed on the Xero contact instead'
 WHERE task_code = 'mail_copy' AND status_code IN ('pending', 'failed');

CREATE OR REPLACE FUNCTION sales.queue_credit_filing() RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_xero integer;
    v_alert integer;
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
    SELECT DISTINCT 'xero_alert', r.credit_subject_id, r.credit_alert_email_id
      FROM sales.v_credit_report r
      JOIN sales.credit_subject s USING (credit_subject_id)
      JOIN sales.credit_relationship rel USING (relationship_code)
     WHERE r.credit_alert_email_id IS NOT NULL AND rel.is_client AND s.is_active
    ON CONFLICT (credit_alert_email_id, credit_subject_id) WHERE task_code = 'xero_alert' DO NOTHING;
    GET DIAGNOSTICS v_alert = ROW_COUNT;
    RETURN v_xero + v_alert;
END
$$;

SELECT sales.queue_credit_filing();
