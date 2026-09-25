-- =============================================================================
-- 0003 CFO decisions of 2026-09-24
--   1. No minimum gross margin. Selling at a loss is allowed but needs a rationale
--      (per line, or for the whole order) and a margin_approval check.
--      FX rates may be up to 28 days old. Totals tolerance stays at +/- 0.05.
--   3. Approvals are restricted: only the CFO may approve orders, waive checks,
--      approve losses and set non-standard credit terms. Permissions are data,
--      granted by migration only (the application cannot grant itself rights).
--   Identity hardening: when the role sales_orders_person exists (production, with
--   Microsoft Entra ID logins), the acting user is the authenticated login and
--   privileged actions must come from a personal login, not the shared app login.
-- =============================================================================

-- ---------------------------------------------------------------- 1. policy
DELETE FROM sales.policy_setting WHERE setting_key = 'min_order_gm_pct';
UPDATE sales.policy_setting SET numeric_value = 28 WHERE setting_key = 'max_fx_rate_age_days';
UPDATE sales.policy_setting SET numeric_value = 0.05 WHERE setting_key = 'stated_total_tolerance';

ALTER TABLE sales.sales_order_line
    ADD COLUMN margin_rationale text CHECK (margin_rationale IS NULL OR btrim(margin_rationale) <> '');
COMMENT ON COLUMN sales.sales_order_line.margin_rationale IS
    'Why this line is sold below cost. Required for a loss line unless the order has margin_exception_reason.';
ALTER TABLE sales.sales_order
    ADD CONSTRAINT sales_order_margin_reason_not_blank
    CHECK (margin_exception_reason IS NULL OR btrim(margin_exception_reason) <> '');
COMMENT ON COLUMN sales.sales_order.margin_exception_reason IS
    'Order-level rationale for selling at a loss (covers every loss line on the order).';

-- ---------------------------------------------------------------- 3. authority
CREATE TABLE sales.permission (
    permission_code text PRIMARY KEY,
    description     text NOT NULL
);
INSERT INTO sales.permission (permission_code, description) VALUES
    ('approve_order',        'Move an order to approved'),
    ('waive_check',          'Waive a pre-processing check'),
    ('approve_loss',         'Pass the margin_approval check on a loss-making order'),
    ('approve_credit_terms', 'Create or change non-standard customer credit terms');

CREATE TABLE sales.employee_permission (
    employee_id     bigint NOT NULL REFERENCES sales.employee,
    permission_code text   NOT NULL REFERENCES sales.permission,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text        NOT NULL DEFAULT audit.current_actor(),
    PRIMARY KEY (employee_id, permission_code)
);
CREATE TRIGGER audit_row AFTER INSERT OR UPDATE OR DELETE ON sales.employee_permission
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();
COMMENT ON TABLE sales.employee_permission IS 'Who may do privileged actions. Changed by migration only.';

INSERT INTO sales.employee (email, full_name, job_title)
VALUES ('andrew.whitford@managed.co.uk', 'Andrew Whitford', 'Chief Financial Officer')
ON CONFLICT ((lower(email))) DO NOTHING;

INSERT INTO sales.employee_permission (employee_id, permission_code)
SELECT e.employee_id, p.permission_code
  FROM sales.employee e CROSS JOIN sales.permission p
 WHERE lower(e.email) = 'andrew.whitford@managed.co.uk';

-- ---------------------------------------------------------------- identity
-- Production: people log in with their own Entra ID identity and are members of
-- sales_orders_person; the login then IS the actor and cannot be overridden.
-- Service / development connections use app.current_user, falling back to the login.
-- True when the role sales_orders_person exists (production mode). NULL-safe helper.
CREATE FUNCTION audit.personal_logins_enforced() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sales_orders_person')
$$;

-- Direct, explicit membership only. pg_has_role() is deliberately NOT used: it reports every
-- superuser/administrator as a member of every role, which would let an admin login pass as a person.
CREATE FUNCTION audit.is_personal_login() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_auth_members m
          JOIN pg_catalog.pg_roles grp ON grp.oid = m.roleid
          JOIN pg_catalog.pg_roles usr ON usr.oid = m.member
         WHERE grp.rolname = 'sales_orders_person'
           AND usr.rolname = session_user
    )
$$;

CREATE OR REPLACE FUNCTION audit.current_actor() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE
             WHEN audit.is_personal_login() THEN session_user::text
             ELSE COALESCE(NULLIF(current_setting('app.current_user', true), ''), session_user::text)
           END
$$;

