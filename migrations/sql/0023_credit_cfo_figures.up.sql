-- =============================================================================
-- 0023 First figures entered by the CFO (CFO, 2026-10-05: "build first figures").
-- Experian and Creditsafe only send an alert when something CHANGES, so a client added to monitoring has
-- no figure until its first change, and six clients owing about £700k have none at all. The CFO now reads
-- the limit (and Experian band) from each portal and enters it on the Credit Desk: a bureau reading with
-- source 'cfo_entry', tied to the client like a workbook import, and recorded with who entered it.
-- Rules held here: only someone with approve_credit_terms may enter figures; an Experian band must be one
-- of the known bands; a later alert always supersedes the entry (it is just an earlier dated reading).
-- =============================================================================

ALTER TABLE sales.credit_report ADD COLUMN entered_by text;

ALTER TABLE sales.credit_report DROP CONSTRAINT credit_report_source_code_check;
ALTER TABLE sales.credit_report ADD CONSTRAINT credit_report_source_code_check
    CHECK (source_code IN ('alert_email', 'workbook_import', 'cfo_entry'));

ALTER TABLE sales.credit_report DROP CONSTRAINT credit_report_check3;
ALTER TABLE sales.credit_report ADD CONSTRAINT credit_report_subject_link_check
    CHECK ((source_code IN ('workbook_import', 'cfo_entry')) = (credit_subject_id IS NOT NULL));
ALTER TABLE sales.credit_report ADD CONSTRAINT credit_report_entered_by_check
    CHECK ((source_code = 'cfo_entry') = (entered_by IS NOT NULL AND btrim(entered_by) <> ''));
ALTER TABLE sales.credit_report ADD CONSTRAINT credit_report_cfo_entry_limit_check
    CHECK (source_code <> 'cfo_entry' OR limit_status IN ('value', 'not_available'));

CREATE FUNCTION sales.tg_credit_report_cfo_entry() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_code = 'cfo_entry' THEN
        PERFORM sales.require_permission('approve_credit_terms', 'entering bureau figures');
        NEW.entered_by := audit.current_actor();  -- always the real login, never a value passed in
        IF NEW.risk_band IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM sales.credit_risk_band b
                WHERE b.bureau_code = NEW.bureau_code AND b.band = NEW.risk_band) THEN
            RAISE EXCEPTION 'unknown % band: %', NEW.bureau_code, NEW.risk_band
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER credit_report_cfo_entry BEFORE INSERT ON sales.credit_report
    FOR EACH ROW EXECUTE FUNCTION sales.tg_credit_report_cfo_entry();
