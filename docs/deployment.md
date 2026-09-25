# Deployment runbook (Azure, production)

Step by step. Each step is one command, run by an Azure administrator for Managed247's tenant
unless stated. Nothing here is manual clicking in the portal.

## What gets built

| Resource | Purpose |
|---|---|
| PostgreSQL 16 Flexible Server, **UK South** | The database. Burstable B1ms, 32 GB auto-grow |
| Geo-redundant backups (UK West), 35 days | Point-in-time restore; data stays in the UK |
| Entra ID administrator = Andrew Whitford | You sign in as yourself; approvals require it |
| Key Vault | Holds the one service password (never in git or email) |
| Log Analytics | Server logs and metrics, 90 days |
| Delete lock | The server cannot be deleted without removing the lock first |
| Budget alert | Emails the CFO at 80% of £50 a month (actual) and 100% (forecast) |

Indicative cost [estimate, to confirm in the Azure pricing calculator]: B1ms compute + 32 GB storage +
geo-backup is typically in the low tens of pounds a month. High availability would roughly double compute.

## Steps

The CFO's step-by-step version is `docs/owner-guides/azure-setup.md`. In short, from Azure Cloud Shell:

```bash
gh auth login
gh repo clone m247andreww/Sales-Orders && cd Sales-Orders
bash infra/deploy.sh
```

`infra/deploy.sh` (safe to re-run) does steps 1–5 below:

1. Checks the signed-in user is the Entra administrator and confirms the subscription.
2. Registers the resource providers and creates `rg-salesorders-prod` in UK South.
3. Deploys `infra/main.bicep` with a newly generated owner password (never shown or saved to disk;
   stored in Key Vault). The delete lock is lifted for the run and put back at the end.
4. Opens the firewall to the Cloud Shell's IP only, runs `db/bootstrap/roles.sql`, sets the app and
   read-only passwords straight into Key Vault, and adds the CFO's Entra login to `sales_orders_person`.
5. Runs the migrations and `grants.sql`, switches on `require_personal_login`, closes the firewall and
   re-applies the lock.

6. **Load master data**: GL chart from Xero, customers, suppliers, products, ARR contracts, then
   the AW SOs Register (`sales-orders load-register <export.csv> --source <sheet id>`).
7. **Connect Xero for account owners** (read-only, no browser login each time):
   1. At developer.xero.com, sign in as a Xero admin, *New app* → *Custom connection*.
   2. Give it the read scope for contacts only (`accounting.contacts.read`) and authorise it for
      Managed247's organisation (Xero emails the authorising admin to confirm; custom connections are
      a paid Xero add-on).
   3. Put the client id and secret in Key Vault as `xero-client-id` / `xero-client-secret`; the job
      exports them as `SALES_ORDERS_XERO_CLIENT_ID` / `SALES_ORDERS_XERO_CLIENT_SECRET`.
   4. Map each owner group in master data (`xero_owner_groups`: group name → salesperson email or House).
   5. First run: `sales-orders sync-xero-groups`, review the differences, then
      `sales-orders sync-xero-groups --adopt-xero` once the CFO confirms Xero is right. Then daily.

## Signing in as yourself (approvals)

Approvals only work from your personal Entra login. Get a token and connect:
```bash
export PGPASSWORD="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"
export SALES_ORDERS_DATABASE_URL="postgresql://andrew.whitford%40managed.co.uk@<serverFqdn>:5432/sales_orders?sslmode=require"
sales-orders status SN260534 approved --reason "..."   # SN or SO- number both work
```

## Restore

Point-in-time restore creates a *new* server (the original is untouched):
`az postgres flexible-server restore -g rg-salesorders-prod -n <new-name> --source-server <server> --restore-time <ISO time>`.
Test a restore quarterly; an untested backup is not a backup.