CREATE FUNCTION sales.current_employee_id() RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT employee_id FROM sales.employee
     WHERE lower(email) = lower(audit.current_actor()) AND is_active
$$;

CREATE FUNCTION sales.require_permission(p_permission text, p_action text) RETURNS void
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF audit.personal_logins_enforced() AND NOT audit.is_personal_login() THEN
        RAISE EXCEPTION '% must be done from a personal login, not login %', p_action, session_user
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM sales.employee_permission
                    WHERE employee_id = sales.current_employee_id() AND permission_code = p_permission) THEN
        RAISE EXCEPTION '% is not permitted for %', p_action, audit.current_actor()
            USING ERRCODE = 'insufficient_privilege',
                  HINT = format('requires permission %s (see sales.employee_permission)', p_permission);
    END IF;
END
$$;

-- Status workflow: as 0001, plus the approval permission.
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

-- Checks: always start pending; every outcome is recorded against the acting employee
-- (nobody can record a check in someone else's name); waivers and loss approvals are privileged.
CREATE FUNCTION sales.tg_sales_order_check_authority() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_employee bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.check_status_code <> 'pending' THEN
            RAISE EXCEPTION 'checks must be created as pending' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.check_status_code IS NOT DISTINCT FROM OLD.check_status_code THEN
        RETURN NEW;
    END IF;
    IF NEW.check_status_code = 'pending' THEN
        NEW.checked_by_employee_id := NULL;
        NEW.checked_at := NULL;
        RETURN NEW;
    END IF;

    v_employee := sales.current_employee_id();
    IF v_employee IS NULL THEN
        RAISE EXCEPTION 'checks must be recorded by an identified employee (actor % is not one)',
            audit.current_actor() USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.check_status_code = 'waived' THEN
        PERFORM sales.require_permission('waive_check', format('waiving check %s', NEW.check_type_code));
    END IF;
    IF NEW.check_type_code = 'margin_approval' AND NEW.check_status_code IN ('passed', 'waived') THEN
        PERFORM sales.require_permission('approve_loss', 'approving a loss-making order');
    END IF;
    NEW.checked_by_employee_id := v_employee;
    NEW.checked_at := now();
    RETURN NEW;
END
$$;
CREATE TRIGGER check_authority
    BEFORE INSERT OR UPDATE ON sales.sales_order_check
    FOR EACH ROW EXECUTE FUNCTION sales.tg_sales_order_check_authority();

-- Non-standard credit terms: privileged, and the approver is always the actor.
CREATE FUNCTION sales.tg_credit_terms_authority() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.is_non_standard AND (TG_OP = 'INSERT' OR ROW(NEW.*) IS DISTINCT FROM ROW(OLD.*)) THEN
        PERFORM sales.require_permission('approve_credit_terms', 'setting non-standard credit terms');
        NEW.approved_by_employee_id := sales.current_employee_id();
        NEW.approved_at := now();
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER credit_terms_authority
    BEFORE INSERT OR UPDATE ON sales.customer_credit_terms
    FOR EACH ROW EXECUTE FUNCTION sales.tg_credit_terms_authority();

-- ---------------------------------------------------------------- exceptions
-- As 0001, with the margin rules replaced: no minimum; losses need a rationale.
CREATE OR REPLACE VIEW sales.v_sales_order_exception AS
WITH s AS (SELECT * FROM sales.v_sales_order_summary),
     p AS (SELECT
             (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'max_fx_rate_age_days') AS max_fx_age,
             (SELECT numeric_value FROM sales.policy_setting WHERE setting_key = 'stated_total_tolerance') AS tolerance)
SELECT s.sales_order_id, s.order_number, 'NO_LINES' AS rule_code, 'error' AS severity,
       'Order has no lines' AS message
  FROM s WHERE s.line_count = 0
UNION ALL
SELECT l.sales_order_id, o.order_number, 'LOSS_LINE_NO_RATIONALE', 'error',
       format('Line %s (%s) sells below cost (margin %s) with no rationale', l.line_number, l.description, l.gross_margin)
  FROM sales.sales_order_line l JOIN sales.sales_order o USING (sales_order_id)
 WHERE l.gross_margin < 0 AND l.margin_rationale IS NULL AND o.margin_exception_reason IS NULL
UNION ALL
SELECT s.sales_order_id, s.order_number, 'ORDER_LOSS_NO_RATIONALE', 'error',
       format('Order sells below cost overall (margin %s) with no order-level rationale', s.gross_margin)
  FROM s JOIN sales.sales_order o USING (sales_order_id)
 WHERE s.gross_margin < 0 AND o.margin_exception_reason IS NULL
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
