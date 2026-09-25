-- =============================================================================
-- 0014 Nightly sync (CFO, 2026-09-25): a scheduled job in Azure loads the Reference sheet (staff,
-- account moves), Xero (chart of accounts, customers, owner groups) and the AW SOs Register.
--   * sales.job_run / sales.job_run_step: every run and every step, with its outcome (monitoring).
--   * sales.move_accounts(): the "Account moves" tab, applied idempotently.
--   * customer.xero_status: Xero archived contacts are followed, never deleted.
-- =============================================================================

CREATE TABLE sales.job_run (
    job_run_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name    text        NOT NULL CHECK (job_name ~ '^[a-z][a-z0-9-]+$'),
    started_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    status      text        NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    summary     text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text        NOT NULL DEFAULT audit.current_actor(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text        NOT NULL DEFAULT audit.current_actor(),
    row_version integer     NOT NULL DEFAULT 1,
    CHECK ((status = 'running') = (finished_at IS NULL))
);
COMMENT ON TABLE sales.job_run IS 'Each run of a scheduled job (e.g. nightly-sync) and how it ended.';
SELECT sales.attach_standard_triggers('sales.job_run');

CREATE TABLE sales.job_run_step (
    job_run_id  bigint  NOT NULL REFERENCES sales.job_run,
    step_no     integer NOT NULL CHECK (step_no > 0),
    step_name   text    NOT NULL,
    status      text    NOT NULL CHECK (status IN ('succeeded', 'failed', 'skipped')),
    detail      text    NOT NULL,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text        NOT NULL DEFAULT audit.current_actor(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text        NOT NULL DEFAULT audit.current_actor(),
    row_version integer     NOT NULL DEFAULT 1,
    PRIMARY KEY (job_run_id, step_no),
    CHECK (finished_at >= started_at)
);
SELECT sales.attach_standard_triggers('sales.job_run_step');

CREATE VIEW sales.v_job_run_latest AS
SELECT r.job_run_id, r.job_name, r.started_at, r.finished_at, r.status, r.summary,
       s.step_no, s.step_name, s.status AS step_status, s.detail
  FROM sales.job_run r
  LEFT JOIN sales.job_run_step s USING (job_run_id)
 WHERE r.job_run_id = (SELECT max(x.job_run_id) FROM sales.job_run x WHERE x.job_name = r.job_name);

ALTER TABLE sales.customer
    ADD COLUMN xero_archived boolean NOT NULL DEFAULT false;
COMMENT ON COLUMN sales.customer.xero_archived IS 'The Xero contact is archived (followed nightly; the customer is never deleted).';

-- Account moves (Reference sheet): every account whose owner the day before p_effective was the
-- given person, or House because that person left, passes to p_to (an employee email or 'House').
-- Idempotent: an account already with the new owner on that date is left alone. A later ownership
-- change is never overwritten (sales.set_account_owner logs it for review).
CREATE FUNCTION sales.move_accounts(p_from_email text, p_to text, p_effective date, p_reason text)
RETURNS TABLE (customer text, result text)
LANGUAGE plpgsql AS $$
DECLARE
    v_from   bigint;
    v_from_email text;
    v_from_name  text;
    v_to     bigint;
    v_house  text;
    r        record;
BEGIN
    SELECT employee_id, email, full_name INTO v_from, v_from_email, v_from_name
      FROM sales.employee WHERE lower(email) = lower(p_from_email);
    IF v_from IS NULL THEN
        RAISE EXCEPTION 'unknown employee %', p_from_email USING ERRCODE = 'no_data_found';
    END IF;
    IF lower(p_to) = 'house' THEN
        v_house := 'House';
    ELSE
        SELECT employee_id INTO v_to FROM sales.employee WHERE lower(email) = lower(p_to) AND is_active;
        IF v_to IS NULL THEN
            RAISE EXCEPTION 'unknown or inactive employee %', p_to USING ERRCODE = 'no_data_found';
        END IF;
    END IF;
    FOR r IN
        SELECT c.customer_id, c.legal_name, cur.employee_id AS cur_emp, cur.house_account AS cur_house
          FROM sales.customer c
          JOIN LATERAL sales.account_allocation_on(c.customer_id, p_effective - 1) prev ON true
          LEFT JOIN LATERAL sales.account_allocation_on(c.customer_id, p_effective) cur ON true
         WHERE prev.customer_account_allocation_id IS NOT NULL
           AND (prev.employee_id = v_from
                OR (prev.house_account = 'House'
                    -- House because this person left: the two forms sales.apply_leaver writes (0013)
                    AND (starts_with(lower(prev.source), lower(format('Leaver: %s (', v_from_email)))
                         OR position(lower(format('; %s had left (last day', v_from_name)) IN lower(prev.source)) > 0)))
         ORDER BY c.legal_name
    LOOP
        customer := r.legal_name;
        IF (r.cur_emp, r.cur_house) IS NOT DISTINCT FROM (v_to, v_house) THEN
            result := 'already moved';
        ELSE
            result := sales.set_account_owner(r.customer_id, v_to, v_house, p_effective,
                                              format('Account move: %s', p_reason));
        END IF;
        RETURN NEXT;
    END LOOP;
END
$$;
