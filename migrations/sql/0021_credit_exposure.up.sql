-- =============================================================================
-- 0021 Credit exposure (CFO, 2026-10-02: "we need invoices (current, overdue) + in progress panda docs.
-- then compare to credit limit. all customers").
--
-- Two daily snapshots, insert-only like the ARR snapshot:
-- * receivables: what each Xero contact owes (current = not yet due; overdue), from Xero's aged
--   receivables report;
-- * pipeline: PandaDoc documents sent to a client and not yet signed or closed, with their value.
-- sales.v_credit_exposure compares, for EVERY customer (monitored or not, excluded or not), what is owed
-- plus what is in progress against the credit limit in force. Pipeline values are quoted figures; VAT is
-- added when credit_pipeline_add_vat = 1 (PandaDoc totals are ex VAT), because limits include VAT.
-- =============================================================================

INSERT INTO sales.policy_setting (setting_key, numeric_value, description) VALUES
    ('credit_pipeline_add_vat', 1, 'Add VAT (credit_vat_rate) to PandaDoc values in credit exposure: 1 = yes (totals are ex VAT), 0 = no');

CREATE TABLE sales.credit_receivable_snapshot (
    credit_receivable_snapshot_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    as_of         date NOT NULL,
    source        text NOT NULL CHECK (btrim(source) <> ''),
    source_sha256 char(64) NOT NULL UNIQUE CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    loaded_by     text        NOT NULL DEFAULT audit.current_actor()
);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_receivable_snapshot
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();

CREATE TABLE sales.credit_receivable_line (
    credit_receivable_snapshot_id bigint NOT NULL REFERENCES sales.credit_receivable_snapshot,
    line_no         integer NOT NULL CHECK (line_no > 0),
    contact_name    text    NOT NULL CHECK (btrim(contact_name) <> ''),
    xero_contact_id uuid    REFERENCES sales.xero_contact_directory,
    current_amount  numeric(18,2) NOT NULL,
    overdue_amount  numeric(18,2) NOT NULL,
    overdue_over_60 numeric(18,2) NOT NULL,
    outstanding     numeric(18,2) GENERATED ALWAYS AS (current_amount + overdue_amount) STORED,
    oldest_due_date date,
    invoice_count   integer NOT NULL CHECK (invoice_count >= 0),
    PRIMARY KEY (credit_receivable_snapshot_id, line_no),
    CHECK (overdue_over_60 <= overdue_amount OR overdue_amount < 0)
);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_receivable_line
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();

CREATE TABLE sales.credit_pipeline_snapshot (
    credit_pipeline_snapshot_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    as_of         date NOT NULL,
    source        text NOT NULL CHECK (btrim(source) <> ''),
    source_sha256 char(64) NOT NULL UNIQUE CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    loaded_by     text        NOT NULL DEFAULT audit.current_actor()
);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_pipeline_snapshot
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();

CREATE TABLE sales.credit_pipeline_document (
    credit_pipeline_snapshot_id bigint NOT NULL REFERENCES sales.credit_pipeline_snapshot,
    document_id     text    NOT NULL CHECK (btrim(document_id) <> ''),
    name            text    NOT NULL,
    status          text    NOT NULL,
    date_sent       timestamptz,
    expiration_date timestamptz,
    grand_total     numeric(18,2) NOT NULL,
    currency        char(3) NOT NULL,
    client_company  text,
    recipient_domains text[] NOT NULL DEFAULT '{}',
    xero_contact_id uuid    REFERENCES sales.xero_contact_directory,
    match_method    text    CHECK (match_method IN ('client_company', 'document_name', 'recipient_domain')),
    PRIMARY KEY (credit_pipeline_snapshot_id, document_id),
    CHECK ((xero_contact_id IS NULL) = (match_method IS NULL))
);
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_pipeline_document
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();

CREATE VIEW sales.v_credit_exposure AS
WITH p AS (
    SELECT (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'credit_vat_rate') AS vat,
           (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'credit_pipeline_add_vat') AS add_vat
), ar_snap AS (
    SELECT max(credit_receivable_snapshot_id) AS id FROM sales.credit_receivable_snapshot
), pl_snap AS (
    SELECT max(credit_pipeline_snapshot_id) AS id FROM sales.credit_pipeline_snapshot
), recv AS (
    SELECT l.xero_contact_id, sum(l.current_amount) AS current_amount, sum(l.overdue_amount) AS overdue_amount,
           sum(l.overdue_over_60) AS overdue_over_60, sum(l.outstanding) AS outstanding,
           min(l.oldest_due_date) AS oldest_due_date, sum(l.invoice_count) AS invoice_count
      FROM sales.credit_receivable_line l JOIN ar_snap ON l.credit_receivable_snapshot_id = ar_snap.id
     WHERE l.xero_contact_id IS NOT NULL
     GROUP BY l.xero_contact_id
), pl AS (
    SELECT d.xero_contact_id, count(*) AS pipeline_count, sum(d.grand_total) AS pipeline_value
      FROM sales.credit_pipeline_document d JOIN pl_snap ON d.credit_pipeline_snapshot_id = pl_snap.id
     WHERE d.xero_contact_id IS NOT NULL
     GROUP BY d.xero_contact_id
), cust AS (   -- one customer per Xero contact (a monitored one first)
    SELECT DISTINCT ON (c.xero_contact_id) c.customer_id, c.legal_name, c.xero_contact_id,
           EXISTS (SELECT 1 FROM sales.credit_subject s WHERE s.customer_id = c.customer_id AND s.is_active) AS monitored
      FROM sales.customer c
     WHERE c.xero_contact_id IS NOT NULL
     ORDER BY c.xero_contact_id,
              EXISTS (SELECT 1 FROM sales.credit_subject s WHERE s.customer_id = c.customer_id AND s.is_active) DESC,
              c.customer_id
), excl AS (
    SELECT xero_contact_id, string_agg(excluded_reason, '; ' ORDER BY arr_prefix) AS excluded_reason
      FROM sales.credit_customer_match WHERE xero_contact_id IS NOT NULL AND excluded_reason IS NOT NULL
     GROUP BY xero_contact_id
), prefix_map AS (   -- every ARR prefix billed to the contact
    SELECT xero_contact_id, arr_prefix FROM sales.customer WHERE xero_contact_id IS NOT NULL AND arr_prefix IS NOT NULL
    UNION
    SELECT xero_contact_id, arr_prefix FROM sales.credit_customer_match WHERE xero_contact_id IS NOT NULL
), arr AS (
    SELECT pm.xero_contact_id, string_agg(pm.arr_prefix, ', ' ORDER BY pm.arr_prefix) AS arr_prefixes,
           sum(a.annual_recurring) AS annual_recurring
      FROM prefix_map pm
      LEFT JOIN (SELECT l.arr_prefix, sum(l.annual_revenue) AS annual_recurring
                   FROM sales.credit_arr_line l JOIN sales.credit_arr_status st USING (arr_status)
                  WHERE l.credit_arr_snapshot_id = sales.latest_credit_arr_snapshot() AND st.counts_as_commitment
                  GROUP BY l.arr_prefix) a USING (arr_prefix)
     GROUP BY pm.xero_contact_id
), keys AS (
    SELECT xero_contact_id FROM cust
    UNION SELECT xero_contact_id FROM prefix_map
    UNION SELECT xero_contact_id FROM recv
    UNION SELECT xero_contact_id FROM pl
)
SELECT k.xero_contact_id,
       COALESCE(c.legal_name, d.name) AS name,
       d.name AS xero_name,
       c.customer_id,
       COALESCE(c.monitored, false) AS monitored,
       excl.excluded_reason,
       arr.arr_prefixes,
       arr.annual_recurring,
       lim.credit_limit,
       COALESCE(ar.current_amount, 0) AS current_amount,
       COALESCE(ar.overdue_amount, 0) AS overdue_amount,
       COALESCE(ar.overdue_over_60, 0) AS overdue_over_60,
       COALESCE(ar.outstanding, 0) AS outstanding,
       ar.oldest_due_date,
       COALESCE(pl.pipeline_count, 0) AS pipeline_count,
       COALESCE(pl.pipeline_value, 0) AS pipeline_value,
       round(COALESCE(pl.pipeline_value, 0) * (1 + CASE WHEN p.add_vat = 1 THEN p.vat ELSE 0 END), 2) AS pipeline_gross,
       COALESCE(ar.outstanding, 0)
         + round(COALESCE(pl.pipeline_value, 0) * (1 + CASE WHEN p.add_vat = 1 THEN p.vat ELSE 0 END), 2) AS exposure,
       lim.credit_limit - (COALESCE(ar.outstanding, 0)
         + round(COALESCE(pl.pipeline_value, 0) * (1 + CASE WHEN p.add_vat = 1 THEN p.vat ELSE 0 END), 2)) AS headroom
  FROM keys k
  CROSS JOIN p
  LEFT JOIN sales.xero_contact_directory d ON d.xero_contact_id = k.xero_contact_id
  LEFT JOIN cust c ON c.xero_contact_id = k.xero_contact_id
  LEFT JOIN excl ON excl.xero_contact_id = k.xero_contact_id
  LEFT JOIN arr ON arr.xero_contact_id = k.xero_contact_id
  LEFT JOIN sales.v_customer_current_credit_limit lim ON lim.customer_id = c.customer_id
  LEFT JOIN recv ar ON ar.xero_contact_id = k.xero_contact_id
  LEFT JOIN pl ON pl.xero_contact_id = k.xero_contact_id;
