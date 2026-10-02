-- =============================================================================
-- 0018 Daily scan for customers with recurring revenue but no credit monitoring (CFO, 2026-10-02:
-- "scan for new customers who don't yet have credit limits defined thru monitoring").
--
-- * sales.xero_contact_directory: the Xero contact list (name, company number, customer/supplier),
--   refreshed by each daily run from the Xero custom connection. Used only to SUGGEST the Xero contact
--   and company number for an unmonitored customer; nothing is linked until the CFO confirms on the
--   Credit Desk ("added to monitoring").
-- * sales.v_credit_unmonitored_customer: every ARR prefix with commitments in the latest ARR file and no
--   monitored client, with when it first appeared; is_new marks prefixes that appeared in the latest
--   file for the first time (and an earlier file exists to compare with).
-- =============================================================================

CREATE TABLE sales.xero_contact_directory (
    xero_contact_id uuid PRIMARY KEY,
    name            text    NOT NULL CHECK (btrim(name) <> ''),
    company_number  text,
    is_customer     boolean NOT NULL,
    is_supplier     boolean NOT NULL,
    contact_status  text    NOT NULL,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_synced_at  timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text        NOT NULL DEFAULT audit.current_actor(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text        NOT NULL DEFAULT audit.current_actor(),
    row_version     integer     NOT NULL DEFAULT 1
);
SELECT sales.attach_standard_triggers('sales.xero_contact_directory');

CREATE VIEW sales.v_credit_unmonitored_customer AS
WITH latest AS (
    SELECT sales.latest_credit_arr_snapshot() AS id
), committed AS (
    SELECT l.credit_arr_snapshot_id, l.arr_prefix, l.customer_name, l.annual_revenue
      FROM sales.credit_arr_line l
      JOIN sales.credit_arr_status st USING (arr_status)
     WHERE st.counts_as_commitment AND l.arr_prefix IS NOT NULL
), cur AS (
    SELECT c.arr_prefix, string_agg(DISTINCT c.customer_name, ' / ') AS customer_name,
           sum(c.annual_revenue) AS annual_revenue, count(*) AS line_count
      FROM committed c, latest
     WHERE c.credit_arr_snapshot_id = latest.id
     GROUP BY c.arr_prefix
), seen AS (
    SELECT c.arr_prefix, min(s.loaded_at) AS first_seen_at, min(c.credit_arr_snapshot_id) AS first_snapshot_id
      FROM committed c JOIN sales.credit_arr_snapshot s USING (credit_arr_snapshot_id)
     GROUP BY c.arr_prefix
)
SELECT cur.arr_prefix, cur.customer_name, cur.annual_revenue, cur.line_count, seen.first_seen_at,
       (seen.first_snapshot_id = latest.id
        AND EXISTS (SELECT 1 FROM sales.credit_arr_snapshot e WHERE e.credit_arr_snapshot_id < latest.id)) AS is_new
  FROM cur
  JOIN seen USING (arr_prefix)
  CROSS JOIN latest
 WHERE NOT EXISTS (SELECT 1
                     FROM sales.credit_subject s
                     JOIN sales.customer c USING (customer_id)
                    WHERE s.is_active AND c.arr_prefix = cur.arr_prefix);
