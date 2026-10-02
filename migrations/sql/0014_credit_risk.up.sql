-- =============================================================================
-- 0014 Credit & risk management (CFO, 2026-10-02): supersedes the Credit Limit Assessment
-- Workings spreadsheet.
--
--   * Bureau readings (Experian Business Express, Creditsafe) arrive as monitoring-alert emails
--     and are kept as append-only evidence (sales.credit_report), keyed by company number.
--   * Monitored companies (sales.credit_subject) are customers, prospects, suppliers or "for
--     information"; only customers get a credit limit, a Xero snapshot and a filed email.
--   * Recurring commitments come from the ARR file, loaded as an immutable snapshot.
--   * An assessment (sales.credit_assessment) freezes its inputs; the arithmetic is a view, so the
--     figures can never disagree with the inputs. The workbook's rules are data:
--       exposure per ARR line = annual value / exposure_divisor * invoices_exposed
--       requirement           = ROUNDUP((recurring exposure + one-off allowance) * (1 + VAT), -2)
--       baseline              = lower of the two bureau limits
--   * The recommended limit is ALWAYS the trading requirement. It is applied automatically only
--     when it is inside risk appetite; otherwise the assessment waits for a CFO decision.
--   * Credit limits live in sales.customer_credit_limit (one source). customer_credit_terms keeps
--     payment terms and risk rating only.
-- =============================================================================

-- ---------------------------------------------------------------- lookups
CREATE TABLE sales.credit_bureau (
    bureau_code   text PRIMARY KEY CHECK (bureau_code ~ '^[a-z]+$'),
    name          text NOT NULL,
    alert_sender  text NOT NULL CHECK (alert_sender ~ '^[^@\s]+@[^@\s]+$'),
    sort_order    smallint NOT NULL UNIQUE
);
INSERT INTO sales.credit_bureau (bureau_code, name, alert_sender, sort_order) VALUES
    ('experian',   'Experian Business Express', 'ebe.noreply@experian.com',  1),
    ('creditsafe', 'Creditsafe',                'monitoring@creditsafe.com', 2);

CREATE TABLE sales.credit_relationship (
    relationship_code text PRIMARY KEY,
    description       text    NOT NULL,
    is_assessed       boolean NOT NULL,   -- gets a credit assessment
    is_client         boolean NOT NULL    -- has a credit limit, a Xero snapshot and filed emails
);
INSERT INTO sales.credit_relationship (relationship_code, description, is_assessed, is_client) VALUES
    ('customer',    'Client: credit limit, Xero snapshot, emails filed to Debt & Credit', true,  true),
    ('prospect',    'Prospective client: assessed, nothing filed until they are a customer', true,  false),
    ('supplier',    'Vendor: monitored for supply risk only',                               false, false),
    ('information', 'Monitored for information only',                                       false, false);

-- How much of a year's ARR is at risk at once, per ARR file invoicing frequency (workbook rules).
-- Workbook: annual /1, quarterly /4, monthly /12*2 (two invoices outstanding), tri-/quint-annual /1.
-- Frequencies the workbook did not list follow the same principle: one invoice period, at most a year.
CREATE TABLE sales.credit_exposure_rule (
    arr_frequency    text PRIMARY KEY CHECK (arr_frequency = lower(btrim(arr_frequency))),
    periods_per_year numeric(10,6) NOT NULL CHECK (periods_per_year > 0),
    exposure_divisor numeric(10,6) NOT NULL CHECK (exposure_divisor > 0),
    invoices_exposed numeric(10,6) NOT NULL CHECK (invoices_exposed > 0),
    description      text NOT NULL
);
INSERT INTO sales.credit_exposure_rule (arr_frequency, periods_per_year, exposure_divisor, invoices_exposed, description) VALUES
    ('monthly',         12,       12, 2, 'Two monthly invoices outstanding (workbook /12*2)'),
    ('quarterly',        4,        4, 1, 'One quarterly invoice (workbook /4)'),
    ('6 months',         2,        2, 1, 'One half-yearly invoice'),
    ('annual',           1,        1, 1, 'One annual invoice (workbook /1)'),
    ('annual and half',  0.666667, 1, 1, 'Multi-year invoice: one year of value (workbook basis for multi-year)'),
    ('20 months',        0.6,      1, 1, 'Multi-year invoice: one year of value (workbook basis for multi-year)'),
    ('bi-annual',        0.5,      1, 1, 'Multi-year invoice: one year of value (workbook basis for multi-year)'),
    ('tri-annual',       0.333333, 1, 1, 'Multi-year invoice: one year of value (workbook /1)'),
    ('52 months',        0.230769, 1, 1, 'Multi-year invoice: one year of value (workbook basis for multi-year)'),
    ('quint-annual',     0.2,      1, 1, 'Multi-year invoice: one year of value (workbook /1)'),
    ('dec-annual',       0.1,      1, 1, 'Multi-year invoice: one year of value (workbook basis for multi-year)');
COMMENT ON TABLE sales.credit_exposure_rule IS
    'Exposure per ARR invoicing frequency. A frequency not listed here fails the ARR load.';

