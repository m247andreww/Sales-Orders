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

Indicative cost [estimate, to confirm in the Azure pricing calculator]: B1ms compute + 32 GB storage +
geo-backup is typically in the low tens of pounds a month. High availability would roughly double compute.

## Steps

1. **Sign in and create the resource group** (once)
   ```bash
   az login
   az group create -n rg-salesorders-prod -l uksouth --tags application=sales-orders owner=finance
   ```
2. **Fill in `infra/main.prod.bicepparam`**: your Entra object id
   (`az ad user show --id andrew.whitford@managed.co.uk --query id -o tsv`) and the office IP range(s).
3. **Deploy** (the password is generated and never displayed or saved locally)
   ```bash
   export SALES_ORDERS_OWNER_PASSWORD="$(openssl rand -base64 32)"
   az deployment group create -g rg-salesorders-prod -f infra/main.bicep -p infra/main.prod.bicepparam
   unset SALES_ORDERS_OWNER_PASSWORD
   ```
4. **Create roles** (as the Entra administrator, connected to database `postgres`)
   ```bash
   psql "host=<serverFqdn> dbname=postgres user=andrew.whitford@managed.co.uk sslmode=require" \
        -v db=sales_orders -f db/bootstrap/roles.sql
   ```
   Then create your personal database login and put it in the people group:
   ```sql
   SELECT * FROM pgaadauth_create_principal('andrew.whitford@managed.co.uk', false, false);
   GRANT sales_orders_person TO "andrew.whitford@managed.co.uk";
   ```
   Set passwords for `sales_orders_app` / `sales_orders_readonly` into Key Vault, never into files.
5. **Build the schema** (as `sales_orders_owner`, password from Key Vault)
   ```bash
   export SALES_ORDERS_DATABASE_URL="postgresql://sales_orders_owner:<from Key Vault>@<serverFqdn>:5432/sales_orders?sslmode=require"
   sales-orders migrate
   psql "$SALES_ORDERS_DATABASE_URL" -f db/bootstrap/grants.sql
   ```
   Switch on production identity mode (from now on only your personal login can approve):
   ```bash
   psql "$SALES_ORDERS_DATABASE_URL" -c "UPDATE sales.policy_setting SET numeric_value = 1 WHERE setting_key = 'require_personal_login'"
   ```
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
