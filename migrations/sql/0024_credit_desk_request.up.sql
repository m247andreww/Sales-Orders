-- =============================================================================
-- 0024 Each Credit Desk button press is applied once (5 Oct 2026).
-- Every press starts its own decision job, each on its own copy of the database. When the CFO saved 18
-- sets of first figures in ten minutes, jobs overlapped: a job that restored the database after another
-- job's save, but before that job marked the request done on the page, applied it again (same values,
-- so no wrong limit, but duplicate readings and decisions). The request id from the page is now
-- recorded in the same transaction as the change it made, so a later job finds it and skips it.
-- =============================================================================

CREATE TABLE sales.credit_desk_request (
    request_id   text        PRIMARY KEY CHECK (request_id ~ '^[A-Za-z0-9_-]{1,64}$'),  -- the page's document id
    kind_code    text        NOT NULL CHECK (kind_code IN ('decision', 'monitor', 'figures')),
    subject_text text        NOT NULL CHECK (btrim(subject_text) <> ''),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text        NOT NULL DEFAULT audit.current_actor()
);
COMMENT ON TABLE sales.credit_desk_request IS 'Credit Desk requests already applied (one row per page request; append-only).';
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON sales.credit_desk_request
    FOR EACH ROW EXECUTE FUNCTION sales.tg_append_only();
CREATE TRIGGER audit_row AFTER INSERT ON sales.credit_desk_request
    FOR EACH ROW EXECUTE FUNCTION audit.tg_log_change();
