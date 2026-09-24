# ADR 0004 — Hosting alternatives to Azure

Status: **proposed**. Azure is on hold (CFO, 2026-09-24). Research date 2026-09-24; prices are
indicative and must be confirmed in each provider's calculator before committing.

The schema, code and tests are provider-neutral (plain PostgreSQL 16 + `btree_gist`). Only
`infra/main.bicep` is Azure-specific. Personal logins work on every option as named PostgreSQL roles in
`sales_orders_person`. What changes is **how people authenticate**:

| Option (London) | Personal login for approvals | PITR | Indicative / month | Notes |
|---|---|---|---|---|
| Azure PostgreSQL Flexible (UK South) — on hold | **Microsoft Entra ID** (native) | up to 35 days | low tens of £ | Built and CI-checked (`infra/`) |
| AWS RDS PostgreSQL (eu-west-2) | Password or AWS IAM token; Entra only via AWS Identity Center for the console | up to 35 days | ~$30–45 | UK GDPR addendum; mature Terraform |
| Google Cloud SQL (europe-west2) | **Entra users via Workforce Identity Federation** | 7 days (Enterprise) / 35 (Enterprise Plus) | ~$55–70 | Best non-Azure identity fit |
| DigitalOcean Managed PG (LON1) | Password only | 7 days | ~$15 | Cheapest credible; single node |
| Aiven (aws-eu-west-2) | Password only | 14–30 days on Business plan | ~$200+ | Startup plan PITR too short |
| Neon / Supabase | Password only | 7–30 days | $20–130 | Poorer fit (serverless / add-on pricing) |
| Managed247's own data centre | Password, or Kerberos/LDAP to own AD | whatever is built (pgBackRest) | internal cost + staff time | Operating burden and single-site risk |

**Consequence of leaving Entra.** With password logins, a leaver's database access is not removed when
their Microsoft account is disabled; it must be removed by hand (add to the leaver routine) and passwords
rotated. Only Azure (native) and Google Cloud SQL (federation) keep "disable in Entra = access gone".

**Recommendation.** If Azure stays off the table: Google Cloud SQL if Entra-backed personal logins are
required; otherwise AWS RDS. DigitalOcean only as a low-cost interim.
