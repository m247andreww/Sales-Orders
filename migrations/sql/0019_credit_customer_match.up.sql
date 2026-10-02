-- =============================================================================
-- 0019 Researched matches for unmonitored ARR customers (CFO, 2026-10-02: "yes, fill numbers").
-- For each ARR prefix the scan lists, the Xero contact Managed247 invoices and the registered company
-- behind it, found from PandaDoc (signed New Customer Application Forms, agreements), the New Orders
-- emails, Xero and web search, with the source kept as evidence. The scan suggests these ahead of name
-- matching; a note (e.g. "in administration") is shown beside the customer. Nothing becomes a monitored
-- client until the CFO presses "I've added it".
-- =============================================================================

CREATE TABLE sales.credit_customer_match (
    arr_prefix      char(3) PRIMARY KEY CHECK (arr_prefix ~ '^[A-Z]{3}$'),
    xero_contact_id uuid    REFERENCES sales.xero_contact_directory,
    registered_name text    CHECK (registered_name IS NULL OR btrim(registered_name) <> ''),
    company_number  text    CHECK (company_number ~ '^[A-Z0-9]{8}$'),
    source          text    NOT NULL CHECK (btrim(source) <> ''),
    confidence      text    NOT NULL CHECK (confidence IN ('certain', 'likely')),
    note            text    CHECK (note IS NULL OR btrim(note) <> ''),
    written_to_xero_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text        NOT NULL DEFAULT audit.current_actor(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text        NOT NULL DEFAULT audit.current_actor(),
    row_version     integer     NOT NULL DEFAULT 1,
    CHECK (xero_contact_id IS NOT NULL OR company_number IS NOT NULL OR note IS NOT NULL)
);
SELECT sales.attach_standard_triggers('sales.credit_customer_match');
