UPDATE sales.customer_credit_terms SET recurring_payment_method_code = 'bank_transfer'
 WHERE recurring_payment_method_code = 'not_seen';
DELETE FROM sales.payment_method WHERE payment_method_code = 'not_seen';
