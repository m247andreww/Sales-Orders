-- =============================================================================
-- 0022 Every CFO credit decision has a reason AND a review / follow-up date (CFO, 2026-10-05: "for EITHER
-- previous choices, i want to enter a reason and a review/follow up date"). The reason was already
-- required (0014); the review date is now required too, and must fall after the decision date (UK time).
-- Applies to decisions made from now on: earlier rows are untouched (a BEFORE INSERT rule, not a CHECK,
-- so closing an earlier decision's period still works).
-- =============================================================================

CREATE FUNCTION sales.tg_credit_decision_review_required() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_code = 'cfo_decision' THEN
        IF NEW.review_by IS NULL THEN
            RAISE EXCEPTION 'a CFO credit decision needs a review / follow-up date'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.review_by <= (NEW.effective_from AT TIME ZONE 'Europe/London')::date THEN
            RAISE EXCEPTION 'the review date (%) must be after the decision date', NEW.review_by
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER credit_decision_review_required BEFORE INSERT ON sales.customer_credit_limit
    FOR EACH ROW EXECUTE FUNCTION sales.tg_credit_decision_review_required();
