# ADR 0002 — Hosting and identity

Status: accepted (CFO, 2026-09-24).

**Hosting.** Azure Database for PostgreSQL – Flexible Server, UK South, built from `infra/main.bicep`
(infrastructure as code: reviewable, repeatable, compiled in CI). Geo-redundant backup to UK West keeps
all data and backups in the UK (UK GDPR). A delete lock protects the system of record.

**Identity.** Production mode is switched on per database by `policy_setting.require_personal_login = 1`
(migration 0005; not by the existence of a server-wide role, which would leak between databases on the same
server). The application cannot change it. People sign in with Microsoft Entra ID as themselves. Membership of `sales_orders_person`
makes the login the audited actor (it cannot be overridden by the application) and is required for
privileged actions. The shared application login can load and check orders but can never approve,
waive, approve a loss or set non-standard terms, even if it claims to be the CFO (tested).
Membership is checked explicitly: `pg_has_role()` treats administrators as members of every role.

**Authority.** CFO decision 3: only the CFO holds `approve_order`, `waive_check`, `approve_loss`,
`approve_credit_terms`. Permissions are granted by migration only, so granting a right is itself
reviewed and audited.

**Not yet decided.** Private networking (VNet/private endpoint) instead of IP allow-listing; where the
application and scheduled syncs will run (e.g. Azure Container Apps job).
