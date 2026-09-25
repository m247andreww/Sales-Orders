# ADR 0005: Nightly sync job in Azure

Status: **accepted** (CFO, 25 Sep 2026: option A, "nightly Azure job").

## Context

The production database (ADR 0004) is empty and closed to the internet. Its data comes from several
places, and each place stays the master of its own data:

- the AW SOs Register (Google Sheets) for SN references and the account history;
- Xero for the chart of accounts, customers and account-owner groups;
- the CFO-maintained **Sales Orders - Reference** sheet (Google Sheets) for staff, Register labels,
  Xero owner groups, last working days and account moves.

Microsoft 365 is not used as the staff source (CFO: no admin permissions), so staff data is kept in
the Reference sheet. It sits on the same Google account as AW SOs, which is already an accepted risk.

## Decision

A scheduled **Azure Container Apps job** (`caj-salesorders-nightly`, 02:15 UTC) runs
`sales-orders nightly-sync`:

| Step | What it does |
|---|---|
| reference-staff | Loads staff, Register labels and Xero owner groups; applies last working days (leaver routine) |
| xero-chart-of-accounts | Mirrors the 4-digit GL codes; archived accounts are marked inactive |
| xero-customers | Adds and renames customers by Xero contact id; archived contacts are marked, never deleted |
| register | Reloads the Register mirror, then sources SNs for waiting orders |
| account-history | Builds ownership history for customers that have none; applies leaving dates |
| account-moves | Applies the Reference sheet's Account moves tab (idempotent) |
| xero-owner-groups | Reconciles owner groups (Xero wins only where it is newer; ADR 0003) |

Each step runs in its own transaction and is logged in `sales.job_run` / `sales.job_run_step`. A
failed step does not stop the others, but it fails the run.

## Security

- **Private network.** The job runs in a private subnet and reaches the database through a private
  endpoint. The database firewall stays closed.
- **No passwords in configuration.** The job's managed identity reads four secrets from Key Vault: the
  application database password, the Google service-account key and the two Xero codes.
- **Least privilege.** The job signs in as `sales_orders_app`, which can read, add and change data but
  never delete it. Google access is Viewer on the two sheets only; Xero access is read-only.
- **The image.** It is built from the repository inside Azure (`az acr build`), from a copy of the
  Python base image held in the project's own registry, because Docker Hub rate-limits shared build
  machines. It runs as a non-root user.

## Monitoring

The job prints `NIGHTLY SYNC OK` only when every step succeeds. An Azure Monitor alert emails the CFO
if a day passes without that line. `sales-orders job-status` shows the latest run step by step.

## Cost

About £10–£12 a month [estimate]: registry (Basic) about £4, private endpoint about £6, and the job
itself pennies (a few minutes a night on the consumption plan).

## Consequences

- A change in a source reaches the database the next morning. For something urgent, run the job
  now from Cloud Shell: `az containerapp job start -n caj-salesorders-nightly -g rg-salesorders-prod`.
- Staff changes are made in the Reference sheet, never in the database.
- The Xero custom connection needs two read scopes: `accounting.contacts.read` and
  `accounting.settings.read` (for the chart of accounts).
