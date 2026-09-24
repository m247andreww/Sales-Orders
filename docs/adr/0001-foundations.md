# ADR 0001 — Foundations

Status: accepted (2026-09-24). Revisit any item by writing a new ADR that supersedes it.

## 1. PostgreSQL as the system of record
**Decision.** PostgreSQL 16. Production hosting is **not yet decided** — proposed: Azure Database for PostgreSQL – Flexible Server, UK region (see README "Decisions needed").
**Why.** Real constraints (CHECK, EXCLUDE, generated columns, triggers), exact `numeric` money,
first-class JSON for the audit log, portable, no licence cost, managed in the same Azure tenant as M365.
**Rejected.** SharePoint lists / Excel (no integrity constraints, no transactions, no audit guarantees);
Dataverse (licence cost, weaker relational constraints); SQL Server (viable, but no EXCLUDE constraints
and higher cost for the same outcome).

## 2. Business rules live in the database, not only in code
Anything that must always be true (totals, locking, workflow, approval gate, non-overlapping terms)
is enforced by PostgreSQL so it holds for every client: the CLI, a future web app, Power Automate,
or a person with a SQL tool. Python validation is the first line; the database is the last.

## 3. Money and rounding
`numeric(18,4)` unit prices, `numeric(18,2)` totals, round half away from zero (PostgreSQL `round`).
Unit cost in order currency rounded to 4 dp; line totals to 2 dp; order totals are sums of line totals
(matching how the existing pricing spreadsheet behaves). Floats are rejected at input.

## 4. Migrations: Alembic running reviewed SQL files
Alembic tracks versions; each revision executes a plain `.sql` file so DBAs and auditors can read the
schema without reading Python. Every migration has a tested `down`. Test runs migrate up → down → up.

## 5. Documents are referenced, not stored
PDFs stay in SharePoint/OneDrive. The database stores type, name, size, storage URI and SHA-256,
which proves the file has not changed since the order was recorded.

## 6. Idempotent ingestion keyed on the email Message-ID
Re-running an import is always safe: `(source_email, source_sequence)` is unique.