-- Which ARR file statuses are a current commitment. The workbook summed every status, so
-- cancelled lines and both halves of a renewal were counted; this table fixes that.
CREATE TABLE sales.credit_arr_status (
    arr_status           text PRIMARY KEY CHECK (btrim(arr_status) <> ''),
    counts_as_commitment boolean NOT NULL,
    description          text    NOT NULL
);
INSERT INTO sales.credit_arr_status (arr_status, counts_as_commitment, description) VALUES
    ('Live',          true,  'Billing now'),
    ('Order',         true,  'Signed, not yet billing'),
    ('Renewal - New', true,  'The renewed contract'),
    ('Renewal - Old', false, 'Being replaced by its renewal: counting both would double count'),
    ('Cancelled',     false, 'No longer a commitment');

-- Bureau risk bands that always need the CFO to look before a limit is applied.
CREATE TABLE sales.credit_risk_band (
    bureau_code     text    NOT NULL REFERENCES sales.credit_bureau,
    band            text    NOT NULL CHECK (btrim(band) <> ''),
    requires_review boolean NOT NULL,
    PRIMARY KEY (bureau_code, band)
);
INSERT INTO sales.credit_risk_band (bureau_code, band, requires_review) VALUES
    ('experian', 'Very Low Risk',               false),
    ('experian', 'Low Risk',                    false),
    ('experian', 'Below Average Risk',          false),
    ('experian', 'Above Average Risk',          false),
    ('experian', 'High Risk',                   true),
    ('experian', 'Maximum Risk',                true),
    ('experian', 'Serious Adverse Information', true);

INSERT INTO sales.policy_setting (setting_key, numeric_value, description) VALUES
    ('credit_vat_rate',               0.20,   'VAT added to the trading requirement (workbook: 20%)'),
    ('credit_round_to',               100,    'Requirement rounded UP to this amount (workbook: ROUNDUP(x,-2))'),
    ('credit_appetite_pct',           0.50,   'Risk appetite: share of the lower bureau limit (workbook: 50%)'),
    ('credit_appetite_threshold',     100000, 'Appetite applies when 50% of the lower bureau limit is at most this (workbook: £100k)'),
    ('credit_default_one_off',        5000,   'One-off & project allowance for a newly monitored customer'),
    ('credit_bureau_max_age_days',    365,    'A bureau reading older than this is stale'),
    ('credit_reassess_on_arr_change', 1,      '1 = reassess when the ARR requirement changes; 0 = only on a bureau change');

