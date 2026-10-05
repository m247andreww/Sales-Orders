-- Restore the 0014 assessment function, then drop the column it no longer writes.
CREATE OR REPLACE FUNCTION sales.create_credit_assessment(p_subject_id bigint, p_trigger text) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    i         record;
    v_id      bigint;
    v_a       record;
    v_current record;
    v_reasons text[] := '{}';
    v_outcome text;
BEGIN
    SELECT * INTO i FROM sales.v_credit_subject_inputs WHERE credit_subject_id = p_subject_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown or inactive credit subject %', p_subject_id USING ERRCODE = 'no_data_found';
    END IF;
    IF NOT i.is_assessed THEN
        RAISE EXCEPTION '% is monitored as %: not credit assessed', i.display_name, i.relationship_code
            USING ERRCODE = 'check_violation';
    END IF;
    IF i.customer_id IS NOT NULL AND i.credit_arr_snapshot_id IS NULL THEN
        RAISE EXCEPTION 'no ARR file has been loaded: load it before assessing customers'
            USING ERRCODE = 'check_violation';
    END IF;

    -- Work out the figures from the same inputs the row will freeze.
    WITH e AS (SELECT * FROM sales.credit_arr_exposure(
                   CASE WHEN i.customer_id IS NOT NULL THEN i.credit_arr_snapshot_id END, i.arr_prefix)),
         pol AS (SELECT
                   (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'credit_vat_rate') AS vat_rate,
                   (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'credit_round_to') AS round_to,
                   (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'credit_appetite_pct') AS pct,
                   (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'credit_appetite_threshold') AS thr),
         n AS (SELECT COALESCE((SELECT sum(exposure) FROM e), 0) + i.one_off_allowance AS net FROM pol)
    SELECT ceil((n.net + round(n.net * pol.vat_rate, 2)) / pol.round_to) * pol.round_to AS requirement,
           LEAST(i.experian_limit, i.creditsafe_limit) AS baseline, pol.vat_rate, pol.round_to, pol.pct, pol.thr
      INTO v_a
      FROM n CROSS JOIN pol;

    IF v_a.baseline IS NULL OR v_a.baseline = 0 THEN
        IF v_a.requirement > 0 THEN
            v_reasons := v_reasons || 'no bureau credit limit to support the trading requirement'::text;
        END IF;
    ELSE
        IF v_a.baseline * v_a.pct <= v_a.thr AND v_a.requirement > v_a.baseline * v_a.pct THEN
            v_reasons := v_reasons || format('trading requirement £%s exceeds risk appetite £%s (%s%% of the lower bureau limit)',
                                             to_char(v_a.requirement, 'FM999,999,999,990'),
                                             to_char(v_a.baseline * v_a.pct, 'FM999,999,999,990'),
                                             to_char(v_a.pct * 100, 'FM990'));
        END IF;
        IF v_a.requirement > v_a.baseline THEN
            v_reasons := v_reasons || format('trading requirement £%s exceeds the lower bureau limit £%s',
                                             to_char(v_a.requirement, 'FM999,999,999,990'),
                                             to_char(v_a.baseline, 'FM999,999,999,990'));
        END IF;
    END IF;
    IF i.band_requires_review THEN
        v_reasons := v_reasons || format('Experian risk band is %s', i.experian_band);
    END IF;

    SELECT credit_limit, source_code, review_by INTO v_current
      FROM sales.v_customer_current_credit_limit WHERE customer_id = i.customer_id;

    v_outcome := CASE
        WHEN i.customer_id IS NULL THEN 'not_a_customer'
        WHEN cardinality(v_reasons) > 0 THEN 'cfo_review'
        WHEN v_current.source_code = 'cfo_decision'
             AND (v_current.review_by IS NULL OR v_current.review_by >= current_date) THEN 'override_in_force'
        WHEN v_current.credit_limit = v_a.requirement THEN 'unchanged'
        ELSE 'applied'
    END;

    INSERT INTO sales.credit_assessment
           (credit_subject_id, trigger_code, experian_report_id, experian_limit, experian_observed_at,
            experian_band, experian_score, creditsafe_report_id, creditsafe_limit, creditsafe_observed_at,
            credit_arr_snapshot_id, arr_prefix, one_off_allowance, vat_rate, round_to, appetite_pct,
            appetite_threshold, outcome_code, review_reason)
    VALUES (i.credit_subject_id, p_trigger, i.experian_report_id, i.experian_limit, i.experian_observed_at,
            i.experian_band, i.experian_score, i.creditsafe_report_id, i.creditsafe_limit, i.creditsafe_observed_at,
            CASE WHEN i.customer_id IS NOT NULL THEN i.credit_arr_snapshot_id END, i.arr_prefix,
            i.one_off_allowance, v_a.vat_rate, v_a.round_to, v_a.pct, v_a.thr, v_outcome,
            CASE WHEN v_outcome = 'cfo_review' THEN array_to_string(v_reasons, '; ') END)
    RETURNING credit_assessment_id INTO v_id;

    INSERT INTO sales.credit_assessment_line (credit_assessment_id, arr_frequency, annual_revenue, exposure, line_count)
    SELECT v_id, e.arr_frequency, e.annual_revenue, e.exposure, e.line_count
      FROM sales.credit_arr_exposure(CASE WHEN i.customer_id IS NOT NULL THEN i.credit_arr_snapshot_id END,
                                     i.arr_prefix) e;

    IF v_outcome = 'applied' THEN
        PERFORM sales.set_credit_limit(i.customer_id, v_a.requirement, 'assessment', v_id, NULL, NULL);
    END IF;
    RETURN v_id;
END
$$;

ALTER TABLE sales.credit_assessment DROP CONSTRAINT credit_assessment_held_check;
ALTER TABLE sales.credit_assessment DROP COLUMN held_by_credit_limit_id;
