DROP VIEW sales.v_customer_payment_terms;
DROP FUNCTION sales.set_customer_payment_terms(bigint, integer, text, integer, boolean, text);

CREATE OR REPLACE FUNCTION sales.tg_credit_terms_authority() RETURNS trigger
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

DELETE FROM sales.policy_setting WHERE setting_key = 'credit_standard_terms_days';