-- ---------------------------------------------------------------- monitored companies
CREATE TABLE sales.credit_subject (
    credit_subject_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    display_name         text    NOT NULL CHECK (btrim(display_name) <> ''),
    company_number       text    CHECK (company_number ~ '^[A-Z0-9]{8}$'),    -- Companies House
    creditsafe_ref       text    CHECK (creditsafe_ref ~ '^[A-Z]{2}[A-Z0-9]+$'), -- Creditsafe "Safe No."
    relationship_code    text    NOT NULL REFERENCES sales.credit_relationship,
    customer_id          bigint  REFERENCES sales.customer,
    supplier_id          bigint  REFERENCES sales.supplier,
    one_off_allowance    numeric(18,2) NOT NULL CHECK (one_off_allowance >= 0),
    debt_credit_folder_id text   CHECK (btrim(debt_credit_folder_id) <> ''),  -- Outlook folder id
    workbook_sheet       text,   -- sheet code in the superseded workbook (one-off import only)
    is_active            boolean NOT NULL DEFAULT true,
    notes                text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text        NOT NULL DEFAULT audit.current_actor(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    updated_by           text        NOT NULL DEFAULT audit.current_actor(),
    row_version          integer     NOT NULL DEFAULT 1,
    CHECK (company_number IS NOT NULL OR creditsafe_ref IS NOT NULL OR workbook_sheet IS NOT NULL),
    CHECK ((relationship_code = 'customer') = (customer_id IS NOT NULL)),
    CHECK (relationship_code <> 'supplier' OR supplier_id IS NOT NULL)
);
CREATE UNIQUE INDEX credit_subject_company_number_uq ON sales.credit_subject (company_number)
    WHERE company_number IS NOT NULL;
CREATE UNIQUE INDEX credit_subject_creditsafe_ref_uq ON sales.credit_subject (creditsafe_ref)
    WHERE creditsafe_ref IS NOT NULL;
CREATE UNIQUE INDEX credit_subject_customer_uq ON sales.credit_subject (customer_id) WHERE customer_id IS NOT NULL;
CREATE UNIQUE INDEX credit_subject_name_uq ON sales.credit_subject ((lower(btrim(display_name))));
CREATE UNIQUE INDEX credit_subject_sheet_uq ON sales.credit_subject (workbook_sheet) WHERE workbook_sheet IS NOT NULL;
COMMENT ON TABLE sales.credit_subject IS
    'Companies monitored with Experian / Creditsafe. Master data: created only by an explicit load.';
SELECT sales.attach_standard_triggers('sales.credit_subject');

-- ---------------------------------------------------------------- bureau evidence (append-only)
CREATE FUNCTION sales.tg_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only evidence: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$;

CREATE TABLE sales.credit_alert_email (
    credit_alert_email_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    internet_message_id   text        NOT NULL UNIQUE CHECK (btrim(internet_message_id) <> ''),
    mailbox_message_id    text,       -- Microsoft Graph id of the original (for copying)
    bureau_code           text        NOT NULL REFERENCES sales.credit_bureau,
    received_at           timestamptz NOT NULL,
    subject               text        NOT NULL,
    body_sha256           char(64)    NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    parse_error           text,       -- NULL = parsed; otherwise why nothing could be read
    companies             integer     NOT NULL CHECK (companies >= 0),
    loaded_at             timestamptz NOT NULL DEFAULT now(),
    loaded_by             text        NOT NULL DEFAULT audit.current_actor(),
    CHECK (parse_error IS NULL OR companies = 0)
);
COMMENT ON TABLE sales.credit_alert_email IS 'Every bureau monitoring email read (idempotent on Message-ID).';
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_alert_email
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();
CREATE TRIGGER audit_row AFTER INSERT ON sales.credit_alert_email
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();

-- One bureau reading of one company. limit_status distinguishes a value, "N/A" from the bureau,
-- and "the limit was not part of this alert" (e.g. a director change).
CREATE TABLE sales.credit_report (
    credit_report_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bureau_code           text        NOT NULL REFERENCES sales.credit_bureau,
    company_number        text        CHECK (company_number ~ '^[A-Z0-9]{8}$'),
    bureau_ref            text        CHECK (bureau_ref ~ '^[A-Z0-9]+$'),  -- the bureau's own id (Creditsafe Safe No.)
    credit_subject_id     bigint      REFERENCES sales.credit_subject,   -- only for workbook imports
    company_name          text        NOT NULL,
    observed_at           timestamptz NOT NULL,
    source_code           text        NOT NULL CHECK (source_code IN ('alert_email', 'workbook_import')),
    credit_alert_email_id bigint      REFERENCES sales.credit_alert_email,
    limit_status          text        NOT NULL CHECK (limit_status IN ('value', 'not_available', 'not_reported')),
    credit_limit          numeric(18,2) CHECK (credit_limit >= 0),
    previous_credit_limit numeric(18,2) CHECK (previous_credit_limit >= 0),
    credit_rating         numeric(18,2) CHECK (credit_rating >= 0),          -- Experian only
    risk_score            integer,
    risk_band             text,
    events                jsonb       NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(events) = 'array'),
    loaded_at             timestamptz NOT NULL DEFAULT now(),
    CHECK (company_number IS NOT NULL OR bureau_ref IS NOT NULL OR credit_subject_id IS NOT NULL),
    CHECK ((limit_status = 'value') = (credit_limit IS NOT NULL)),
    CHECK ((source_code = 'alert_email') = (credit_alert_email_id IS NOT NULL)),
    CHECK ((source_code = 'workbook_import') = (credit_subject_id IS NOT NULL))
);
CREATE INDEX credit_report_company_idx ON sales.credit_report (company_number, bureau_code, observed_at);
CREATE INDEX credit_report_ref_idx ON sales.credit_report (bureau_ref, bureau_code, observed_at);
CREATE UNIQUE INDEX credit_report_alert_uq ON sales.credit_report
    (credit_alert_email_id, COALESCE(company_number, bureau_ref)) WHERE credit_alert_email_id IS NOT NULL;
CREATE UNIQUE INDEX credit_report_import_uq ON sales.credit_report (credit_subject_id, bureau_code, observed_at)
    WHERE credit_subject_id IS NOT NULL;
COMMENT ON TABLE sales.credit_report IS 'Append-only bureau readings (one row per company per alert).';
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_report
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();
CREATE TRIGGER audit_row AFTER INSERT ON sales.credit_report
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();

-- Which subject a reading belongs to: by company number, else Creditsafe ref, else the import link.
CREATE VIEW sales.v_credit_report AS
SELECT r.credit_report_id, r.bureau_code,
       COALESCE(r.credit_subject_id, sn.credit_subject_id, sr.credit_subject_id) AS credit_subject_id,
       r.company_number, r.bureau_ref, r.company_name, r.observed_at, r.source_code,
       r.credit_alert_email_id, r.limit_status, r.credit_limit, r.previous_credit_limit,
       r.credit_rating, r.risk_score, r.risk_band, r.events
  FROM sales.credit_report r
  LEFT JOIN sales.credit_subject sn ON sn.company_number = r.company_number
  LEFT JOIN sales.credit_subject sr
         ON sr.creditsafe_ref = r.bureau_ref AND r.bureau_code = 'creditsafe';

