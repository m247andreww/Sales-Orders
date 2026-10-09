-- =============================================================================
-- 0028 "No recurring invoices seen" is a fact, not bank transfer (5 Oct 2026 lesson: never present inference as
-- fact). When the latest Xero invoices hold no RD/RI invoice for a customer, the register records 'not_seen' for
-- how recurring invoices are paid instead of guessing a method; their recurring days follow the one-off terms.
-- =============================================================================
INSERT INTO sales.payment_method (payment_method_code, description) VALUES
    ('not_seen', 'No recurring invoices seen in Xero');
