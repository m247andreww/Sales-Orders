-- =============================================================================
-- 0012 Xero contact groups record the account owner (CFO, 2026-09-25): "the a/c owner is defined
-- in Xero under groups" (one group per salesperson, e.g. "a. <first name>").
--
-- Xero is the recorded master for the CURRENT owner. The database keeps the dated history
-- (customer_account_allocation) and reconciles to Xero on every sync:
--   * Xero changed more recently than the database  -> the database adopts Xero's owner;
--   * the database changed more recently (an approved order passed the account on) -> finance
--     is told to update the Xero group;
--   * first sync, no way to tell which is newer     -> reported; adopted only on CFO instruction.
-- Xero keeps no history of group membership, so a change is dated the day a sync first sees it.
-- Only groups mapped in sales.xero_owner_group count as ownership groups; others are ignored.
-- =============================================================================

CREATE TABLE sales.xero_owner_group (
    xero_owner_group_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_name            text   NOT NULL CHECK (btrim(group_name) <> ''),
    xero_contact_group_id uuid   UNIQUE,                    -- filled in by the first sync that sees it
    employee_id           bigint REFERENCES sales.employee,
    house_account         text   REFERENCES sales.house_account_type,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text        NOT NULL DEFAULT audit.current_actor(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            text        NOT NULL DEFAULT audit.current_actor(),
    row_version           integer     NOT NULL DEFAULT 1,
    CHECK ((employee_id IS NULL) <> (house_account IS NULL))
);
CREATE UNIQUE INDEX xero_owner_group_name_uq ON sales.xero_owner_group ((lower(btrim(group_name))));
COMMENT ON TABLE sales.xero_owner_group IS
    'Xero contact groups that record the account owner, mapped to a salesperson or House.';
SELECT sales.attach_standard_triggers('sales.xero_owner_group');

CREATE TABLE sales.xero_group_sync (
    xero_group_sync_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source             text        NOT NULL,     -- 'Xero API', or the file a snapshot came from
    synced_at          timestamptz NOT NULL DEFAULT now(),
    synced_by          text        NOT NULL DEFAULT audit.current_actor(),
    groups_seen        integer     NOT NULL CHECK (groups_seen >= 0),
    memberships        integer     NOT NULL CHECK (memberships >= 0)
);
CREATE TRIGGER audit_row AFTER INSERT ON sales.xero_group_sync
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();

-- Every group membership seen by every sync (append-only evidence of what Xero said, and when).
CREATE TABLE sales.xero_group_membership (
    xero_group_sync_id    bigint NOT NULL REFERENCES sales.xero_group_sync,
    xero_contact_group_id uuid   NOT NULL,
    group_name            text   NOT NULL,
    xero_contact_id       uuid   NOT NULL,
    contact_name          text,
    PRIMARY KEY (xero_group_sync_id, xero_contact_group_id, xero_contact_id)
);
CREATE INDEX xero_group_membership_contact_idx ON sales.xero_group_membership (xero_contact_id);

-- Xero's owner for each customer as of the latest sync, and since when it has said so.
-- observed_since is NULL when the owner has been the same since the very first sync (baseline:
-- nothing shows whether Xero or the database is more recent).
CREATE TABLE sales.customer_xero_owner (
    customer_id        bigint PRIMARY KEY REFERENCES sales.customer,
    employee_id        bigint REFERENCES sales.employee,
    house_account      text   REFERENCES sales.house_account_type,
    owner_groups       integer     NOT NULL CHECK (owner_groups >= 0),   -- 0 none, 2+ ambiguous
    group_names        text,
    observed_since     timestamptz,
    last_sync_id       bigint      NOT NULL REFERENCES sales.xero_group_sync,
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         text        NOT NULL DEFAULT audit.current_actor(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    updated_by         text        NOT NULL DEFAULT audit.current_actor(),
    row_version        integer     NOT NULL DEFAULT 1,
    CHECK (employee_id IS NULL OR house_account IS NULL),
    CHECK ((owner_groups = 1) = (employee_id IS NOT NULL OR house_account IS NOT NULL))
);
COMMENT ON TABLE sales.customer_xero_owner IS
    'Account owner according to the Xero contact groups at the latest sync.';
SELECT sales.attach_standard_triggers('sales.customer_xero_owner');

-- Refresh customer_xero_owner from one sync's memberships. Returns the customers whose Xero owner changed.
CREATE FUNCTION sales.refresh_customer_xero_owner(p_sync_id bigint) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_synced_at timestamptz;
    v_baseline  boolean;
    v_changed   integer;
BEGIN
    SELECT synced_at INTO v_synced_at FROM sales.xero_group_sync WHERE xero_group_sync_id = p_sync_id;
    IF v_synced_at IS NULL THEN
        RAISE EXCEPTION 'unknown Xero group sync %', p_sync_id USING ERRCODE = 'no_data_found';
    END IF;
    v_baseline := NOT EXISTS (SELECT 1 FROM sales.customer_xero_owner);

    -- Record the Xero group ids of mapped groups (matched by name the first time).
    UPDATE sales.xero_owner_group g SET xero_contact_group_id = m.xero_contact_group_id
      FROM (SELECT DISTINCT xero_contact_group_id, group_name FROM sales.xero_group_membership
             WHERE xero_group_sync_id = p_sync_id) m
     WHERE g.xero_contact_group_id IS NULL AND lower(btrim(g.group_name)) = lower(btrim(m.group_name));

    WITH owner_rows AS (
        SELECT c.customer_id, g.employee_id, g.house_account, m.group_name
          FROM sales.customer c
          JOIN sales.xero_group_membership m
            ON m.xero_contact_id = c.xero_contact_id AND m.xero_group_sync_id = p_sync_id
          JOIN sales.xero_owner_group g
            ON g.xero_contact_group_id = m.xero_contact_group_id
    ), per_customer AS (
        SELECT c.customer_id,
               count(o.group_name)::integer AS owner_groups,
               CASE WHEN count(o.group_name) = 1 THEN min(o.employee_id) END AS employee_id,
               CASE WHEN count(o.group_name) = 1 THEN min(o.house_account) END AS house_account,
               string_agg(o.group_name, ', ' ORDER BY o.group_name) AS group_names
          FROM sales.customer c
          LEFT JOIN owner_rows o USING (customer_id)
         WHERE c.xero_contact_id IS NOT NULL
         GROUP BY c.customer_id
    ), changed AS (
        INSERT INTO sales.customer_xero_owner AS x
               (customer_id, employee_id, house_account, owner_groups, group_names, observed_since, last_sync_id)
        SELECT p.customer_id, p.employee_id, p.house_account, p.owner_groups, p.group_names,
               CASE WHEN v_baseline THEN NULL ELSE v_synced_at END, p_sync_id
          FROM per_customer p
        ON CONFLICT (customer_id) DO UPDATE
           SET employee_id = EXCLUDED.employee_id, house_account = EXCLUDED.house_account,
               owner_groups = EXCLUDED.owner_groups, group_names = EXCLUDED.group_names,
               last_sync_id = EXCLUDED.last_sync_id,
               observed_since = CASE WHEN (x.employee_id, x.house_account, x.owner_groups, x.group_names)
                                          IS DISTINCT FROM
                                          (EXCLUDED.employee_id, EXCLUDED.house_account, EXCLUDED.owner_groups,
                                           EXCLUDED.group_names)
                                     THEN v_synced_at ELSE x.observed_since END
        RETURNING x.observed_since
    )
    SELECT count(*) FILTER (WHERE observed_since = v_synced_at) INTO v_changed FROM changed;
    RETURN v_changed;
END
$$;

-- Database owner today versus Xero's owner, with the action each difference needs.
CREATE VIEW sales.v_account_owner_reconciliation AS
WITH d AS (
    SELECT c.customer_id, c.legal_name, c.xero_contact_id,
           al.customer_account_allocation_id AS allocation_id, al.employee_id AS db_employee_id,
           al.house_account AS db_house_account, al.allocated_from AS db_owner_from, al.source AS db_source,
           al.updated_at AS db_recorded_at,   -- when the database last changed this owner period
           x.employee_id AS xero_employee_id, x.house_account AS xero_house_account, x.owner_groups,
           x.group_names, x.observed_since AS xero_observed_since, x.customer_id IS NOT NULL AS synced
      FROM sales.customer c
      LEFT JOIN LATERAL sales.account_allocation_on(c.customer_id, (now() AT TIME ZONE 'Europe/London')::date) al
             ON true
      LEFT JOIN sales.customer_xero_owner x ON x.customer_id = c.customer_id
     WHERE c.is_active
), s AS (
    SELECT d.customer_id, d.legal_name, d.xero_contact_id, d.allocation_id, d.db_employee_id,
           d.db_house_account, d.db_owner_from, d.db_source, d.db_recorded_at, d.xero_employee_id,
           d.xero_house_account, d.owner_groups, d.group_names, d.xero_observed_since,
           CASE
               WHEN d.xero_contact_id IS NULL THEN 'NO_XERO_CONTACT'
               WHEN NOT d.synced THEN 'NOT_SYNCED'
               WHEN d.owner_groups = 0 THEN 'NOT_IN_XERO_OWNER_GROUP'
               WHEN d.owner_groups > 1 THEN 'MULTIPLE_XERO_OWNER_GROUPS'
               WHEN (d.db_employee_id, d.db_house_account) IS NOT DISTINCT FROM (d.xero_employee_id, d.xero_house_account)
                   THEN 'MATCH'
               WHEN xe.employee_id IS NOT NULL AND NOT xe.is_active THEN 'XERO_OWNER_HAS_LEFT'
               WHEN d.allocation_id IS NULL THEN 'XERO_CHANGED'
               WHEN d.xero_observed_since > d.db_recorded_at THEN 'XERO_CHANGED'
               WHEN d.db_recorded_at > COALESCE(d.xero_observed_since,
                                                (SELECT min(synced_at) FROM sales.xero_group_sync)) THEN 'DB_CHANGED'
               ELSE 'DIFFERS'
           END AS status
      FROM d
      LEFT JOIN sales.employee xe ON xe.employee_id = d.xero_employee_id
)
SELECT s.customer_id, s.legal_name, s.xero_contact_id,
       COALESCE(de.full_name, s.db_house_account) AS db_owner, s.db_owner_from, s.db_source,
       COALESCE(xe.full_name, s.xero_house_account) AS xero_owner, s.group_names AS xero_groups,
       s.xero_observed_since, s.status,
       CASE s.status
           WHEN 'NO_XERO_CONTACT' THEN 'Link the customer to its Xero contact (xero_contact_id)'
           WHEN 'NOT_SYNCED' THEN 'Run sync-xero-groups'
           WHEN 'NOT_IN_XERO_OWNER_GROUP' THEN format('Put the contact in the Xero group for %s',
                                                      COALESCE(de.full_name, s.db_house_account, 'its owner'))
           WHEN 'MULTIPLE_XERO_OWNER_GROUPS' THEN format('Leave the contact in one owner group only (now: %s)', s.group_names)
           WHEN 'XERO_OWNER_HAS_LEFT' THEN format('Xero group maps to %s, who has left: move the contact to its new owner''s group', xe.full_name)
           WHEN 'XERO_CHANGED' THEN format('Database adopts Xero owner %s', COALESCE(xe.full_name, s.xero_house_account))
           WHEN 'DB_CHANGED' THEN format('Move the Xero contact to the group for %s (%s)',
                                         COALESCE(de.full_name, s.db_house_account), s.db_source)
           WHEN 'DIFFERS' THEN 'CFO to confirm which owner is right (sync-xero-groups --adopt-xero takes Xero''s)'
       END AS action
  FROM s
  LEFT JOIN sales.employee de ON de.employee_id = s.db_employee_id
  LEFT JOIN sales.employee xe ON xe.employee_id = s.xero_employee_id;

COMMENT ON VIEW sales.v_account_owner_reconciliation IS
    'Account owner in the database today versus the Xero owner group, with the action a difference needs.';

-- Xero contacts in an owner group that match no customer in this database.
CREATE VIEW sales.v_xero_owner_unmatched_contact AS
SELECT m.xero_contact_id, m.contact_name, m.group_name
  FROM sales.xero_group_membership m
  JOIN sales.xero_owner_group g ON g.xero_contact_group_id = m.xero_contact_group_id
 WHERE m.xero_group_sync_id = (SELECT max(xero_group_sync_id) FROM sales.xero_group_sync)
   AND NOT EXISTS (SELECT 1 FROM sales.customer c WHERE c.xero_contact_id = m.xero_contact_id);

-- Give an account to an owner (salesperson or House) from a date. A later allocation is never
-- overwritten (logged for review instead); an allocation starting the same day is replaced and logged.
CREATE FUNCTION sales.set_account_owner(p_customer_id bigint, p_employee_id bigint, p_house text,
                                        p_from date, p_source text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    v_cur sales.customer_account_allocation;
BEGIN
    IF (p_employee_id IS NULL) = (p_house IS NULL) THEN
        RAISE EXCEPTION 'give exactly one of employee or house account' USING ERRCODE = 'check_violation';
    END IF;
    IF p_employee_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sales.employee WHERE employee_id = p_employee_id AND is_active) THEN
        RAISE EXCEPTION 'employee % is unknown or has left', p_employee_id USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM sales.customer_account_allocation
                WHERE customer_id = p_customer_id AND allocated_from > p_from) THEN
        INSERT INTO sales.account_history_review (customer_id, on_date, note)
        VALUES (p_customer_id, p_from, format('%s: a later ownership change exists, so ownership was not changed', p_source));
        RETURN 'later ownership change exists: logged';
    END IF;
    v_cur := sales.account_allocation_on(p_customer_id, p_from);
    IF v_cur.customer_account_allocation_id IS NOT NULL
       AND (v_cur.employee_id, v_cur.house_account) IS NOT DISTINCT FROM (p_employee_id, p_house) THEN
        RETURN 'already owner';
    END IF;
    IF v_cur.customer_account_allocation_id IS NOT NULL AND v_cur.allocated_from = p_from THEN
        UPDATE sales.customer_account_allocation
           SET employee_id = p_employee_id, house_account = p_house, source = p_source
         WHERE customer_account_allocation_id = v_cur.customer_account_allocation_id;
        INSERT INTO sales.account_history_review (customer_id, on_date, note)
        VALUES (p_customer_id, p_from, format('Owner set on %s replaced a same-day change (%s)', p_from, v_cur.source));
        RETURN 'same-day owner replaced: logged';
    END IF;
    IF v_cur.customer_account_allocation_id IS NOT NULL THEN
        UPDATE sales.customer_account_allocation SET allocated_to = p_from - 1
         WHERE customer_account_allocation_id = v_cur.customer_account_allocation_id;
    END IF;
    INSERT INTO sales.customer_account_allocation (customer_id, employee_id, house_account, allocated_from, source)
    VALUES (p_customer_id, p_employee_id, p_house, p_from, p_source);
    RETURN 'ownership set';
END
$$;

-- Apply Xero where Xero is the more recent change (and, with p_adopt_all, where nothing shows which is newer:
-- the CFO's instruction that Xero is right, typically once at go-live). Changes are dated the day
-- the sync saw them, in UK time.
CREATE FUNCTION sales.apply_xero_owners(p_adopt_all boolean)
RETURNS TABLE (customer text, xero_owner text, result text)
LANGUAGE plpgsql AS $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT v.customer_id, v.legal_name, v.xero_owner, v.status, x.employee_id, x.house_account,
               -- baseline owners date from the first sync: the earliest evidence of them
               (COALESCE(x.observed_since, (SELECT min(synced_at) FROM sales.xero_group_sync))
                   AT TIME ZONE 'Europe/London')::date AS seen_on, sy.source
          FROM sales.v_account_owner_reconciliation v
          JOIN sales.customer_xero_owner x USING (customer_id)
          JOIN sales.xero_group_sync sy ON sy.xero_group_sync_id = x.last_sync_id
         WHERE v.status = 'XERO_CHANGED' OR (p_adopt_all AND v.status = 'DIFFERS')
         ORDER BY v.legal_name
    LOOP
        customer := r.legal_name;
        xero_owner := r.xero_owner;
        result := sales.set_account_owner(
            r.customer_id, r.employee_id, r.house_account, r.seen_on,
            format('Xero owner group (%s%s)', r.source, CASE WHEN r.status <> 'XERO_CHANGED' THEN ', adopted on CFO instruction' ELSE '' END));
        RETURN NEXT;
    END LOOP;
END
$$;

-- ------------------------------------------------------------ exception rule
CREATE VIEW sales.v_sales_order_exception_0012 AS
SELECT o.sales_order_id, o.order_number, 'XERO_OWNER_MISMATCH' AS rule_code, 'warning' AS severity,
       format('Account owner per Xero groups: %s; per this database: %s (%s)',
              COALESCE(v.xero_owner, v.xero_groups, 'none'), COALESCE(v.db_owner, 'none'), v.action) AS message
  FROM sales.sales_order o
  JOIN sales.v_account_owner_reconciliation v ON v.customer_id = o.customer_id
 WHERE o.status_code IN ('received', 'validated', 'on_hold')
   AND v.status NOT IN ('MATCH', 'NOT_SYNCED', 'NO_XERO_CONTACT');

CREATE OR REPLACE VIEW sales.v_sales_order_exception_all AS
SELECT sales_order_id, order_number, rule_code, severity, message FROM sales.v_sales_order_exception
UNION ALL
SELECT sales_order_id, order_number, rule_code, severity, message FROM sales.v_sales_order_exception_0004
UNION ALL
SELECT sales_order_id, order_number, rule_code, severity, message FROM sales.v_sales_order_exception_0006
UNION ALL
SELECT sales_order_id, order_number, rule_code, severity, message FROM sales.v_sales_order_exception_0012;

INSERT INTO sales.exception_rule (rule_code, description, blocks_approval, introduced_in) VALUES
    ('XERO_OWNER_MISMATCH', 'Account owner differs between Xero contact groups and this database (warning)', false, '0012');
