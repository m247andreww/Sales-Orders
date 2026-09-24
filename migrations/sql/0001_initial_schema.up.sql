-- =============================================================================
-- 0001 Initial schema: Managed247 sales order system of record
--
-- Conventions (see docs/data-model.md):
--   * Schemas: sales (business data), audit (change log). Nothing in public.
--   * Surrogate keys: bigint identity. Lookup tables use readable text codes.
--   * Money: numeric, never float. Unit prices numeric(18,4); totals numeric(18,2).
--   * Derived money (net cost / sell / margin) is GENERATED, never typed in.
--   * Every business table carries created_at / updated_at / row_version and
--     is covered by the audit trigger.
--   * Business rules that can be enforced by the database ARE enforced by it.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS btree_gist;  -- for non-overlapping date ranges

CREATE SCHEMA sales;
CREATE SCHEMA audit;

COMMENT ON SCHEMA sales IS 'Managed247 sales orders: customers, suppliers, orders, lines, evidence.';
COMMENT ON SCHEMA audit IS 'Append-only change log for every table in the sales schema.';

-- -----------------------------------------------------------------------------
-- Shared trigger functions
-- -----------------------------------------------------------------------------

-- Who is making the change. The application sets app.current_user per transaction
-- (SET LOCAL). Falls back to the database login so direct SQL is still attributed.
CREATE FUNCTION audit.current_actor() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(NULLIF(current_setting('app.current_user', true), ''), session_user::text)
$$;

CREATE FUNCTION sales.tg_touch_row() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at  := now();
    NEW.updated_by  := audit.current_actor();
    NEW.row_version := OLD.row_version + 1;
    RETURN NEW;
END
$$;

CREATE TABLE audit.change_log (
    change_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    table_name     text        NOT NULL,
    action         text        NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    row_data       jsonb       NOT NULL,           -- NEW row (or OLD row for DELETE)
    changed_fields jsonb,                          -- UPDATE only: {column: [old, new]}
    changed_at     timestamptz NOT NULL DEFAULT now(),
    changed_by     text        NOT NULL DEFAULT audit.current_actor(),
    transaction_id xid8        NOT NULL DEFAULT pg_current_xact_id()
);
CREATE INDEX change_log_table_time_idx ON audit.change_log (table_name, changed_at);
COMMENT ON TABLE audit.change_log IS 'Append-only. Written only by audit.tg_log_change(); never updated or deleted.';

CREATE FUNCTION audit.tg_log_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, audit AS $$
DECLARE
    diff jsonb;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        SELECT jsonb_object_agg(n.key, jsonb_build_array(o.value, n.value))
          INTO diff
          FROM jsonb_each(to_jsonb(NEW)) n
          JOIN jsonb_each(to_jsonb(OLD)) o USING (key)
         WHERE n.value IS DISTINCT FROM o.value;
        INSERT INTO audit.change_log (table_name, action, row_data, changed_fields)
        VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, TG_OP, to_jsonb(NEW), diff);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO audit.change_log (table_name, action, row_data)
        VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, TG_OP, to_jsonb(OLD));
        RETURN OLD;
    END IF;
    INSERT INTO audit.change_log (table_name, action, row_data)
    VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, TG_OP, to_jsonb(NEW));
    RETURN NEW;
END
$$;

CREATE FUNCTION audit.tg_block_modification() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.change_log is append-only'
        USING ERRCODE = 'insufficient_privilege';
END
$$;
CREATE TRIGGER change_log_append_only
    BEFORE UPDATE OR DELETE ON audit.change_log
    FOR EACH ROW EXECUTE FUNCTION audit.tg_block_modification();

