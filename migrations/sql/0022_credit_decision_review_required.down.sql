-- Reverse 0022.
DROP TRIGGER credit_decision_review_required ON sales.customer_credit_limit;
DROP FUNCTION sales.tg_credit_decision_review_required();
