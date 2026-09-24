-- Reverses 0001. Destroys all sales and audit data: never run against production.
DROP SCHEMA IF EXISTS sales CASCADE;
DROP SCHEMA IF EXISTS audit CASCADE;
-- btree_gist is left installed: other schemas may depend on it.