-- Each subject's current position with each bureau: the latest limit and the latest band/score.
CREATE VIEW sales.v_credit_bureau_position AS
WITH lim AS (
    SELECT DISTINCT ON (credit_subject_id, bureau_code)
           credit_subject_id, bureau_code, credit_report_id, credit_limit, observed_at
      FROM sales.v_credit_report
     WHERE credit_subject_id IS NOT NULL AND limit_status <> 'not_reported'
     ORDER BY credit_subject_id, bureau_code, observed_at DESC, credit_report_id DESC
), band AS (
    SELECT DISTINCT ON (credit_subject_id, bureau_code)
           credit_subject_id, bureau_code, risk_band, risk_score, observed_at
      FROM sales.v_credit_report
     WHERE credit_subject_id IS NOT NULL AND (risk_band IS NOT NULL OR risk_score IS NOT NULL)
     ORDER BY credit_subject_id, bureau_code, observed_at DESC, credit_report_id DESC
), last_seen AS (
    SELECT credit_subject_id, bureau_code, max(observed_at) AS last_observed_at
      FROM sales.v_credit_report WHERE credit_subject_id IS NOT NULL
     GROUP BY credit_subject_id, bureau_code
)
SELECT s.credit_subject_id, b.bureau_code,
       lim.credit_report_id AS limit_report_id, lim.credit_limit, lim.observed_at AS limit_observed_at,
       band.risk_band, band.risk_score, ls.last_observed_at,
       COALESCE(rb.requires_review, false) AS band_requires_review
  FROM sales.credit_subject s
 CROSS JOIN sales.credit_bureau b
  LEFT JOIN lim  ON lim.credit_subject_id = s.credit_subject_id AND lim.bureau_code = b.bureau_code
  LEFT JOIN band ON band.credit_subject_id = s.credit_subject_id AND band.bureau_code = b.bureau_code
  LEFT JOIN last_seen ls ON ls.credit_subject_id = s.credit_subject_id AND ls.bureau_code = b.bureau_code
  LEFT JOIN sales.credit_risk_band rb ON rb.bureau_code = b.bureau_code AND rb.band = band.risk_band
 WHERE lim.credit_subject_id IS NOT NULL OR band.credit_subject_id IS NOT NULL;

-- ---------------------------------------------------------------- ARR file snapshots (append-only)
CREATE TABLE sales.credit_arr_snapshot (
    credit_arr_snapshot_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_name            text        NOT NULL CHECK (btrim(source_name) <> ''),
    source_sha256          char(64)    NOT NULL UNIQUE CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_modified_at     timestamptz,
    line_count             integer     NOT NULL CHECK (line_count >= 0),
    loaded_at              timestamptz NOT NULL DEFAULT now(),
    loaded_by              text        NOT NULL DEFAULT audit.current_actor()
);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_arr_snapshot
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();
CREATE TRIGGER audit_row AFTER INSERT ON sales.credit_arr_snapshot
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();

CREATE TABLE sales.credit_arr_line (
    credit_arr_snapshot_id bigint  NOT NULL REFERENCES sales.credit_arr_snapshot,
    source_row             integer NOT NULL CHECK (source_row > 0),
    customer_name          text    NOT NULL,
    internal_ref           text,
    arr_prefix             char(3) GENERATED ALWAYS AS (substring(internal_ref FROM '^([A-Z]{3})[0-9]')) STORED,
    arr_status             text    NOT NULL REFERENCES sales.credit_arr_status,
    arr_frequency          text    NOT NULL REFERENCES sales.credit_exposure_rule,
    annual_revenue         numeric(18,2) NOT NULL,
    PRIMARY KEY (credit_arr_snapshot_id, source_row)
);
CREATE INDEX credit_arr_line_prefix_idx ON sales.credit_arr_line (credit_arr_snapshot_id, arr_prefix);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_arr_line
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();

CREATE FUNCTION sales.latest_credit_arr_snapshot() RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT max(credit_arr_snapshot_id) FROM sales.credit_arr_snapshot
$$;

-- Recurring exposure by frequency for an ARR prefix in a snapshot (commitments only).
CREATE FUNCTION sales.credit_arr_exposure(p_snapshot_id bigint, p_arr_prefix text)
RETURNS TABLE (arr_frequency text, annual_revenue numeric, exposure numeric, line_count integer)
LANGUAGE sql STABLE AS $$
    SELECT l.arr_frequency, sum(l.annual_revenue),
           round(sum(l.annual_revenue) / r.exposure_divisor * r.invoices_exposed, 2),
           count(*)::integer
      FROM sales.credit_arr_line l
      JOIN sales.credit_arr_status st USING (arr_status)
      JOIN sales.credit_exposure_rule r USING (arr_frequency)
     WHERE l.credit_arr_snapshot_id = p_snapshot_id
       AND l.arr_prefix = p_arr_prefix
       AND st.counts_as_commitment
     GROUP BY l.arr_frequency, r.exposure_divisor, r.invoices_exposed
$$;

