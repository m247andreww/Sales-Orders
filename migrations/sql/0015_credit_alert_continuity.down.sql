-- Reverses 0015: the credit exception view as in 0014, without the continuity rule.
CREATE OR REPLACE VIEW sales.v_credit_exception AS
WITH p AS (
    SELECT numeric_value::integer AS max_age FROM sales.policy_setting WHERE setting_key = 'credit_bureau_max_age_days'
), latest AS (
    SELECT DISTINCT ON (credit_subject_id) credit_subject_id, credit_assessment_id, outcome_code, review_reason
      FROM sales.credit_assessment ORDER BY credit_subject_id, credit_assessment_id DESC
), subj AS (
    SELECT s.credit_subject_id, s.display_name, s.relationship_code, s.customer_id, s.debt_credit_folder_id,
           rel.is_assessed, rel.is_client
      FROM sales.credit_subject s JOIN sales.credit_relationship rel USING (relationship_code)
     WHERE s.is_active
)
SELECT 'CREDIT_REVIEW_NEEDED' AS rule_code, 'error' AS severity, s.credit_subject_id, s.display_name,
       format('Assessment %s: %s', l.credit_assessment_id, l.review_reason) AS message
  FROM latest l JOIN subj s USING (credit_subject_id)
 WHERE l.outcome_code = 'cfo_review'
   AND NOT EXISTS (SELECT 1 FROM sales.customer_credit_limit cl WHERE cl.credit_assessment_id = l.credit_assessment_id)
UNION ALL
SELECT 'ALERT_NOT_READ', 'error', NULL, b.name,
       format('%s email %s received %s: %s', b.name, e.internet_message_id, e.received_at, e.parse_error)
  FROM sales.credit_alert_email e JOIN sales.credit_bureau b USING (bureau_code)
 WHERE e.parse_error IS NOT NULL
UNION ALL
SELECT 'FILING_FAILED', 'error', s.credit_subject_id, s.display_name,
       format('%s failed %s time(s): %s', t.task_code, t.attempts, t.last_error)
  FROM sales.credit_filing_task t JOIN sales.credit_subject s USING (credit_subject_id)
 WHERE t.status_code = 'failed'
UNION ALL
SELECT 'CUSTOMER_NO_ARR_PREFIX', 'warning', s.credit_subject_id, s.display_name,
       'Customer has no ARR prefix: assessed with no recurring commitments from the ARR file'
  FROM subj s JOIN sales.customer c USING (customer_id)
 WHERE c.arr_prefix IS NULL
UNION ALL
SELECT 'CUSTOMER_NO_XERO_CONTACT', 'error', s.credit_subject_id, s.display_name,
       'Customer has no Xero contact id: the snapshot cannot be filed in Xero'
  FROM subj s JOIN sales.customer c USING (customer_id)
 WHERE c.xero_contact_id IS NULL
UNION ALL
SELECT 'UNKNOWN_COMPANY', 'warning', NULL, u.company_name,
       format('%s reported on %s %s (%s reading(s), latest %s)', u.bureaus, COALESCE(u.company_number, u.bureau_ref),
              u.company_name, u.readings, u.latest::date)
  FROM (SELECT r.company_number, r.bureau_ref, max(r.company_name) AS company_name, count(*) AS readings,
               max(r.observed_at) AS latest, string_agg(DISTINCT r.bureau_code, '+') AS bureaus
          FROM sales.v_credit_report r WHERE r.credit_subject_id IS NULL
         GROUP BY r.company_number, r.bureau_ref) u
UNION ALL
SELECT 'ARR_NOT_MONITORED', 'warning', NULL, a.customer_name,
       format('ARR prefix %s (%s): £%s a year of commitments, no credit monitoring',
              COALESCE(a.arr_prefix, '(none)'), a.customer_name, to_char(a.annual_revenue, 'FM999,999,999,990.00'))
  FROM (SELECT l.arr_prefix, string_agg(DISTINCT l.customer_name, ' / ') AS customer_name,
               sum(l.annual_revenue) AS annual_revenue
          FROM sales.credit_arr_line l JOIN sales.credit_arr_status st USING (arr_status)
         WHERE l.credit_arr_snapshot_id = sales.latest_credit_arr_snapshot() AND st.counts_as_commitment
         GROUP BY l.arr_prefix) a
 WHERE NOT EXISTS (SELECT 1 FROM subj s JOIN sales.customer c USING (customer_id) WHERE c.arr_prefix = a.arr_prefix)
UNION ALL
SELECT 'NO_BUREAU_LIMIT', 'warning', s.credit_subject_id, s.display_name,
       'No credit limit from Experian or Creditsafe'
  FROM subj s
 WHERE s.is_assessed
   AND NOT EXISTS (SELECT 1 FROM sales.v_credit_bureau_position bp
                    WHERE bp.credit_subject_id = s.credit_subject_id AND bp.credit_limit IS NOT NULL)
UNION ALL
SELECT 'SINGLE_BUREAU', 'warning', s.credit_subject_id, s.display_name,
       format('Credit limit from %s only', min(bp.bureau_code))
  FROM subj s JOIN sales.v_credit_bureau_position bp USING (credit_subject_id)
 WHERE s.is_assessed AND bp.credit_limit IS NOT NULL
 GROUP BY s.credit_subject_id, s.display_name
HAVING count(*) = 1
UNION ALL
SELECT 'STALE_BUREAU_DATA', 'warning', s.credit_subject_id, s.display_name,
       format('Latest %s reading is %s days old', bp.bureau_code, current_date - bp.last_observed_at::date)
  FROM subj s JOIN sales.v_credit_bureau_position bp USING (credit_subject_id) CROSS JOIN p
 WHERE s.is_assessed AND current_date - bp.last_observed_at::date > p.max_age
UNION ALL
SELECT 'ADVERSE_RISK_BAND', 'warning', s.credit_subject_id, s.display_name,
       format('%s risk band: %s', bp.bureau_code, bp.risk_band)
  FROM subj s JOIN sales.v_credit_bureau_position bp USING (credit_subject_id)
 WHERE bp.band_requires_review
UNION ALL
SELECT 'NO_DEBT_CREDIT_FOLDER', 'warning', s.credit_subject_id, s.display_name,
       'No Debt & Credit mail folder mapped: alert emails cannot be filed'
  FROM subj s
 WHERE s.is_client AND s.debt_credit_folder_id IS NULL
UNION ALL
SELECT 'CREDIT_DECISION_REVIEW_DUE', 'warning', s.credit_subject_id, s.display_name,
       format('CFO limit £%s was to be reviewed by %s', to_char(cl.credit_limit, 'FM999,999,999,990'), cl.review_by)
  FROM subj s JOIN sales.v_customer_current_credit_limit cl USING (customer_id)
 WHERE cl.source_code = 'cfo_decision' AND cl.review_by < current_date;

DELETE FROM sales.credit_exception_rule WHERE rule_code = 'ALERT_CONTINUITY';
INSERT INTO sales.credit_exception_rule (rule_code, severity, description, action) VALUES
    ('NO_DEBT_CREDIT_FOLDER', 'warning', 'A monitored customer has no Debt & Credit mail folder mapped',
     'Run credit-map-folders, or create the folder');
