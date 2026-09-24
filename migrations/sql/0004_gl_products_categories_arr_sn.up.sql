-- =============================================================================
-- 0004 GL codes, product database, Register categories, ARR database, SN reference.
--
-- Vocabulary is aligned with the existing AW SOs Register and the earlier SQLite
-- build (see docs/data-model.md "Alignment with AW SOs"). Rules adopted from it:
--   * SN refs are NEVER generated here: AW SOs assigns them. Orders are held under their
--     internal number until the SN is sourced from the Register.
--   * ARR refs (e.g. TIL030) are assigned by the ARR file, not the database.
--   * Microsoft CSP SKUs (CFQ7...) map to GL 1233 Cloud Services: Office 365.
-- =============================================================================

-- ---------------------------------------------------------------- GL (mirror of Xero)
CREATE TABLE sales.gl_account (
    account_code    text PRIMARY KEY CHECK (account_code ~ '^[0-9]{4}[A-Z]{0,2}$'),
    name            text NOT NULL,
    account_class   text NOT NULL CHECK (account_class IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')),
    account_type    text NOT NULL,        -- Xero type: REVENUE, DIRECTCOSTS, CURRLIAB, ...
    tax_type        text,
    xero_account_id uuid UNIQUE,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text        NOT NULL DEFAULT audit.current_actor(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text        NOT NULL DEFAULT audit.current_actor(),
    row_version     integer     NOT NULL DEFAULT 1
);
COMMENT ON TABLE sales.gl_account IS 'Mirror of the Xero chart of accounts (sales-relevant). Xero is the master.';
SELECT sales.attach_standard_triggers('sales.gl_account');

-- Seed only the accounts the rules below depend on; the full chart is synced from Xero.
INSERT INTO sales.gl_account (account_code, name, account_class, account_type, tax_type, xero_account_id) VALUES
    ('1233', 'Cloud Services: Office 365', 'REVENUE', 'REVENUE',     'OUTPUT2', '11ff66b5-90d5-4cb7-83b2-8dac6cd0d88c'),
    ('2233', 'Office 365 (COS)',           'EXPENSE', 'DIRECTCOSTS', 'INPUT2',  NULL);

-- ---------------------------------------------------------------- Register vocabulary
CREATE TABLE sales.service_category (
    service_category_code      text PRIMARY KEY,
    name                       text NOT NULL UNIQUE,   -- as written on the Register / Details
    default_revenue_gl_code    text REFERENCES sales.gl_account,
    default_cost_gl_code       text REFERENCES sales.gl_account,
    sort_order                 smallint NOT NULL UNIQUE
);
INSERT INTO sales.service_category (service_category_code, name, default_revenue_gl_code, default_cost_gl_code, sort_order) VALUES
    ('cloud_services', 'Cloud Services',  '1233', '2233', 10),
    ('prof_services',  'Prof. Services',  NULL, NULL, 20),
    ('hardware',       'Hardware',        NULL, NULL, 30),
    ('security',       'Security',        NULL, NULL, 40),
    ('support',        'Support',         NULL, NULL, 50),
    ('other_revenue',  'Other Revenue',   NULL, NULL, 60),
    ('monitoring',     'Monitoring',      NULL, NULL, 70),
    ('data_centre',    'Data Centre',     NULL, NULL, 80),
    ('breakfix',       'Breakfix',        NULL, NULL, 90),
    ('recur_software', 'Recur. Software', NULL, NULL, 100),
    ('connectivity',   'Connectivity',    NULL, NULL, 110),
    ('backup',         'Backup',          NULL, NULL, 120),
    ('discount',       'Discount',        NULL, NULL, 130),
    ('perp_software',  'Perp. Software',  NULL, NULL, 140);

CREATE TABLE sales.reporting_category (
    reporting_category_code text PRIMARY KEY,
    description             text NOT NULL,
    definition_confirmed    boolean NOT NULL DEFAULT false
);
INSERT INTO sales.reporting_category (reporting_category_code, description) VALUES
    ('NET_NEW',              'Net new'),
    ('RENEWAL',              'Renewal'),
    ('EXPANSION_NN',         'Expansion (NN - definition to confirm)'),
    ('EXPANSION_E',          'Expansion (E - definition to confirm)'),
    ('RETENTION',            'Retention'),
    ('CHURN_NN',             'Churn (NN - definition to confirm)'),
    ('CHURN_E',              'Churn (E - definition to confirm)'),
    ('LAST_ORDER_RENEWAL',   'Last order - renewal'),
    ('LAST_ORDER_RETENTION', 'Last order - retention');

CREATE TABLE sales.order_document_type (
    order_document_type_code text PRIMARY KEY,
    description              text NOT NULL
);
INSERT INTO sales.order_document_type VALUES
    ('QUOTATION', 'Quotation'), ('PROPOSAL', 'Proposal'), ('SELF_SERVE', 'Self-serve'),
    ('AUTO_RENEWAL', 'Auto-renewal'), ('CANCELLATION', 'Cancellation'), ('LAST_ORDER', 'Last order'),
    ('VARIATION', 'Contract variation'), ('REQUEST', 'Request'), ('ORDER', 'Order'),
    ('PURCHASE_ORDER', 'Customer purchase order');

-- Order type becomes the Register's "Document Type" (order category).
INSERT INTO sales.order_type (order_type_code, description) VALUES
    ('NEW_ORDER',  'New order'),
    ('VOLUME',     'Volume change / add-on for an existing service'),
    ('RENEWAL',    'Renewal'),
    ('LAST_ORDER', 'Last order: Register reversal row (SNnnnnnnLO) of the contract being renewed'),
    ('CHURN',      'Churn / cancellation');
UPDATE sales.sales_order SET order_type_code = CASE order_type_code
        WHEN 'new' THEN 'NEW_ORDER' WHEN 'additional' THEN 'VOLUME'
        WHEN 'renewal' THEN 'RENEWAL' WHEN 'amendment' THEN 'VOLUME' ELSE order_type_code END
 WHERE order_type_code IN ('new', 'additional', 'renewal', 'amendment');
DELETE FROM sales.order_type WHERE order_type_code IN ('new', 'additional', 'renewal', 'amendment');

ALTER TABLE sales.sales_order
    ADD COLUMN sn_ref text UNIQUE CHECK (sn_ref ~ '^SN([0-9]{4}|[0-9]{6})(LO|CA)?$'),
    ADD COLUMN sn_source text,
    ADD COLUMN sn_assigned_at timestamptz,
    ADD COLUMN order_document_type_code text REFERENCES sales.order_document_type,
    ADD COLUMN reporting_category_code text REFERENCES sales.reporting_category,
    ADD COLUMN previous_sales_order_id bigint REFERENCES sales.sales_order,   -- the LAST_ORDER being renewed
    ADD COLUMN ticket_reference text,                                        -- Halo ticket
    ADD COLUMN project text,
    ADD COLUMN signed_by_customer text,
    ADD COLUMN signed_by_managed247 text,
    ADD CONSTRAINT sales_order_sn_has_source CHECK ((sn_ref IS NULL) = (sn_source IS NULL)
                                                    AND (sn_ref IS NULL) = (sn_assigned_at IS NULL));
COMMENT ON COLUMN sales.sales_order.sn_ref IS
    'SN reference assigned by the AW SOs Register (e.g. SN260518). Never generated by this system.';

ALTER TABLE sales.customer
    ADD COLUMN xero_tracking_customer text,    -- Xero "Customer" tracking option (spelling differs from contact)
    ADD COLUMN arr_prefix char(3) UNIQUE CHECK (arr_prefix ~ '^[A-Z]{3}$');

-- ---------------------------------------------------------------- product database
ALTER TABLE sales.product
    ADD COLUMN description               text,
    ADD COLUMN vendor_part_number        text,
    ADD COLUMN service_category_code     text REFERENCES sales.service_category,
    ADD COLUMN default_billing_frequency_code text REFERENCES sales.billing_frequency,
    ADD COLUMN default_revenue_gl_code   text REFERENCES sales.gl_account,
    ADD COLUMN default_cost_gl_code      text REFERENCES sales.gl_account,
    ADD COLUMN list_price                numeric(18,4) CHECK (list_price >= 0),
    ADD COLUMN list_cost                 numeric(18,4) CHECK (list_cost >= 0),
    ADD COLUMN pandadoc_catalog_item_id  uuid UNIQUE;
COMMENT ON TABLE sales.product IS 'Product database: one row per sellable SKU, with its default category, GL codes and pricing.';

-- ---------------------------------------------------------------- lines: GL, category, dates, ARR
CREATE TABLE sales.arr_treatment (
    arr_treatment_code text PRIMARY KEY,
    description        text NOT NULL
);
INSERT INTO sales.arr_treatment VALUES
    ('arr',  'Counts towards ARR'),
    ('stub', 'Recurring charge for a part period (co-term stub): excluded from ARR'),
    ('none', 'Not ARR (one-off)');

ALTER TABLE sales.sales_order_line
    ADD COLUMN service_category_code text REFERENCES sales.service_category,
    ADD COLUMN revenue_gl_code      text REFERENCES sales.gl_account,
    ADD COLUMN cost_gl_code         text REFERENCES sales.gl_account,
    ADD COLUMN service_start_date   date,
    ADD COLUMN service_end_date     date,
    ADD COLUMN arr_treatment_code   text REFERENCES sales.arr_treatment,
    ADD COLUMN arr_contract_id      bigint,   -- FK added below
    ADD COLUMN supplier_po_number   text,
    ADD CONSTRAINT sales_order_line_service_dates CHECK (service_end_date IS NULL OR service_start_date IS NULL
                                                         OR service_end_date >= service_start_date);

-- The line view is rebuilt with an EXPLICIT column list (0001 used l.*, which PostgreSQL freezes
-- at creation, so new columns never appeared). New columns are appended, as CREATE OR REPLACE requires.
CREATE OR REPLACE VIEW sales.v_sales_order_line AS
SELECT l.sales_order_line_id, l.sales_order_id, l.line_number, l.product_id, l.sku, l.description,
       l.line_category_code, l.supplier_id, l.supplier_quote_id, l.quantity, l.billing_frequency_code,
       l.billing_periods, l.cost_currency_code, l.unit_cost_in_cost_currency, l.fx_rate_id, l.fx_rate,
       l.unit_sell, l.unit_cost, l.net_cost, l.net_sell, l.gross_margin, l.notes, l.created_at, l.created_by,
       l.updated_at, l.updated_by, l.row_version,
       bf.months_per_period,
       (bf.months_per_period IS NOT NULL)                                           AS is_recurring,
       CASE WHEN bf.months_per_period IS NOT NULL
            THEN round(l.quantity * l.unit_sell / bf.months_per_period, 2) END      AS monthly_recurring_sell,
       CASE WHEN bf.months_per_period IS NOT NULL
            THEN round(l.quantity * l.unit_cost / bf.months_per_period, 2) END      AS monthly_recurring_cost,
       CASE WHEN l.net_sell = 0 THEN NULL
            ELSE round(l.gross_margin / l.net_sell * 100, 2) END                    AS gross_margin_pct,
       l.margin_rationale, l.service_category_code, l.revenue_gl_code, l.cost_gl_code,
       l.service_start_date, l.service_end_date, l.arr_treatment_code, l.arr_contract_id,
       l.supplier_po_number
  FROM sales.sales_order_line l
  JOIN sales.billing_frequency bf USING (billing_frequency_code);

-- GL codes must be of the right kind: revenue on revenue, direct cost on cost.
CREATE FUNCTION sales.tg_line_defaults_and_gl() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_product  sales.product%ROWTYPE;
    v_category sales.service_category%ROWTYPE;
    v_class    text;
    v_type     text;
    v_months   smallint;
BEGIN
    IF NEW.product_id IS NOT NULL THEN
        SELECT * INTO v_product FROM sales.product WHERE product_id = NEW.product_id;
        NEW.service_category_code := COALESCE(NEW.service_category_code, v_product.service_category_code);
        NEW.revenue_gl_code := COALESCE(NEW.revenue_gl_code, v_product.default_revenue_gl_code);
        NEW.cost_gl_code    := COALESCE(NEW.cost_gl_code, v_product.default_cost_gl_code);
    END IF;
    -- Rule adopted from the Register history: Microsoft CSP SKUs are Cloud Services / 1233.
    IF upper(COALESCE(NEW.sku, '')) LIKE 'CFQ7%' THEN
        NEW.service_category_code := COALESCE(NEW.service_category_code, 'cloud_services');
    END IF;
    IF NEW.service_category_code IS NOT NULL THEN
        SELECT * INTO v_category FROM sales.service_category
         WHERE service_category_code = NEW.service_category_code;
        NEW.revenue_gl_code := COALESCE(NEW.revenue_gl_code, v_category.default_revenue_gl_code);
        NEW.cost_gl_code    := COALESCE(NEW.cost_gl_code, v_category.default_cost_gl_code);
    END IF;

    IF NEW.revenue_gl_code IS NOT NULL THEN
        SELECT account_class, account_type INTO v_class, v_type FROM sales.gl_account WHERE account_code = NEW.revenue_gl_code;
        IF v_class <> 'REVENUE' THEN
            RAISE EXCEPTION 'line %: revenue GL % is a % account, not revenue', NEW.line_number, NEW.revenue_gl_code, v_class
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.cost_gl_code IS NOT NULL THEN
        SELECT account_class, account_type INTO v_class, v_type FROM sales.gl_account WHERE account_code = NEW.cost_gl_code;
        IF v_type <> 'DIRECTCOSTS' THEN
            RAISE EXCEPTION 'line %: cost GL % is type %, not a direct cost', NEW.line_number, NEW.cost_gl_code, v_type
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    SELECT months_per_period INTO v_months FROM sales.billing_frequency
     WHERE billing_frequency_code = NEW.billing_frequency_code;
    IF v_months IS NULL THEN
        IF NEW.arr_treatment_code IS DISTINCT FROM 'none' AND NEW.arr_treatment_code IS NOT NULL THEN
            RAISE EXCEPTION 'line %: a one-off line cannot be ARR', NEW.line_number USING ERRCODE = 'check_violation';
        END IF;
        NEW.arr_treatment_code := 'none';
    ELSE
        NEW.arr_treatment_code := COALESCE(NEW.arr_treatment_code, 'arr');
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER line_defaults_and_gl
    BEFORE INSERT OR UPDATE ON sales.sales_order_line
    FOR EACH ROW EXECUTE FUNCTION sales.tg_line_defaults_and_gl();

-- ---------------------------------------------------------------- ARR database
CREATE TABLE sales.arr_contract_status (
    status_code text PRIMARY KEY,
    description text NOT NULL
);
INSERT INTO sales.arr_contract_status VALUES
    ('active', 'Live and billing'), ('notice_given', 'Customer has given notice'),
    ('ended', 'Ended at term'), ('cancelled', 'Cancelled early');

CREATE TABLE sales.arr_contract (
    arr_contract_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    arr_ref                text   NOT NULL UNIQUE CHECK (arr_ref ~ '^[A-Z]{3}([0-9]{2,3})?(-[0-9]{2})?$'),
    customer_id            bigint NOT NULL REFERENCES sales.customer,
    description            text   NOT NULL,
    service_category_code  text   REFERENCES sales.service_category,
    status_code            text   NOT NULL DEFAULT 'active' REFERENCES sales.arr_contract_status,
    start_date             date   NOT NULL,
    end_date               date,                   -- current term end (co-term / anniversary date)
    auto_renews            boolean NOT NULL DEFAULT true,
    notice_period_days     integer CHECK (notice_period_days >= 0),
    source                 text   NOT NULL DEFAULT 'ARR Live.xlsx',
    created_at             timestamptz NOT NULL DEFAULT now(),
    created_by             text        NOT NULL DEFAULT audit.current_actor(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    updated_by             text        NOT NULL DEFAULT audit.current_actor(),
    row_version            integer     NOT NULL DEFAULT 1,
    CHECK (end_date IS NULL OR end_date >= start_date)
);
COMMENT ON TABLE sales.arr_contract IS 'One row per ARR reference (customer contract/service). ARR refs are assigned by the ARR file.';
SELECT sales.attach_standard_triggers('sales.arr_contract');

ALTER TABLE sales.sales_order_line
    ADD CONSTRAINT sales_order_line_arr_contract_fk FOREIGN KEY (arr_contract_id) REFERENCES sales.arr_contract;
CREATE INDEX sales_order_line_arr_contract_idx ON sales.sales_order_line (arr_contract_id);

CREATE TABLE sales.arr_movement_type (
    movement_type_code text PRIMARY KEY,
    description        text NOT NULL,
    sign               smallint NOT NULL CHECK (sign IN (-1, 0, 1))  -- expected direction of the MRR change
);
INSERT INTO sales.arr_movement_type VALUES
    ('new',          'New contract',                         1),
    ('expansion',    'Volume / add-on increase',             1),
    ('contraction',  'Volume reduction',                    -1),
    ('price_change', 'Price change (e.g. RPI uplift)',       0),
    ('renewal',      'Renewal re-price (delta vs previous)', 0),
    ('churn',        'Contract lost',                       -1),
    ('opening',      'Opening balance migrated from the ARR file', 1);

-- ARR is a ledger: every change is a dated movement, never an overwritten figure.
CREATE TABLE sales.arr_movement (
    arr_movement_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    arr_contract_id     bigint NOT NULL REFERENCES sales.arr_contract,
    effective_date      date   NOT NULL,
    movement_type_code  text   NOT NULL REFERENCES sales.arr_movement_type,
    mrr_delta           numeric(18,2) NOT NULL,   -- change in monthly recurring revenue (sell)
    mrr_cost_delta      numeric(18,2) NOT NULL DEFAULT 0,
    sales_order_line_id bigint UNIQUE REFERENCES sales.sales_order_line,  -- one movement per order line
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text        NOT NULL DEFAULT audit.current_actor(),
    CHECK (mrr_delta <> 0 OR mrr_cost_delta <> 0 OR movement_type_code IN ('price_change', 'renewal'))
);
CREATE INDEX arr_movement_contract_date_idx ON sales.arr_movement (arr_contract_id, effective_date);
CREATE TRIGGER audit_row AFTER INSERT OR UPDATE OR DELETE ON sales.arr_movement
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();
COMMENT ON TABLE sales.arr_movement IS 'Append-only. Correct a movement by posting a reversing movement.';

CREATE FUNCTION audit.tg_block_modification_named() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; post a reversing entry instead', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END
$$;
CREATE TRIGGER arr_movement_append_only BEFORE UPDATE OR DELETE ON sales.arr_movement
    FOR EACH ROW EXECUTE FUNCTION audit.tg_block_modification_named();

-- Order category -> ARR movement type used when an approved order is posted.
CREATE TABLE sales.order_type_arr_movement (
    order_type_code    text PRIMARY KEY REFERENCES sales.order_type,
    movement_type_code text NOT NULL REFERENCES sales.arr_movement_type
);
INSERT INTO sales.order_type_arr_movement VALUES
    ('NEW_ORDER', 'new'), ('VOLUME', 'expansion'), ('RENEWAL', 'renewal'), ('CHURN', 'churn');
-- LAST_ORDER rows are excluded from bookings/ARR (Register rule).

-- Posting: on approval, each ARR-bearing line creates one movement on its contract.
-- MRR = quantity x unit price / months per billing period. Churn lines post negative.
CREATE FUNCTION sales.post_arr_movements(p_sales_order_id bigint) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_count integer;
BEGIN
    INSERT INTO sales.arr_movement (arr_contract_id, effective_date, movement_type_code, mrr_delta,
                                    mrr_cost_delta, sales_order_line_id, notes)
    SELECT l.arr_contract_id,
           COALESCE(l.service_start_date, o.signed_date, o.received_at::date),
           m.movement_type_code,
           round(l.quantity * l.unit_sell / bf.months_per_period, 2) * CASE WHEN m.movement_type_code = 'churn' THEN -1 ELSE 1 END,
           round(l.quantity * l.unit_cost / bf.months_per_period, 2) * CASE WHEN m.movement_type_code = 'churn' THEN -1 ELSE 1 END,
           l.sales_order_line_id,
           format('Posted from %s line %s', COALESCE(o.sn_ref, o.order_number), l.line_number)
      FROM sales.sales_order_line l
      JOIN sales.sales_order o USING (sales_order_id)
      JOIN sales.billing_frequency bf USING (billing_frequency_code)
      JOIN sales.order_type_arr_movement m ON m.order_type_code = o.order_type_code
     WHERE l.sales_order_id = p_sales_order_id
       AND l.arr_treatment_code = 'arr'
       AND l.arr_contract_id IS NOT NULL
    ON CONFLICT (sales_order_line_id) DO NOTHING;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END
$$;

CREATE FUNCTION sales.tg_sales_order_post_arr() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sales.post_arr_movements(NEW.sales_order_id);
    RETURN NEW;
END
$$;
CREATE TRIGGER post_arr_on_approval
    AFTER UPDATE OF status_code ON sales.sales_order
    FOR EACH ROW WHEN (NEW.status_code = 'approved' AND OLD.status_code IS DISTINCT FROM 'approved')
    EXECUTE FUNCTION sales.tg_sales_order_post_arr();

-- ARR position of every contract on any date: sum of movements up to that date.
CREATE FUNCTION sales.arr_at(p_as_of date)
RETURNS TABLE (arr_contract_id bigint, arr_ref text, customer_id bigint, mrr numeric, arr numeric,
               mrr_cost numeric, arr_margin numeric)
LANGUAGE sql STABLE AS $$
    SELECT c.arr_contract_id, c.arr_ref, c.customer_id,
           sum(m.mrr_delta), sum(m.mrr_delta) * 12,
           sum(m.mrr_cost_delta), (sum(m.mrr_delta) - sum(m.mrr_cost_delta)) * 12
      FROM sales.arr_contract c
      JOIN sales.arr_movement m USING (arr_contract_id)
     WHERE m.effective_date <= p_as_of
     GROUP BY c.arr_contract_id
$$;

CREATE VIEW sales.v_arr_current AS
SELECT a.*, c.status_code, c.end_date, cu.legal_name AS customer_name
  FROM sales.arr_at(current_date) a
  JOIN sales.arr_contract c USING (arr_contract_id)
  JOIN sales.customer cu ON cu.customer_id = a.customer_id;

-- ARR bridge between two dates by movement type (opening + movements = closing).
CREATE FUNCTION sales.arr_bridge(p_from date, p_to date)
RETURNS TABLE (movement_type_code text, arr_change numeric)
LANGUAGE sql STABLE AS $$
    SELECT m.movement_type_code, sum(m.mrr_delta) * 12
      FROM sales.arr_movement m
     WHERE m.effective_date > p_from AND m.effective_date <= p_to
     GROUP BY m.movement_type_code
$$;

-- ---------------------------------------------------------------- exception rules (added)
-- New rules live in their own view and are unioned into the main one, so earlier rules
-- are not re-declared each time a rule is added.
CREATE VIEW sales.v_sales_order_exception_0004 AS
SELECT o.sales_order_id, o.order_number, 'SN_NOT_ASSIGNED' AS rule_code, 'error' AS severity,
       'No SN reference yet: source it from the AW SOs Register before approval' AS message
  FROM sales.sales_order o
 WHERE o.sn_ref IS NULL AND o.status_code NOT IN ('cancelled')
UNION ALL
SELECT l.sales_order_id, o.order_number, 'NO_GL_CODE', 'error',
       format('Line %s (%s) has no %s GL code', l.line_number, l.description,
              concat_ws(' or ', CASE WHEN l.revenue_gl_code IS NULL THEN 'revenue' END,
                                CASE WHEN l.cost_gl_code IS NULL THEN 'cost' END))
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.revenue_gl_code IS NULL OR l.cost_gl_code IS NULL
UNION ALL
SELECT l.sales_order_id, o.order_number, 'NO_SERVICE_CATEGORY', 'error',
       format('Line %s (%s) has no service category', l.line_number, l.description)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.service_category_code IS NULL
UNION ALL
SELECT l.sales_order_id, o.order_number, 'ARR_LINE_NO_ARR_REF', 'error',
       format('Recurring line %s (%s) has no ARR reference', l.line_number, l.description)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.arr_treatment_code = 'arr' AND l.arr_contract_id IS NULL
   AND o.order_type_code <> 'LAST_ORDER'
UNION ALL
SELECT l.sales_order_id, o.order_number, 'ARR_CONTRACT_OTHER_CUSTOMER', 'error',
       format('Line %s: ARR ref %s belongs to a different customer', l.line_number, c.arr_ref)
  FROM sales.sales_order_line l
  JOIN sales.sales_order o USING (sales_order_id)
  JOIN sales.arr_contract c USING (arr_contract_id)
 WHERE c.customer_id <> o.customer_id
UNION ALL
SELECT l.sales_order_id, o.order_number, 'RECURRING_LINE_NO_DATES', 'warning',
       format('Recurring line %s (%s) has no service start/end date', l.line_number, l.description)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.arr_treatment_code IN ('arr', 'stub') AND (l.service_start_date IS NULL OR l.service_end_date IS NULL);

CREATE OR REPLACE VIEW sales.v_sales_order_exception_all AS
SELECT * FROM sales.v_sales_order_exception
UNION ALL
SELECT * FROM sales.v_sales_order_exception_0004;
COMMENT ON VIEW sales.v_sales_order_exception_all IS
    'All exception rules. Use this view; the approval gate reads it.';

-- Approval gate now reads the complete rule set.
CREATE OR REPLACE FUNCTION sales.tg_sales_order_status() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status_code <> 'received' THEN
            RAISE EXCEPTION 'new orders must start in status received, not %', NEW.status_code
                USING ERRCODE = 'check_violation';
        END IF;
        INSERT INTO sales.sales_order_status_history (sales_order_id, from_status, to_status, reason)
        VALUES (NEW.sales_order_id, NULL, NEW.status_code, 'created');
        RETURN NEW;
    END IF;

    IF NEW.status_code IS DISTINCT FROM OLD.status_code THEN
        IF NOT EXISTS (SELECT 1 FROM sales.order_status_transition
                        WHERE from_status = OLD.status_code AND to_status = NEW.status_code) THEN
            RAISE EXCEPTION 'order %: status change % -> % is not allowed',
                OLD.order_number, OLD.status_code, NEW.status_code USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.status_code = 'approved' THEN
            PERFORM sales.require_permission('approve_order', format('approving order %s', NEW.order_number));
            IF EXISTS (SELECT 1 FROM sales.v_sales_order_exception_all e
                        WHERE e.sales_order_id = NEW.sales_order_id AND e.severity = 'error') THEN
                RAISE EXCEPTION 'order % cannot be approved: it has error-level exceptions (see sales.v_sales_order_exception_all)',
                    NEW.order_number USING ERRCODE = 'check_violation';
            END IF;
            IF EXISTS (SELECT 1 FROM sales.sales_order_check c
                         JOIN sales.check_status cs USING (check_status_code)
                        WHERE c.sales_order_id = NEW.sales_order_id AND NOT cs.is_resolved) THEN
                RAISE EXCEPTION 'order % cannot be approved: pre-processing checks are unresolved',
                    NEW.order_number USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        INSERT INTO sales.sales_order_status_history (sales_order_id, from_status, to_status, reason)
        VALUES (NEW.sales_order_id, OLD.status_code, NEW.status_code,
                NULLIF(current_setting('app.status_reason', true), ''));
    END IF;
    RETURN NEW;
END
$$;

-- SN reference and ARR/GL/reporting fields freeze with the other commercial fields.
CREATE OR REPLACE FUNCTION sales.tg_sales_order_lock() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT lines_locked FROM sales.order_status WHERE status_code = OLD.status_code)
       AND (NEW.customer_id, NEW.currency_code, NEW.order_type_code, NEW.price_list_id,
            NEW.margin_exception_reason, NEW.sn_ref, NEW.reporting_category_code)
           IS DISTINCT FROM
           (OLD.customer_id, OLD.currency_code, OLD.order_type_code, OLD.price_list_id,
            OLD.margin_exception_reason, OLD.sn_ref, OLD.reporting_category_code)
    THEN
        RAISE EXCEPTION 'order %: commercial fields are locked in status %', OLD.order_number, OLD.status_code
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

-- ---------------------------------------------------------------- AW SOs Register mirror & SN sourcing
-- The Register is the master of SN references. This is a read-only mirror, replaced on every sync;
-- the database never writes to AW SOs.
-- Customer-name matching key: case, punctuation, "&"/"and" and legal suffixes ignored.
CREATE FUNCTION sales.normalised_name(p text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT regexp_replace(
             regexp_replace(
               regexp_replace(lower(COALESCE(p, '')), '&', ' and ', 'g'),
               '\m(ltd|limited|plc|llp|inc|group|holdings?|uk)\M', ' ', 'g'),
             '[^a-z0-9]+', '', 'g')
$$;

CREATE TABLE sales.register_sync (
    register_sync_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source           text        NOT NULL,     -- e.g. Google Sheet id / export name
    synced_at        timestamptz NOT NULL DEFAULT now(),
    synced_by        text        NOT NULL DEFAULT audit.current_actor(),
    rows_loaded      integer     NOT NULL CHECK (rows_loaded >= 0),
    rows_rejected    integer     NOT NULL CHECK (rows_rejected >= 0)
);
CREATE TRIGGER audit_row AFTER INSERT ON sales.register_sync
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();

CREATE TABLE sales.register_sync_rejection (
    register_sync_id bigint NOT NULL REFERENCES sales.register_sync,
    row_number       integer NOT NULL,
    raw_sn           text,
    reason           text NOT NULL,
    PRIMARY KEY (register_sync_id, row_number)
);

CREATE TABLE sales.register_entry (
    sn_ref              text PRIMARY KEY CHECK (sn_ref ~ '^SN([0-9]{4}|[0-9]{6})(LO|CA)?$'),
    register_sync_id    bigint NOT NULL REFERENCES sales.register_sync,
    date_issued         date,
    document_type_raw   text,    -- Register "Type": Quotation, Proposal, ...
    client              text NOT NULL,
    new_logo            text,
    project             text,
    customer_po         text,
    ticket_ref          text,
    document_date       date,
    signed_by_managed247 text,
    signed_by_customer  text,
    order_category_raw  text,    -- Register "Document Type": New Order, Volume, ...
    reporting_category_raw text,
    salesperson         text,
    revenue             numeric(18,2),
    expected_costs      numeric(18,2)
);
CREATE INDEX register_entry_client_idx ON sales.register_entry (sales.normalised_name(client));

-- Candidate SNs for orders that do not have one yet: same customer (normalised name, trading
-- name or Xero tracking name), issued within 7 days of the signed/received date, SN not used
-- by another order. Score: 3 = project matches title, 2 = same day, 1 = within window.
CREATE VIEW sales.v_sn_candidate AS
SELECT o.sales_order_id, o.order_number, r.sn_ref, r.client, r.project, r.date_issued, r.revenue,
       s.net_sell AS order_net_sell,
       (CASE WHEN r.project IS NOT NULL AND (o.title ILIKE '%' || r.project || '%' OR r.project ILIKE '%' || o.title || '%')
             THEN 3 ELSE 0 END)
       + (CASE WHEN r.date_issued = COALESCE(o.signed_date, o.received_at::date) THEN 2 ELSE 1 END)
       + (CASE WHEN abs(COALESCE(r.revenue, 0) - s.net_sell) <= 1 THEN 3 ELSE 0 END) AS score
  FROM sales.sales_order o
  JOIN sales.customer c USING (customer_id)
  JOIN sales.v_sales_order_summary s USING (sales_order_id)
  JOIN sales.register_entry r
    ON sales.normalised_name(r.client) IN (sales.normalised_name(c.legal_name),
                                           sales.normalised_name(c.trading_name),
                                           sales.normalised_name(c.xero_tracking_customer))
   AND abs(r.date_issued - COALESCE(o.signed_date, o.received_at::date)) <= 7
 WHERE o.sn_ref IS NULL
   AND r.sn_ref !~ '(LO|CA)$'
   AND NOT EXISTS (SELECT 1 FROM sales.sales_order x WHERE x.sn_ref = r.sn_ref);

-- Assigns the SN when there is exactly one best candidate; otherwise returns NULL and the
-- order stays on the SN_NOT_ASSIGNED exception for a person to resolve. Never invents an SN.
CREATE FUNCTION sales.assign_sn_from_register(p_sales_order_id bigint) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    v_best  integer;
    v_count integer;
    v_sn    text;
BEGIN
    SELECT max(score) INTO v_best FROM sales.v_sn_candidate WHERE sales_order_id = p_sales_order_id;
    IF v_best IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT count(*), min(sn_ref) INTO v_count, v_sn
      FROM sales.v_sn_candidate WHERE sales_order_id = p_sales_order_id AND score = v_best;
    IF v_count <> 1 THEN
        RETURN NULL;
    END IF;
    UPDATE sales.sales_order
       SET sn_ref = v_sn, sn_source = format('AW SOs Register (auto-match, score %s)', v_best),
           sn_assigned_at = now()
     WHERE sales_order_id = p_sales_order_id;
    RETURN v_sn;
END
$$;

-- A manually chosen SN must exist on the Register and belong to the same customer.
CREATE FUNCTION sales.tg_sn_must_be_on_register() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.sn_ref IS NOT NULL AND NEW.sn_ref IS DISTINCT FROM OLD.sn_ref THEN
        IF NOT EXISTS (
            SELECT 1 FROM sales.register_entry r JOIN sales.customer c ON c.customer_id = NEW.customer_id
             WHERE r.sn_ref = NEW.sn_ref
               AND sales.normalised_name(r.client) IN (sales.normalised_name(c.legal_name),
                                                       sales.normalised_name(c.trading_name),
                                                       sales.normalised_name(c.xero_tracking_customer))) THEN
            RAISE EXCEPTION 'SN % is not on the AW SOs Register for this customer', NEW.sn_ref
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER sn_must_be_on_register
    BEFORE INSERT OR UPDATE OF sn_ref ON sales.sales_order
    FOR EACH ROW EXECUTE FUNCTION sales.tg_sn_must_be_on_register();