-- ---------------------------------------------------------------- assessments (immutable)
CREATE TABLE sales.credit_assessment (
    credit_assessment_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    credit_subject_id      bigint      NOT NULL REFERENCES sales.credit_subject,
    trigger_code           text        NOT NULL CHECK (trigger_code IN
                               ('initial', 'bureau_change', 'risk_band_change', 'arr_change',
                                'allowance_change', 'manual')),
    experian_report_id     bigint      REFERENCES sales.credit_report,
    experian_limit         numeric(18,2) CHECK (experian_limit >= 0),
    experian_observed_at   timestamptz,
    experian_band          text,
    experian_score         integer,
    creditsafe_report_id   bigint      REFERENCES sales.credit_report,
    creditsafe_limit       numeric(18,2) CHECK (creditsafe_limit >= 0),
    creditsafe_observed_at timestamptz,
    baseline               numeric(18,2) GENERATED ALWAYS AS (LEAST(experian_limit, creditsafe_limit)) STORED,
    credit_arr_snapshot_id bigint      REFERENCES sales.credit_arr_snapshot,
    arr_prefix             char(3),
    one_off_allowance      numeric(18,2) NOT NULL CHECK (one_off_allowance >= 0),
    vat_rate               numeric(6,4)  NOT NULL CHECK (vat_rate >= 0),
    round_to               numeric(18,2) NOT NULL CHECK (round_to > 0),
    appetite_pct           numeric(6,4)  NOT NULL CHECK (appetite_pct > 0),
    appetite_threshold     numeric(18,2) NOT NULL CHECK (appetite_threshold >= 0),
    outcome_code           text        NOT NULL CHECK (outcome_code IN
                               ('applied', 'unchanged', 'cfo_review', 'override_in_force', 'not_a_customer')),
    review_reason          text,
    assessed_at            timestamptz NOT NULL DEFAULT now(),
    assessed_by            text        NOT NULL DEFAULT audit.current_actor(),
    CHECK ((outcome_code = 'cfo_review') = (review_reason IS NOT NULL)),
    CHECK ((experian_limit IS NULL OR experian_report_id IS NOT NULL)
           AND (creditsafe_limit IS NULL OR creditsafe_report_id IS NOT NULL))
);
CREATE INDEX credit_assessment_subject_idx ON sales.credit_assessment (credit_subject_id, credit_assessment_id);
COMMENT ON TABLE sales.credit_assessment IS
    'One frozen credit assessment. Figures are in sales.v_credit_assessment; nothing is typed in.';
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_assessment
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();
CREATE TRIGGER audit_row AFTER INSERT ON sales.credit_assessment
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();

CREATE TABLE sales.credit_assessment_line (
    credit_assessment_id bigint  NOT NULL REFERENCES sales.credit_assessment,
    arr_frequency        text    NOT NULL REFERENCES sales.credit_exposure_rule,
    annual_revenue       numeric(18,2) NOT NULL,
    exposure             numeric(18,2) NOT NULL,
    line_count           integer NOT NULL CHECK (line_count > 0),
    PRIMARY KEY (credit_assessment_id, arr_frequency)
);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_assessment_line
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();

-- The arithmetic, derived from the frozen inputs (never stored, so never inconsistent).
CREATE VIEW sales.v_credit_assessment AS
WITH lines AS (
    SELECT credit_assessment_id, sum(exposure) AS recurring_exposure
      FROM sales.credit_assessment_line GROUP BY credit_assessment_id
), base AS (
    SELECT a.credit_assessment_id, a.credit_subject_id, a.trigger_code, a.outcome_code, a.review_reason,
           a.experian_limit, a.experian_observed_at, a.experian_band, a.experian_score,
           a.creditsafe_limit, a.creditsafe_observed_at, a.baseline,
           a.credit_arr_snapshot_id, a.arr_prefix, a.one_off_allowance, a.vat_rate, a.round_to,
           a.appetite_pct, a.appetite_threshold, a.assessed_at, a.assessed_by,
           COALESCE(l.recurring_exposure, 0) AS recurring_exposure,
           COALESCE(l.recurring_exposure, 0) + a.one_off_allowance AS net_requirement
      FROM sales.credit_assessment a
      LEFT JOIN lines l USING (credit_assessment_id)
), calc AS (
    SELECT b.*, round(b.net_requirement * b.vat_rate, 2) AS vat,
           b.net_requirement + round(b.net_requirement * b.vat_rate, 2) AS gross_requirement
      FROM base b
)
SELECT c.credit_assessment_id, c.credit_subject_id, c.trigger_code, c.outcome_code, c.review_reason,
       c.experian_limit, c.experian_observed_at, c.experian_band, c.experian_score,
       c.creditsafe_limit, c.creditsafe_observed_at, c.baseline,
       c.credit_arr_snapshot_id, c.arr_prefix, c.one_off_allowance, c.vat_rate, c.round_to,
       c.appetite_pct, c.appetite_threshold, c.assessed_at, c.assessed_by,
       c.recurring_exposure, c.net_requirement, c.vat, c.gross_requirement,
       ceil(c.gross_requirement / c.round_to) * c.round_to             AS trading_requirement,
       round(COALESCE(c.baseline, 0) * c.appetite_pct, 2)               AS risk_appetite,
       COALESCE(c.baseline, 0) * c.appetite_pct <= c.appetite_threshold AS appetite_applies,
       ceil(c.gross_requirement / c.round_to) * c.round_to              AS recommended_limit
  FROM calc c;