-- Attaches the standard touch + audit triggers to a table. Used for every
-- business table so none can be forgotten.
CREATE FUNCTION sales.attach_standard_triggers(p_table regclass) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format('CREATE TRIGGER touch_row BEFORE UPDATE ON %s
                    FOR EACH ROW EXECUTE FUNCTION sales.tg_touch_row()', p_table);
    EXECUTE format('CREATE TRIGGER audit_row AFTER INSERT OR UPDATE OR DELETE ON %s
                    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change()', p_table);
END
$$;

-- -----------------------------------------------------------------------------
-- Reference (lookup) tables. Seeded in 0001_reference_data section below.
-- -----------------------------------------------------------------------------

CREATE TABLE sales.currency (
    currency_code char(3)  PRIMARY KEY CHECK (currency_code ~ '^[A-Z]{3}$'),
    name          text     NOT NULL,
    minor_units   smallint NOT NULL DEFAULT 2 CHECK (minor_units BETWEEN 0 AND 4)
);

CREATE TABLE sales.order_status (
    status_code text     PRIMARY KEY,
    description text     NOT NULL,
    is_terminal boolean  NOT NULL DEFAULT false,
    lines_locked boolean NOT NULL DEFAULT false,  -- commercial content frozen in this status
    sort_order  smallint NOT NULL UNIQUE
);

CREATE TABLE sales.order_status_transition (
    from_status text NOT NULL REFERENCES sales.order_status,
    to_status   text NOT NULL REFERENCES sales.order_status,
    PRIMARY KEY (from_status, to_status),
    CHECK (from_status <> to_status)
);

CREATE TABLE sales.order_type (
    order_type_code text PRIMARY KEY,
    description     text NOT NULL
);

CREATE TABLE sales.line_category (
    line_category_code  text    PRIMARY KEY,
    description         text    NOT NULL,
    is_tax_pass_through boolean NOT NULL DEFAULT false
);

CREATE TABLE sales.billing_frequency (
    billing_frequency_code text     PRIMARY KEY,
    description            text     NOT NULL,
    months_per_period      smallint CHECK (months_per_period > 0),  -- NULL = one-off
    sort_order             smallint NOT NULL UNIQUE
);

CREATE TABLE sales.payment_method (
    payment_method_code text PRIMARY KEY,
    description         text NOT NULL
);

CREATE TABLE sales.risk_rating (
    risk_rating_code text     PRIMARY KEY,
    description      text     NOT NULL,
    severity         smallint NOT NULL UNIQUE
);

CREATE TABLE sales.document_type (
    document_type_code text PRIMARY KEY,
    description        text NOT NULL
);

CREATE TABLE sales.check_type (
    check_type_code text PRIMARY KEY,
    description     text NOT NULL
);

CREATE TABLE sales.check_status (
    check_status_code text    PRIMARY KEY,
    description       text    NOT NULL,
    is_resolved       boolean NOT NULL
);

CREATE TABLE sales.supplier_account_status (
    status_code text    PRIMARY KEY,
    description text    NOT NULL,
    can_order   boolean NOT NULL
);

-- Tunable policy values (margin thresholds etc.) live in data, not code.
CREATE TABLE sales.policy_setting (
    setting_key   text    PRIMARY KEY,
    numeric_value numeric NOT NULL,
    description   text    NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text        NOT NULL DEFAULT audit.current_actor(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text        NOT NULL DEFAULT audit.current_actor(),
    row_version   integer     NOT NULL DEFAULT 1
);
SELECT sales.attach_standard_triggers('sales.policy_setting');

-- -----------------------------------------------------------------------------
-- People and organisations
-- -----------------------------------------------------------------------------

CREATE TABLE sales.employee (
    employee_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email       text    NOT NULL CHECK (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    full_name   text    NOT NULL CHECK (btrim(full_name) <> ''),
    job_title   text,
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text        NOT NULL DEFAULT audit.current_actor(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text        NOT NULL DEFAULT audit.current_actor(),
    row_version integer     NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX employee_email_uq ON sales.employee (lower(email));
SELECT sales.attach_standard_triggers('sales.employee');

CREATE TABLE sales.customer (
    customer_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    legal_name      text NOT NULL CHECK (btrim(legal_name) <> ''),
    trading_name    text,
    company_number  text CHECK (company_number ~ '^[A-Z0-9]{8}$'),   -- Companies House
    xero_contact_id uuid UNIQUE,
    is_active       boolean NOT NULL DEFAULT true,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text        NOT NULL DEFAULT audit.current_actor(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text        NOT NULL DEFAULT audit.current_actor(),
    row_version     integer     NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX customer_legal_name_uq ON sales.customer (lower(legal_name));
CREATE UNIQUE INDEX customer_company_number_uq ON sales.customer (company_number)
    WHERE company_number IS NOT NULL;
SELECT sales.attach_standard_triggers('sales.customer');

CREATE TABLE sales.customer_contact (
    customer_contact_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id         bigint NOT NULL REFERENCES sales.customer,
    full_name           text,
    email               text NOT NULL CHECK (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    role_description    text,   -- e.g. 'M365 admin', 'Procurement', 'Accounts payable'
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text        NOT NULL DEFAULT audit.current_actor(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          text        NOT NULL DEFAULT audit.current_actor(),
    row_version         integer     NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX customer_contact_email_uq ON sales.customer_contact (customer_id, lower(email));
SELECT sales.attach_standard_triggers('sales.customer_contact');

-- Effective-dated credit terms: the single register of standard AND
-- non-standard terms per customer. Overlapping periods are impossible.
CREATE TABLE sales.customer_credit_terms (
    customer_credit_terms_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id                  bigint  NOT NULL REFERENCES sales.customer,
    effective_from               date    NOT NULL,
    effective_to                 date,                        -- NULL = open-ended
    recurring_terms_days         integer NOT NULL CHECK (recurring_terms_days BETWEEN 0 AND 180),
    recurring_payment_method_code text   NOT NULL REFERENCES sales.payment_method,
    one_off_terms_days           integer NOT NULL CHECK (one_off_terms_days BETWEEN 0 AND 180),
    one_off_prepayment_required  boolean NOT NULL DEFAULT false,
    credit_limit                 numeric(18,2) CHECK (credit_limit >= 0),
    risk_rating_code             text    NOT NULL REFERENCES sales.risk_rating,
    is_non_standard              boolean NOT NULL DEFAULT false,
    reason                       text,
    approved_by_employee_id      bigint  REFERENCES sales.employee,
    approved_at                  timestamptz,
    created_at                   timestamptz NOT NULL DEFAULT now(),
    created_by                   text        NOT NULL DEFAULT audit.current_actor(),
    updated_at                   timestamptz NOT NULL DEFAULT now(),
    updated_by                   text        NOT NULL DEFAULT audit.current_actor(),
    row_version                  integer     NOT NULL DEFAULT 1,
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    -- Non-standard terms must say why and who approved them.
    CHECK (NOT is_non_standard
           OR (reason IS NOT NULL AND approved_by_employee_id IS NOT NULL AND approved_at IS NOT NULL)),
    CONSTRAINT customer_credit_terms_no_overlap EXCLUDE USING gist (
        customer_id WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
);
COMMENT ON TABLE sales.customer_credit_terms IS
    'Register of credit terms per customer over time, including non-standard terms and risk rating.';
SELECT sales.attach_standard_triggers('sales.customer_credit_terms');

-- Microsoft 365 tenants (CSP orders reference these).
CREATE TABLE sales.customer_m365_tenant (
    customer_m365_tenant_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id             bigint NOT NULL REFERENCES sales.customer,
    tenant_guid             uuid   NOT NULL UNIQUE,
    primary_domain          text   NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    created_by              text        NOT NULL DEFAULT audit.current_actor(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    updated_by              text        NOT NULL DEFAULT audit.current_actor(),
    row_version             integer     NOT NULL DEFAULT 1
);
SELECT sales.attach_standard_triggers('sales.customer_m365_tenant');

CREATE TABLE sales.supplier (
    supplier_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                 text    NOT NULL CHECK (btrim(name) <> ''),
    is_internal          boolean NOT NULL DEFAULT false,  -- Managed247 own delivery (PS days)
    account_status_code  text    NOT NULL REFERENCES sales.supplier_account_status,
    payment_terms_days   integer CHECK (payment_terms_days BETWEEN 0 AND 180),
    default_currency_code char(3) NOT NULL DEFAULT 'GBP' REFERENCES sales.currency,
    xero_contact_id      uuid UNIQUE,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text        NOT NULL DEFAULT audit.current_actor(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    updated_by           text        NOT NULL DEFAULT audit.current_actor(),
    row_version          integer     NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX supplier_name_uq ON sales.supplier (lower(name));
CREATE UNIQUE INDEX supplier_single_internal_uq ON sales.supplier (is_internal) WHERE is_internal;
SELECT sales.attach_standard_triggers('sales.supplier');

-- -----------------------------------------------------------------------------
-- Catalogue
-- -----------------------------------------------------------------------------

CREATE TABLE sales.product (
    product_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku                 text   NOT NULL CHECK (btrim(sku) <> ''),
    name                text   NOT NULL,
    line_category_code  text   NOT NULL REFERENCES sales.line_category,
    default_supplier_id bigint REFERENCES sales.supplier,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text        NOT NULL DEFAULT audit.current_actor(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          text        NOT NULL DEFAULT audit.current_actor(),
    row_version         integer     NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX product_sku_uq ON sales.product (upper(sku));
SELECT sales.attach_standard_triggers('sales.product');

-- e.g. 'Managed Service Catalogue 2026'
CREATE TABLE sales.price_list (
    price_list_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text NOT NULL UNIQUE,
    currency_code char(3) NOT NULL DEFAULT 'GBP' REFERENCES sales.currency,
    valid_from    date NOT NULL,
    valid_to      date,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text        NOT NULL DEFAULT audit.current_actor(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text        NOT NULL DEFAULT audit.current_actor(),
    row_version   integer     NOT NULL DEFAULT 1,
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
SELECT sales.attach_standard_triggers('sales.price_list');

CREATE TABLE sales.price_list_item (
    price_list_item_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    price_list_id          bigint NOT NULL REFERENCES sales.price_list,
    product_id             bigint NOT NULL REFERENCES sales.product,
    billing_frequency_code text   NOT NULL REFERENCES sales.billing_frequency,
    unit_cost              numeric(18,4) NOT NULL CHECK (unit_cost >= 0),
    unit_sell              numeric(18,4) NOT NULL CHECK (unit_sell >= 0),
    created_at             timestamptz NOT NULL DEFAULT now(),
    created_by             text        NOT NULL DEFAULT audit.current_actor(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    updated_by             text        NOT NULL DEFAULT audit.current_actor(),
    row_version            integer     NOT NULL DEFAULT 1,
    UNIQUE (price_list_id, product_id, billing_frequency_code)
);
SELECT sales.attach_standard_triggers('sales.price_list_item');

-- -----------------------------------------------------------------------------
-- Evidence: source emails, documents, supplier quotes, FX rates
-- -----------------------------------------------------------------------------

CREATE TABLE sales.source_email (
    source_email_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mailbox             text NOT NULL,                 -- e.g. neworders@managed.co.uk
    internet_message_id text NOT NULL UNIQUE,          -- RFC 5322 Message-ID: idempotency key
    graph_message_id    text,
    conversation_id     text,
    subject             text NOT NULL,
    sender_email        text NOT NULL,
    received_at         timestamptz NOT NULL,
    body_text           text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text        NOT NULL DEFAULT audit.current_actor(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          text        NOT NULL DEFAULT audit.current_actor(),
    row_version         integer     NOT NULL DEFAULT 1
);
SELECT sales.attach_standard_triggers('sales.source_email');

CREATE TABLE sales.document (
    document_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_type_code   text   NOT NULL REFERENCES sales.document_type,
    file_name            text   NOT NULL,
    content_type         text   NOT NULL,
    size_bytes           bigint NOT NULL CHECK (size_bytes >= 0),
    sha256               char(64) NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    storage_uri          text   NOT NULL,   -- SharePoint/OneDrive location; the file is not stored in the DB
    source_email_id      bigint REFERENCES sales.source_email,
    pandadoc_document_id text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text        NOT NULL DEFAULT audit.current_actor(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    updated_by           text        NOT NULL DEFAULT audit.current_actor(),
    row_version          integer     NOT NULL DEFAULT 1
);
SELECT sales.attach_standard_triggers('sales.document');

CREATE TABLE sales.supplier_quote (
    supplier_quote_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    supplier_id        bigint NOT NULL REFERENCES sales.supplier,
    supplier_reference text   NOT NULL,
    quote_date         date,
    valid_until        date,
    currency_code      char(3) NOT NULL REFERENCES sales.currency,
    document_id        bigint REFERENCES sales.document,
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         text        NOT NULL DEFAULT audit.current_actor(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    updated_by         text        NOT NULL DEFAULT audit.current_actor(),
    row_version        integer     NOT NULL DEFAULT 1,
    UNIQUE (supplier_id, supplier_reference),
    CHECK (valid_until IS NULL OR quote_date IS NULL OR valid_until >= quote_date)
);
SELECT sales.attach_standard_triggers('sales.supplier_quote');

CREATE TABLE sales.fx_rate (
    fx_rate_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_currency_code   char(3) NOT NULL REFERENCES sales.currency,
    to_currency_code     char(3) NOT NULL REFERENCES sales.currency,
    rate_date            date    NOT NULL,
    rate                 numeric(18,8) NOT NULL CHECK (rate > 0),  -- 1 unit FROM = rate units TO
    source               text    NOT NULL,                         -- e.g. 'XE mid-market'
    evidence_document_id bigint  REFERENCES sales.document,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text        NOT NULL DEFAULT audit.current_actor(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    updated_by           text        NOT NULL DEFAULT audit.current_actor(),
    row_version          integer     NOT NULL DEFAULT 1,
    CHECK (from_currency_code <> to_currency_code),
    UNIQUE (from_currency_code, to_currency_code, rate_date, source)
);
SELECT sales.attach_standard_triggers('sales.fx_rate');

-- -----------------------------------------------------------------------------
-- Sales orders
-- -----------------------------------------------------------------------------

CREATE TABLE sales.sales_order (
    sales_order_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_number                text GENERATED ALWAYS AS ('SO-' || lpad(sales_order_id::text, 6, '0')) STORED UNIQUE,
    customer_id                 bigint NOT NULL REFERENCES sales.customer,
    title                       text   NOT NULL CHECK (btrim(title) <> ''),
    order_type_code             text   NOT NULL REFERENCES sales.order_type,
    status_code                 text   NOT NULL DEFAULT 'received' REFERENCES sales.order_status,
    currency_code               char(3) NOT NULL DEFAULT 'GBP' REFERENCES sales.currency,
    quote_reference             text,           -- customer-facing quote / proposal name
    pandadoc_document_id        text,
    customer_po_reference       text,
    price_list_id               bigint REFERENCES sales.price_list,
    customer_m365_tenant_id     bigint REFERENCES sales.customer_m365_tenant,
    signed_date                 date,
    received_at                 timestamptz NOT NULL,
    source_email_id             bigint REFERENCES sales.source_email,
    source_sequence             smallint NOT NULL DEFAULT 1 CHECK (source_sequence > 0),
    submitted_by_employee_id    bigint NOT NULL REFERENCES sales.employee,  -- who raised it into New Orders
    account_manager_employee_id bigint REFERENCES sales.employee,           -- commercial owner
    is_expedited                boolean NOT NULL DEFAULT false,
    margin_exception_reason     text,           -- required to accept a margin below policy
    -- Totals as typed in the submission email, kept only to reconcile against the computed totals.
    stated_net_cost             numeric(18,2),
    stated_net_sell             numeric(18,2),
    stated_gross_margin         numeric(18,2),
    notes                       text,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    created_by                  text        NOT NULL DEFAULT audit.current_actor(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    updated_by                  text        NOT NULL DEFAULT audit.current_actor(),
    row_version                 integer     NOT NULL DEFAULT 1,
    -- One email can carry several orders; each (email, sequence) is ingested once.
    UNIQUE (source_email_id, source_sequence)
);
CREATE INDEX sales_order_customer_idx ON sales.sales_order (customer_id);
CREATE INDEX sales_order_status_idx   ON sales.sales_order (status_code);
CREATE INDEX sales_order_received_idx ON sales.sales_order (received_at);
SELECT sales.attach_standard_triggers('sales.sales_order');

CREATE TABLE sales.sales_order_line (
    sales_order_line_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sales_order_id             bigint   NOT NULL REFERENCES sales.sales_order,
    line_number                smallint NOT NULL CHECK (line_number > 0),
    product_id                 bigint   REFERENCES sales.product,
    sku                        text,
    description                text     NOT NULL CHECK (btrim(description) <> ''),
    line_category_code         text     NOT NULL REFERENCES sales.line_category,
    supplier_id                bigint   NOT NULL REFERENCES sales.supplier,
    supplier_quote_id          bigint   REFERENCES sales.supplier_quote,
    quantity                   numeric(12,4) NOT NULL CHECK (quantity > 0),
    -- Unit prices are per billing period. billing_periods = the email's "Recurring" column
    -- (1 for one-off; 12 for annual-commit/monthly-billed; 36 for a 36-month service).
    billing_frequency_code     text     NOT NULL REFERENCES sales.billing_frequency,
    billing_periods            integer  NOT NULL CHECK (billing_periods > 0),
    cost_currency_code         char(3)  NOT NULL REFERENCES sales.currency,
    unit_cost_in_cost_currency numeric(18,4) NOT NULL CHECK (unit_cost_in_cost_currency >= 0),
    fx_rate_id                 bigint   REFERENCES sales.fx_rate,
    fx_rate                    numeric(18,8) NOT NULL DEFAULT 1 CHECK (fx_rate > 0),  -- set by trigger
    unit_sell                  numeric(18,4) NOT NULL CHECK (unit_sell >= 0),        -- order currency
    unit_cost                  numeric(18,4) GENERATED ALWAYS AS
                                   (round(unit_cost_in_cost_currency * fx_rate, 4)) STORED,
    net_cost                   numeric(18,2) GENERATED ALWAYS AS
                                   (round(quantity * billing_periods * round(unit_cost_in_cost_currency * fx_rate, 4), 2)) STORED,
    net_sell                   numeric(18,2) GENERATED ALWAYS AS
                                   (round(quantity * billing_periods * unit_sell, 2)) STORED,
    gross_margin               numeric(18,2) GENERATED ALWAYS AS
                                   (round(quantity * billing_periods * unit_sell, 2)
                                    - round(quantity * billing_periods * round(unit_cost_in_cost_currency * fx_rate, 4), 2)) STORED,
    notes                      text,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    created_by                 text        NOT NULL DEFAULT audit.current_actor(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    updated_by                 text        NOT NULL DEFAULT audit.current_actor(),
    row_version                integer     NOT NULL DEFAULT 1,
    UNIQUE (sales_order_id, line_number),
    CHECK (billing_frequency_code <> 'one_off' OR billing_periods = 1)
);
CREATE INDEX sales_order_line_supplier_idx ON sales.sales_order_line (supplier_id);
CREATE INDEX sales_order_line_product_idx  ON sales.sales_order_line (product_id);
SELECT sales.attach_standard_triggers('sales.sales_order_line');

-- Line integrity: FX rate must match the line's cost currency -> order currency and
-- is snapshotted from sales.fx_rate (users cannot type an FX rate onto a line);
-- supplier quote must belong to the line's supplier; lines are frozen once the
-- order reaches a locked status.
CREATE FUNCTION sales.tg_sales_order_line_validate() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_order_ccy   char(3);
    v_order_no    text;
    v_status      text;
    v_locked      boolean;
    v_fx          sales.fx_rate%ROWTYPE;
    v_quote_supp  bigint;
    v_order_id    bigint;
BEGIN
    v_order_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.sales_order_id ELSE NEW.sales_order_id END;

    SELECT o.currency_code, o.order_number, o.status_code, s.lines_locked
      INTO v_order_ccy, v_order_no, v_status, v_locked
      FROM sales.sales_order o JOIN sales.order_status s USING (status_code)
     WHERE o.sales_order_id = v_order_id;

    IF v_locked THEN
        RAISE EXCEPTION 'order % is in status % and its lines are locked', v_order_no, v_status
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.sales_order_id <> OLD.sales_order_id THEN
        RAISE EXCEPTION 'a line cannot be moved to a different order' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    IF NEW.cost_currency_code = v_order_ccy THEN
        IF NEW.fx_rate_id IS NOT NULL THEN
            RAISE EXCEPTION 'line %: fx_rate_id given but cost currency equals order currency (%)',
                NEW.line_number, v_order_ccy USING ERRCODE = 'check_violation';
        END IF;
        NEW.fx_rate := 1;
    ELSE
        IF NEW.fx_rate_id IS NULL THEN
            RAISE EXCEPTION 'line %: cost in % needs an FX rate to %', NEW.line_number,
                NEW.cost_currency_code, v_order_ccy USING ERRCODE = 'check_violation';
        END IF;
        SELECT * INTO v_fx FROM sales.fx_rate WHERE fx_rate_id = NEW.fx_rate_id;
        IF v_fx.from_currency_code <> NEW.cost_currency_code OR v_fx.to_currency_code <> v_order_ccy THEN
            RAISE EXCEPTION 'line %: FX rate % is %->%, expected %->%', NEW.line_number, v_fx.fx_rate_id,
                v_fx.from_currency_code, v_fx.to_currency_code, NEW.cost_currency_code, v_order_ccy
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.fx_rate := v_fx.rate;
    END IF;

    IF NEW.supplier_quote_id IS NOT NULL THEN
        SELECT supplier_id INTO v_quote_supp FROM sales.supplier_quote
         WHERE supplier_quote_id = NEW.supplier_quote_id;
        IF v_quote_supp <> NEW.supplier_id THEN
            RAISE EXCEPTION 'line %: supplier quote % belongs to a different supplier',
                NEW.line_number, NEW.supplier_quote_id USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END
$$;
CREATE TRIGGER validate_line
    BEFORE INSERT OR UPDATE OR DELETE ON sales.sales_order_line
    FOR EACH ROW EXECUTE FUNCTION sales.tg_sales_order_line_validate();

CREATE TABLE sales.sales_order_status_history (
    sales_order_status_history_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sales_order_id bigint NOT NULL REFERENCES sales.sales_order,
    from_status    text   REFERENCES sales.order_status,   -- NULL for creation
    to_status      text   NOT NULL REFERENCES sales.order_status,
    reason         text,
    changed_at     timestamptz NOT NULL DEFAULT now(),
    changed_by     text        NOT NULL DEFAULT audit.current_actor()
);
CREATE INDEX sales_order_status_history_order_idx ON sales.sales_order_status_history (sales_order_id);

-- Enforces the status workflow and records every change. The reason for a change
-- is passed via SET LOCAL app.status_reason.
CREATE FUNCTION sales.tg_sales_order_status() RETURNS trigger
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
        -- Approval gate: no error-level exceptions, no unresolved pre-processing checks.
        IF NEW.status_code = 'approved' THEN
            IF EXISTS (SELECT 1 FROM sales.v_sales_order_exception e
                        WHERE e.sales_order_id = NEW.sales_order_id AND e.severity = 'error') THEN
                RAISE EXCEPTION 'order % cannot be approved: it has error-level exceptions (see sales.v_sales_order_exception)',
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
CREATE TRIGGER status_workflow
    AFTER INSERT OR UPDATE OF status_code ON sales.sales_order
    FOR EACH ROW EXECUTE FUNCTION sales.tg_sales_order_status();

-- Commercial header fields are frozen with the lines.
CREATE FUNCTION sales.tg_sales_order_lock() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT lines_locked FROM sales.order_status WHERE status_code = OLD.status_code)
       AND (NEW.customer_id, NEW.currency_code, NEW.order_type_code, NEW.price_list_id, NEW.margin_exception_reason)
           IS DISTINCT FROM
           (OLD.customer_id, OLD.currency_code, OLD.order_type_code, OLD.price_list_id, OLD.margin_exception_reason)
    THEN
        RAISE EXCEPTION 'order %: commercial fields are locked in status %', OLD.order_number, OLD.status_code
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER lock_commercials
    BEFORE UPDATE ON sales.sales_order
    FOR EACH ROW EXECUTE FUNCTION sales.tg_sales_order_lock();

CREATE TABLE sales.sales_order_document (
    sales_order_id bigint NOT NULL REFERENCES sales.sales_order,
    document_id    bigint NOT NULL REFERENCES sales.document,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text        NOT NULL DEFAULT audit.current_actor(),
    PRIMARY KEY (sales_order_id, document_id)
);
CREATE TRIGGER audit_row AFTER INSERT OR UPDATE OR DELETE ON sales.sales_order_document
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();

-- Pre-processing checks per order (DD mandate, GDAP, customer PO, supplier account ...)
CREATE TABLE sales.sales_order_check (
    sales_order_check_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sales_order_id          bigint NOT NULL REFERENCES sales.sales_order,
    check_type_code         text   NOT NULL REFERENCES sales.check_type,
    check_status_code       text   NOT NULL DEFAULT 'pending' REFERENCES sales.check_status,
    evidence_document_id    bigint REFERENCES sales.document,
    checked_by_employee_id  bigint REFERENCES sales.employee,
    checked_at              timestamptz,
    notes                   text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    created_by              text        NOT NULL DEFAULT audit.current_actor(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    updated_by              text        NOT NULL DEFAULT audit.current_actor(),
    row_version             integer     NOT NULL DEFAULT 1,
    UNIQUE (sales_order_id, check_type_code),
    CHECK (check_status_code IN ('pending') OR (checked_by_employee_id IS NOT NULL AND checked_at IS NOT NULL)),
    CHECK (check_status_code <> 'waived' OR notes IS NOT NULL)
);
SELECT sales.attach_standard_triggers('sales.sales_order_check');

-- -----------------------------------------------------------------------------
-- Reporting views
-- -----------------------------------------------------------------------------

CREATE VIEW sales.v_customer_current_terms AS
SELECT t.*
  FROM sales.customer_credit_terms t
 WHERE t.effective_from <= current_date
   AND (t.effective_to IS NULL OR t.effective_to >= current_date);

CREATE VIEW sales.v_sales_order_line AS
SELECT l.*,
       bf.months_per_period,
       (bf.months_per_period IS NOT NULL)                                           AS is_recurring,
       CASE WHEN bf.months_per_period IS NOT NULL
            THEN round(l.quantity * l.unit_sell / bf.months_per_period, 2) END      AS monthly_recurring_sell,
       CASE WHEN bf.months_per_period IS NOT NULL
            THEN round(l.quantity * l.unit_cost / bf.months_per_period, 2) END      AS monthly_recurring_cost,
       CASE WHEN l.net_sell = 0 THEN NULL
            ELSE round(l.gross_margin / l.net_sell * 100, 2) END                    AS gross_margin_pct
  FROM sales.sales_order_line l
  JOIN sales.billing_frequency bf USING (billing_frequency_code);

CREATE VIEW sales.v_sales_order_summary AS
SELECT o.sales_order_id,
       o.order_number,
       o.title,
       o.status_code,
       o.order_type_code,
       c.customer_id,
       c.legal_name                                               AS customer_name,
       o.received_at,
       o.signed_date,
       o.currency_code,
       count(l.sales_order_line_id)                               AS line_count,
       COALESCE(sum(l.net_cost), 0)                               AS net_cost,
       COALESCE(sum(l.net_sell), 0)                               AS net_sell,
       COALESCE(sum(l.gross_margin), 0)                           AS gross_margin,
       CASE WHEN COALESCE(sum(l.net_sell), 0) = 0 THEN NULL
            ELSE round(sum(l.gross_margin) / sum(l.net_sell) * 100, 2) END AS gross_margin_pct,
       COALESCE(sum(l.net_sell)     FILTER (WHERE NOT l.is_recurring), 0) AS one_off_sell,
       COALESCE(sum(l.net_sell)     FILTER (WHERE l.is_recurring), 0)     AS recurring_contract_sell,
       COALESCE(sum(l.monthly_recurring_sell), 0)                 AS monthly_recurring_sell,
       COALESCE(sum(l.monthly_recurring_sell), 0)
         - COALESCE(sum(l.monthly_recurring_cost), 0)             AS monthly_recurring_margin,
       o.stated_net_cost,
       o.stated_net_sell,
       o.stated_gross_margin
  FROM sales.sales_order o
  JOIN sales.customer c USING (customer_id)
  LEFT JOIN sales.v_sales_order_line l USING (sales_order_id)
 GROUP BY o.sales_order_id, c.customer_id;

-- Rule-based exception report. Each rule is one UNION branch; add rules here.
CREATE VIEW sales.v_sales_order_exception AS
WITH s AS (SELECT * FROM sales.v_sales_order_summary),
     p AS (SELECT
             (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'min_order_gm_pct')  AS min_gm_pct,
             (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'max_fx_rate_age_days') AS max_fx_age,
             (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'stated_total_tolerance') AS tolerance)
SELECT s.sales_order_id, s.order_number, 'NO_LINES' AS rule_code, 'error' AS severity,
       'Order has no lines' AS message
  FROM s WHERE s.line_count = 0
UNION ALL
SELECT l.sales_order_id, o.order_number, 'NEGATIVE_LINE_MARGIN', 'error',
       format('Line %s (%s) sells below cost: margin %s', l.line_number, l.description, l.gross_margin)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.gross_margin < 0
UNION ALL
SELECT s.sales_order_id, s.order_number, 'LOW_ORDER_MARGIN', 'warning',
       format('Order GM %s%% is below policy minimum %s%%', s.gross_margin_pct, p.min_gm_pct)
  FROM s CROSS JOIN p
  JOIN sales.sales_order o USING (sales_order_id)
 WHERE s.gross_margin_pct < p.min_gm_pct AND o.margin_exception_reason IS NULL
UNION ALL
SELECT l.sales_order_id, o.order_number, 'TAX_LINE_MARKED_UP', 'error',
       format('Line %s (%s) is a tax pass-through but sell %s <> cost %s',
              l.line_number, l.description, l.net_sell, l.net_cost)
  FROM sales.sales_order_line l
  JOIN sales.sales_order o USING (sales_order_id)
  JOIN sales.line_category lc USING (line_category_code)
 WHERE lc.is_tax_pass_through AND l.net_sell <> l.net_cost
UNION ALL
SELECT s.sales_order_id, s.order_number, 'STATED_TOTAL_MISMATCH', 'error',
       format('Submitted totals (cost %s / sell %s / GM %s) do not match computed (cost %s / sell %s / GM %s)',
              s.stated_net_cost, s.stated_net_sell, s.stated_gross_margin,
              s.net_cost, s.net_sell, s.gross_margin)
  FROM s CROSS JOIN p
 WHERE abs(COALESCE(s.stated_net_cost, s.net_cost) - s.net_cost) > p.tolerance
    OR abs(COALESCE(s.stated_net_sell, s.net_sell) - s.net_sell) > p.tolerance
    OR abs(COALESCE(s.stated_gross_margin, s.gross_margin) - s.gross_margin) > p.tolerance
UNION ALL
SELECT o.sales_order_id, o.order_number, 'MISSING_SIGNED_ORDER', 'error',
       'No signed order document is attached'
  FROM sales.sales_order o
 WHERE NOT EXISTS (SELECT 1 FROM sales.sales_order_document sod
                     JOIN sales.document d USING (document_id)
                    WHERE sod.sales_order_id = o.sales_order_id
                      AND d.document_type_code = 'signed_order')
UNION ALL
SELECT o.sales_order_id, o.order_number, 'NO_CREDIT_TERMS', 'error',
       'Customer has no credit terms in force on the order date'
  FROM sales.sales_order o
 WHERE NOT EXISTS (SELECT 1 FROM sales.customer_credit_terms t
                    WHERE t.customer_id = o.customer_id
                      AND t.effective_from <= o.received_at::date
                      AND (t.effective_to IS NULL OR t.effective_to >= o.received_at::date))
UNION ALL
SELECT o.sales_order_id, o.order_number, 'CUSTOMER_CREDIT_RISK', 'warning',
       format('Customer risk rating is %s; terms: recurring %s days by %s, one-off %s',
              t.risk_rating_code, t.recurring_terms_days, t.recurring_payment_method_code,
              CASE WHEN t.one_off_prepayment_required THEN 'payment on order'
                   ELSE t.one_off_terms_days || ' days' END)
  FROM sales.sales_order o
  JOIN sales.customer_credit_terms t
    ON t.customer_id = o.customer_id
   AND t.effective_from <= o.received_at::date
   AND (t.effective_to IS NULL OR t.effective_to >= o.received_at::date)
  JOIN sales.risk_rating r USING (risk_rating_code)
 WHERE r.severity >= 2
UNION ALL
SELECT DISTINCT l.sales_order_id, o.order_number, 'SUPPLIER_NOT_APPROVED', 'error',
       format('Supplier %s account status is %s', sp.name, sp.account_status_code)
  FROM sales.sales_order_line l
  JOIN sales.sales_order o USING (sales_order_id)
  JOIN sales.supplier sp USING (supplier_id)
  JOIN sales.supplier_account_status sas ON sas.status_code = sp.account_status_code
 WHERE NOT sas.can_order
UNION ALL
SELECT DISTINCT l.sales_order_id, o.order_number, 'STALE_FX_RATE', 'warning',
       format('FX rate %s->%s dated %s is more than %s days before the order was received',
              fx.from_currency_code, fx.to_currency_code, fx.rate_date, p.max_fx_age)
  FROM sales.sales_order_line l
  JOIN sales.sales_order o USING (sales_order_id)
  JOIN sales.fx_rate fx USING (fx_rate_id)
  CROSS JOIN p
 WHERE o.received_at::date - fx.rate_date > p.max_fx_age
UNION ALL
SELECT c.sales_order_id, o.order_number, 'CHECK_FAILED', 'error',
       format('Pre-processing check %s failed', c.check_type_code)
  FROM sales.sales_order_check c JOIN sales.sales_order o USING (sales_order_id)
 WHERE c.check_status_code = 'failed';

COMMENT ON VIEW sales.v_sales_order_exception IS
    'One row per rule breach per order. Empty result for an order = clean.';
