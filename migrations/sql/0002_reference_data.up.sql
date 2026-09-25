-- =============================================================================
-- 0002 Reference data. Changes to these values go in a NEW migration, never an edit here.
-- =============================================================================

INSERT INTO sales.currency (currency_code, name, minor_units) VALUES
    ('GBP', 'Pound sterling', 2),
    ('USD', 'US dollar', 2),
    ('EUR', 'Euro', 2);

INSERT INTO sales.order_status (status_code, description, is_terminal, lines_locked, sort_order) VALUES
    ('received',     'Received into New Orders; not yet checked',                 false, false, 10),
    ('validated',    'Checked by finance; awaiting approval',                     false, false, 20),
    ('approved',     'Approved; commercial content frozen',                       false, true,  30),
    ('provisioning', 'Supplier orders placed / delivery in progress',             false, true,  40),
    ('invoiced',     'Invoiced in Xero',                                          false, true,  50),
    ('closed',       'Complete',                                                  true,  true,  60),
    ('on_hold',      'Stopped pending a query; must be re-validated to proceed',  false, false, 70),
    ('cancelled',    'Cancelled',                                                 true,  true,  80);

-- on_hold always returns to 'received' so a held order is re-validated before approval.
INSERT INTO sales.order_status_transition (from_status, to_status) VALUES
    ('received',     'validated'),
    ('received',     'on_hold'),
    ('received',     'cancelled'),
    ('validated',    'approved'),
    ('validated',    'received'),
    ('validated',    'on_hold'),
    ('validated',    'cancelled'),
    ('approved',     'provisioning'),
    ('approved',     'on_hold'),
    ('approved',     'cancelled'),
    ('provisioning', 'invoiced'),
    ('provisioning', 'on_hold'),
    ('invoiced',     'closed'),
    ('on_hold',      'received'),
    ('on_hold',      'cancelled');

INSERT INTO sales.order_type (order_type_code, description) VALUES
    ('new',        'New customer or new service'),
    ('additional', 'Additional quantity / services for an existing customer'),
    ('renewal',    'Renewal of an existing term'),
    ('amendment',  'Change to a previously processed order');

INSERT INTO sales.line_category (line_category_code, description, is_tax_pass_through) VALUES
    ('licence',               'Software licence / subscription (incl. Microsoft CSP)', false),
    ('hardware',              'Hardware',                                              false),
    ('professional_services', 'Professional services delivered by days / rate card',   false),
    ('managed_service',       'Recurring managed service',                             false),
    ('connectivity',          'Internet / network connectivity',                       false),
    ('third_party_labour',    'Installation or labour by a third party',               false),
    ('delivery',              'Delivery / shipping',                                   false),
    ('tax_pass_through',      'Tax charged by a supplier and recharged at cost',       true),
    ('other',                 'Other',                                                 false);

INSERT INTO sales.billing_frequency (billing_frequency_code, description, months_per_period, sort_order) VALUES
    ('one_off',   'One-off charge',       NULL, 10),
    ('monthly',   'Billed monthly',       1,    20),
    ('quarterly', 'Billed quarterly',     3,    30),
    ('annual',    'Billed annually',      12,   40);

INSERT INTO sales.payment_method (payment_method_code, description) VALUES
    ('direct_debit',  'Direct Debit'),
    ('bank_transfer', 'Bank transfer (BACS / Faster Payments)'),
    ('card',          'Card');

INSERT INTO sales.risk_rating (risk_rating_code, description, severity) VALUES
    ('standard', 'Standard credit risk',                                   1),
    ('elevated', 'Elevated risk: restricted terms apply',                  2),
    ('high',     'High risk (e.g. insolvency process): prepayment/DD only', 3);

INSERT INTO sales.document_type (document_type_code, description) VALUES
    ('signed_order',                'Customer-signed order / proposal / SOW'),
    ('customer_po',                 'Customer purchase order'),
    ('supplier_quote',              'Supplier quotation'),
    ('customer_application_form',   'Customer account application form'),
    ('supplier_terms_confirmation', 'Evidence of supplier payment terms'),
    ('direct_debit_mandate',        'Signed Direct Debit mandate'),
    ('fx_evidence',                 'Evidence of the FX rate used'),
    ('other',                       'Other supporting document');

INSERT INTO sales.check_type (check_type_code, description) VALUES
    ('signed_order_verified',     'Signed order matches the pricing submitted'),
    ('customer_po_received',      'Customer PO received where the customer requires one'),
    ('direct_debit_mandate',      'Direct Debit mandate in place'),
    ('gdap_relationship',         'Microsoft GDAP relationship approved'),
    ('customer_application_form', 'Customer account application form received'),
    ('supplier_account_set_up',   'Supplier account and terms in place'),
    ('credit_terms_confirmed',    'Order terms match the customer credit terms register'),
    ('margin_approval',           'Below-policy margin approved');

INSERT INTO sales.check_status (check_status_code, description, is_resolved) VALUES
    ('pending',        'Not yet checked',     false),
    ('passed',         'Checked and passed',  true),
    ('failed',         'Checked and failed',  false),
    ('waived',         'Waived (with note)',  true),
    ('not_applicable', 'Not applicable',      true);

INSERT INTO sales.supplier_account_status (status_code, description, can_order) VALUES
    ('approved',           'Account open, terms agreed',      true),
    ('pending_onboarding', 'Account / terms not yet in place', false),
    ('suspended',          'Do not order',                    false);

-- PLACEHOLDER VALUES: to be confirmed by the CFO before go-live (see README "Decisions needed").
INSERT INTO sales.policy_setting (setting_key, numeric_value, description) VALUES
    ('min_order_gm_pct',       20,   'Orders below this gross margin % need margin_exception_reason'),
    ('max_fx_rate_age_days',   7,    'FX rates older than this many days before receipt are flagged'),
    ('stated_total_tolerance', 0.05, 'Allowed difference (currency units) between emailed and computed totals');