-- ---------------------------------------------------------------- credit limit register
CREATE TABLE sales.customer_credit_limit (
    customer_credit_limit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id              bigint        NOT NULL REFERENCES sales.customer,
    credit_limit             numeric(18,2) NOT NULL CHECK (credit_limit >= 0),
    effective_from           timestamptz   NOT NULL DEFAULT now(),
    effective_to             timestamptz,
    source_code              text          NOT NULL CHECK (source_code IN ('assessment', 'cfo_decision')),
    credit_assessment_id     bigint        REFERENCES sales.credit_assessment,
    reason                   text          CHECK (reason IS NULL OR btrim(reason) <> ''),
    review_by                date,
    approved_by_employee_id  bigint        REFERENCES sales.employee,
    approved_at              timestamptz,
    created_at               timestamptz   NOT NULL DEFAULT now(),
    created_by               text          NOT NULL DEFAULT audit.current_actor(),
    updated_at               timestamptz   NOT NULL DEFAULT now(),
    updated_by               text          NOT NULL DEFAULT audit.current_actor(),
    row_version              integer       NOT NULL DEFAULT 1,
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK (source_code <> 'assessment' OR credit_assessment_id IS NOT NULL),
    CHECK (source_code <> 'cfo_decision'
           OR (reason IS NOT NULL AND approved_by_employee_id IS NOT NULL AND approved_at IS NOT NULL)),
    CONSTRAINT customer_credit_limit_no_overlap EXCLUDE USING gist (
        customer_id WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    )
);
COMMENT ON TABLE sales.customer_credit_limit IS
    'The register of customer credit limits over time: automatic (within appetite) or a CFO decision.';
SELECT sales.attach_standard_triggers('sales.customer_credit_limit');

-- Limits are history: only an open period may be closed. An automatic limit must be exactly the
-- recommendation of an assessment that did not need review; anything else is a CFO decision.
CREATE FUNCTION sales.tg_customer_credit_limit_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_a record;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.effective_to IS NOT NULL
           OR (NEW.customer_id, NEW.credit_limit, NEW.effective_from, NEW.source_code, NEW.credit_assessment_id,
               NEW.reason, NEW.review_by, NEW.approved_by_employee_id, NEW.approved_at)
              IS DISTINCT FROM
              (OLD.customer_id, OLD.credit_limit, OLD.effective_from, OLD.source_code, OLD.credit_assessment_id,
               OLD.reason, OLD.review_by, OLD.approved_by_employee_id, OLD.approved_at) THEN
            RAISE EXCEPTION 'credit limits are history: only an open period can be closed (set a new limit instead)'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.credit_assessment_id IS NOT NULL THEN
        SELECT a.outcome_code, a.recommended_limit, s.customer_id INTO v_a
          FROM sales.v_credit_assessment a JOIN sales.credit_subject s USING (credit_subject_id)
         WHERE a.credit_assessment_id = NEW.credit_assessment_id;
        IF v_a.customer_id IS DISTINCT FROM NEW.customer_id THEN
            RAISE EXCEPTION 'credit assessment % is for another customer', NEW.credit_assessment_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF NEW.source_code = 'assessment' THEN
        IF NEW.credit_assessment_id IS NULL THEN
            RAISE EXCEPTION 'an automatic credit limit needs its assessment' USING ERRCODE = 'check_violation';
        END IF;
        IF v_a.outcome_code <> 'applied' OR NEW.credit_limit <> v_a.recommended_limit THEN
            RAISE EXCEPTION 'an automatic credit limit must be the recommendation of an assessment within appetite'
                USING ERRCODE = 'check_violation',
                      HINT = 'anything else is a CFO decision (sales-orders credit-decide)';
        END IF;
        NEW.approved_by_employee_id := NULL;
        NEW.approved_at := NULL;
    ELSE
        PERFORM sales.require_permission('approve_credit_terms', 'setting a credit limit');
        NEW.approved_by_employee_id := sales.current_employee_id();
        NEW.approved_at := now();
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER credit_limit_rules BEFORE INSERT OR UPDATE ON sales.customer_credit_limit
    FOR EACH ROW EXECUTE FUNCTION sales.tg_customer_credit_limit_rules();

CREATE VIEW sales.v_customer_current_credit_limit AS
SELECT l.customer_credit_limit_id, l.customer_id, l.credit_limit, l.effective_from, l.source_code,
       l.credit_assessment_id, l.reason, l.review_by, l.approved_by_employee_id, l.approved_at
  FROM sales.customer_credit_limit l
 WHERE l.effective_from <= now() AND (l.effective_to IS NULL OR l.effective_to > now());

-- Close the open period and open a new one, in one step.
CREATE FUNCTION sales.set_credit_limit(
    p_customer_id bigint, p_credit_limit numeric, p_source text, p_assessment_id bigint,
    p_reason text, p_review_by date
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_id bigint;
BEGIN
    UPDATE sales.customer_credit_limit SET effective_to = now()
     WHERE customer_id = p_customer_id AND effective_to IS NULL;
    INSERT INTO sales.customer_credit_limit
           (customer_id, credit_limit, source_code, credit_assessment_id, reason, review_by)
    VALUES (p_customer_id, p_credit_limit, p_source, p_assessment_id, p_reason, p_review_by)
    RETURNING customer_credit_limit_id INTO v_id;
    RETURN v_id;
END
$$;

-- One source for credit limits from now on (the register above).
ALTER TABLE sales.customer_credit_terms
    ADD CONSTRAINT customer_credit_terms_no_limit CHECK (credit_limit IS NULL);
COMMENT ON COLUMN sales.customer_credit_terms.credit_limit IS
    'Superseded by sales.customer_credit_limit (migration 0014); always NULL.';

-- ---------------------------------------------------------------- creating an assessment
-- The current inputs for a subject, as an assessment would freeze them now.
CREATE VIEW sales.v_credit_subject_inputs AS
SELECT s.credit_subject_id, s.display_name, s.relationship_code, s.customer_id, c.arr_prefix,
       s.one_off_allowance, rel.is_assessed, rel.is_client,
       ex.limit_report_id AS experian_report_id, ex.credit_limit AS experian_limit,
       ex.limit_observed_at AS experian_observed_at, ex.risk_band AS experian_band, ex.risk_score AS experian_score,
       COALESCE(ex.band_requires_review, false) AS band_requires_review,
       cs.limit_report_id AS creditsafe_report_id, cs.credit_limit AS creditsafe_limit,
       cs.limit_observed_at AS creditsafe_observed_at,
       sales.latest_credit_arr_snapshot() AS credit_arr_snapshot_id,
       COALESCE((SELECT sum(e.exposure)
                   FROM sales.credit_arr_exposure(sales.latest_credit_arr_snapshot(), c.arr_prefix) e), 0)
           AS recurring_exposure
  FROM sales.credit_subject s
  JOIN sales.credit_relationship rel USING (relationship_code)
  LEFT JOIN sales.customer c USING (customer_id)
  LEFT JOIN sales.v_credit_bureau_position ex
         ON ex.credit_subject_id = s.credit_subject_id AND ex.bureau_code = 'experian'
  LEFT JOIN sales.v_credit_bureau_position cs
         ON cs.credit_subject_id = s.credit_subject_id AND cs.bureau_code = 'creditsafe'
 WHERE s.is_active;

-- Which subjects need a new assessment, and why (the orchestrator assesses exactly these).
CREATE VIEW sales.v_credit_assessment_due AS
WITH last AS (
    SELECT DISTINCT ON (credit_subject_id) credit_subject_id, credit_assessment_id, experian_limit,
           creditsafe_limit, experian_band, one_off_allowance, recurring_exposure
      FROM sales.v_credit_assessment
     ORDER BY credit_subject_id, credit_assessment_id DESC
), p AS (
    SELECT numeric_value = 1 AS on_arr FROM sales.policy_setting WHERE setting_key = 'credit_reassess_on_arr_change'
)
SELECT i.credit_subject_id, i.display_name,
       CASE WHEN l.credit_subject_id IS NULL THEN 'initial'
            WHEN i.experian_limit IS DISTINCT FROM l.experian_limit
              OR i.creditsafe_limit IS DISTINCT FROM l.creditsafe_limit THEN 'bureau_change'
            WHEN i.experian_band IS DISTINCT FROM l.experian_band THEN 'risk_band_change'
            WHEN i.one_off_allowance <> l.one_off_allowance THEN 'allowance_change'
            ELSE 'arr_change'
       END AS trigger_code
  FROM sales.v_credit_subject_inputs i
  LEFT JOIN last l USING (credit_subject_id)
 CROSS JOIN p
 WHERE i.is_assessed
   AND (i.experian_limit IS NOT NULL OR i.creditsafe_limit IS NOT NULL OR l.credit_subject_id IS NOT NULL)
   AND (l.credit_subject_id IS NULL
        OR i.experian_limit IS DISTINCT FROM l.experian_limit
        OR i.creditsafe_limit IS DISTINCT FROM l.creditsafe_limit
        OR i.experian_band IS DISTINCT FROM l.experian_band
        OR i.one_off_allowance <> l.one_off_allowance
        OR (p.on_arr AND i.recurring_exposure <> l.recurring_exposure));

-- Freeze a subject's current inputs as an assessment, decide the outcome, and apply the limit when
-- it is within appetite. Returns the assessment id.
CREATE FUNCTION sales.create_credit_assessment(p_subject_id bigint, p_trigger text) RETURNS bigint
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

-- ---------------------------------------------------------------- filing (outbox)
-- Work the orchestrator must do outside the database: put the snapshot PDF on the Xero contact,
-- and copy each alert email into the client's Debt & Credit folder. Retried until done.
CREATE TABLE sales.credit_filing_task (
    credit_filing_task_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_code             text    NOT NULL CHECK (task_code IN ('xero_snapshot', 'mail_copy')),
    credit_subject_id     bigint  NOT NULL REFERENCES sales.credit_subject,
    credit_assessment_id  bigint  REFERENCES sales.credit_assessment,
    credit_alert_email_id bigint  REFERENCES sales.credit_alert_email,
    status_code           text    NOT NULL DEFAULT 'pending' CHECK (status_code IN ('pending', 'done', 'failed')),
    attempts              integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error            text,
    external_ref          text,   -- Xero attachment id / copied message id
    completed_at          timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text        NOT NULL DEFAULT audit.current_actor(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            text        NOT NULL DEFAULT audit.current_actor(),
    row_version           integer     NOT NULL DEFAULT 1,
    CHECK ((task_code = 'xero_snapshot') = (credit_assessment_id IS NOT NULL)),
    CHECK ((task_code = 'mail_copy') = (credit_alert_email_id IS NOT NULL)),
    CHECK ((status_code = 'done') = (completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX credit_filing_xero_uq ON sales.credit_filing_task (credit_assessment_id)
    WHERE task_code = 'xero_snapshot';
CREATE UNIQUE INDEX credit_filing_mail_uq ON sales.credit_filing_task (credit_alert_email_id, credit_subject_id)
    WHERE task_code = 'mail_copy';
SELECT sales.attach_standard_triggers('sales.credit_filing_task');

-- The PDF snapshot of an assessment's summary page, exactly as filed.
CREATE TABLE sales.credit_assessment_snapshot (
    credit_assessment_id bigint PRIMARY KEY REFERENCES sales.credit_assessment,
    file_name            text   NOT NULL CHECK (file_name ~ '\.pdf$'),
    pdf                  bytea  NOT NULL CHECK (octet_length(pdf) > 0),
    sha256               char(64) GENERATED ALWAYS AS (encode(sha256(pdf), 'hex')) STORED,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text        NOT NULL DEFAULT audit.current_actor()
);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_assessment_snapshot
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();

-- Queue whatever filing is missing (idempotent). Clients only. A snapshot is filed once the limit is
-- settled: at once when automatic, after the CFO's decision when it needed review.
CREATE FUNCTION sales.queue_credit_filing() RETURNS integer
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

-- ---------------------------------------------------------------- exceptions
CREATE TABLE sales.credit_exception_rule (
    rule_code   text PRIMARY KEY CHECK (rule_code ~ '^[A-Z][A-Z_]+$'),
    severity    text NOT NULL CHECK (severity IN ('error', 'warning')),
    description text NOT NULL,
    action      text NOT NULL
);
INSERT INTO sales.credit_exception_rule (rule_code, severity, description, action) VALUES
    ('CREDIT_REVIEW_NEEDED',       'error',   'An assessment is outside risk appetite and waits for the CFO',
                                             'Decide the limit: sales-orders credit-decide'),
    ('ALERT_NOT_READ',             'error',   'A bureau alert email could not be read (format changed?)',
                                             'Send the email to the developer; nothing was loaded from it'),
    ('FILING_FAILED',              'error',   'A Xero snapshot or Debt & Credit copy failed after retries',
                                             'See the error; the next run retries'),
    ('CUSTOMER_NO_ARR_PREFIX',     'warning', 'A monitored customer has no ARR prefix: assessed as having no recurring commitments',
                                             'If it has ARR lines, add arr_prefix to the customer (master data)'),
    ('CUSTOMER_NO_XERO_CONTACT',   'error',   'A monitored customer has no Xero contact, so nothing can be filed in Xero',
                                             'Add xero_contact_id to the customer (master data)'),
    ('UNKNOWN_COMPANY',            'warning', 'A bureau reported on a company that is not in the monitored list',
                                             'Classify it once: customer, prospect, supplier or information'),
    ('ARR_NOT_MONITORED',          'warning', 'Recurring revenue in the ARR file for a prefix with no credit monitoring',
                                             'Add the company to monitoring, or confirm the ARR prefix'),
    ('NO_BUREAU_LIMIT',            'warning', 'A monitored company has no limit from either bureau',
                                             'Add it to the Experian and Creditsafe portfolios'),
    ('SINGLE_BUREAU',              'warning', 'A monitored company has a limit from only one bureau',
                                             'Add it to the missing bureau''s portfolio'),
    ('STALE_BUREAU_DATA',          'warning', 'The latest bureau reading is older than policy',
                                             'Check it is still in the bureau portfolio'),
    ('ADVERSE_RISK_BAND',          'warning', 'A monitored company is in a risk band that needs review',
                                             'Review exposure (suppliers: supply risk)'),
    ('NO_DEBT_CREDIT_FOLDER',      'warning', 'A monitored customer has no Debt & Credit mail folder mapped',
                                             'Run credit-map-folders, or create the folder'),
    ('CREDIT_DECISION_REVIEW_DUE', 'warning', 'A CFO credit decision has passed its review date',
                                             'Re-decide, or let the next assessment apply');

CREATE VIEW sales.v_credit_exception AS
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
