#!/usr/bin/env bash
# =============================================================================
# Build (or rebuild) the Sales Orders production database, end to end, from Azure Cloud Shell.
#
#   cd Sales-Orders && bash infra/deploy.sh
#
# Safe to run again: every step checks what already exists. It
#   1. checks you are signed in to the right tenant and subscription, and asks before changing anything;
#   2. builds the server, Key Vault and logs from infra/main.bicep (UK South);
#   3. opens the firewall to THIS Cloud Shell only, for the length of the run;
#   4. creates the database roles, stores their passwords in Key Vault (never on screen or disk);
#   5. builds the schema (migrations), applies permissions and switches on production identity mode;
#   6. closes the firewall again and puts the delete lock back on.
# =============================================================================
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-salesorders-prod}"
LOCATION="uksouth"
ENTRA_ADMIN="${SALES_ORDERS_ENTRA_ADMIN:-andrew.whitford@managed.co.uk}"
DATABASE="sales_orders"
FIREWALL_RULE="setup-cloud-shell"
LOCK_NAME="protect-sales-orders-database"
SERVER=""

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mSTOPPED:\033[0m %s\nNothing after this point was changed. Send a screenshot of this screen.\n' "$*" >&2; exit 1; }
need() { command -v "$1" > /dev/null || fail "the tool '$1' is not available in this shell"; }
new_password() { openssl rand -hex 24; }  # 48 hex characters: strong, and safe inside a connection string

cleanup() {
    if [[ -n "$SERVER" ]]; then
        az postgres flexible-server firewall-rule delete -g "$RESOURCE_GROUP" -n "$SERVER" \
            --rule-name "$FIREWALL_RULE" --yes -o none 2> /dev/null || true
    fi
}
trap cleanup EXIT

[[ -f infra/main.bicep ]] || fail "run this from the Sales-Orders folder (type: cd Sales-Orders)"
for tool in az psql python3 openssl curl; do need "$tool"; done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fail "Python 3.11 or newer is needed"

step "1/8 Checking who you are signed in as"
az account show -o none 2> /dev/null || fail "not signed in to Azure (type: az login)"
SIGNED_IN="$(az ad signed-in-user show --query userPrincipalName -o tsv)"
[[ "${SIGNED_IN,,}" == "${ENTRA_ADMIN,,}" ]] || fail "signed in as $SIGNED_IN; the database administrator must be $ENTRA_ADMIN"
export SALES_ORDERS_ENTRA_OBJECT_ID
SALES_ORDERS_ENTRA_OBJECT_ID="$(az ad signed-in-user show --query id -o tsv)"
SUBSCRIPTION="$(az account show --query name -o tsv)"
TENANT="$(az account show --query tenantDisplayName -o tsv 2> /dev/null || echo unknown)"
echo "Signed in as:  $SIGNED_IN"
echo "Organisation:  $TENANT"
echo "Subscription:  $SUBSCRIPTION   (the bill for the database goes here)"
echo "Will create:   resource group $RESOURCE_GROUP in UK South, with the database server, Key Vault and logs"
read -r -p "Type yes to continue: " answer
[[ "$answer" == "yes" ]] || fail "you did not type yes"

step "2/8 Switching on the Azure services the database needs"
for namespace in Microsoft.DBforPostgreSQL Microsoft.KeyVault Microsoft.OperationalInsights Microsoft.Insights; do
    az provider register --namespace "$namespace" --wait -o none
done
az group create -n "$RESOURCE_GROUP" -l "$LOCATION" --tags application=sales-orders owner=finance -o none

step "3/8 Building the server (first time: about 10-15 minutes, please leave this window open)"
SERVER="$(az postgres flexible-server list -g "$RESOURCE_GROUP" --query '[0].name' -o tsv 2> /dev/null || true)"
if [[ -n "$SERVER" ]]; then
    # A rebuild: lift the delete lock for the length of the run (it is put back in step 8).
    az lock delete -n "$LOCK_NAME" -g "$RESOURCE_GROUP" --resource "$SERVER" \
        --resource-type Microsoft.DBforPostgreSQL/flexibleServers -o none 2> /dev/null || true
fi
export SALES_ORDERS_OWNER_PASSWORD SALES_ORDERS_APPLY_LOCK=false
SALES_ORDERS_OWNER_PASSWORD="$(new_password)"
az deployment group create -g "$RESOURCE_GROUP" -n "sales-orders-$(date +%Y%m%d-%H%M%S)" \
    -f infra/main.bicep -p infra/main.prod.bicepparam --query properties.outputs -o json > /tmp/sales-orders-outputs.json
SERVER_FQDN="$(python3 -c 'import json; print(json.load(open("/tmp/sales-orders-outputs.json"))["serverFqdn"]["value"])')"
VAULT="$(python3 -c 'import json; print(json.load(open("/tmp/sales-orders-outputs.json"))["keyVaultName"]["value"])')"
SERVER="${SERVER_FQDN%%.*}"
echo "Server: $SERVER_FQDN   Key Vault: $VAULT"

step "4/8 Opening the firewall to this Cloud Shell only (closed again at the end)"
MY_IP="$(curl -fsS https://api.ipify.org)"
az postgres flexible-server firewall-rule create -g "$RESOURCE_GROUP" -n "$SERVER" --rule-name "$FIREWALL_RULE" \
    --start-ip-address "$MY_IP" --end-ip-address "$MY_IP" -o none

owner_psql() {  # psql as the owner login; the password comes from the environment, never the command line
    PGPASSWORD="$SALES_ORDERS_OWNER_PASSWORD" psql "host=$SERVER_FQDN user=sales_orders_owner sslmode=require dbname=$1" \
        -v ON_ERROR_STOP=1 -q "${@:2}"
}
for _ in $(seq 1 20); do owner_psql postgres -c 'SELECT 1' > /dev/null 2>&1 && break; sleep 15; done
owner_psql postgres -c 'SELECT 1' > /dev/null || fail "cannot reach the database server from Cloud Shell"

step "5/8 Creating database roles and storing their passwords in Key Vault"
owner_psql postgres -v db="$DATABASE" -f db/bootstrap/roles.sql
store_secret() {  # retries: Key Vault permission can take a few minutes to arrive after step 3
    for _ in $(seq 1 20); do
        az keyvault secret set --vault-name "$VAULT" -n "$1" --value "$2" -o none 2> /dev/null && return 0
        sleep 15
    done
    fail "could not save $1 in Key Vault (permission not yet active? run the script again in 10 minutes)"
}
for role in app readonly; do
    password="$(new_password)"
    store_secret "sales-orders-$role-password" "$password"
    owner_psql postgres -v role="sales_orders_$role" -v pw="$password" <<< "ALTER ROLE :\"role\" PASSWORD :'pw';"
done
# Your personal (Entra ID) database login: Azure normally creates it with the administrator setting;
# if not, create it signed in as you. Then put it in the people group, which approvals require.
if [[ "$(owner_psql postgres -tA -v person="$ENTRA_ADMIN" <<< "SELECT count(*) FROM pg_roles WHERE rolname = :'person';")" != "1" ]]; then
    PGPASSWORD="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)" \
        psql "host=$SERVER_FQDN user=$ENTRA_ADMIN sslmode=require dbname=postgres" -v ON_ERROR_STOP=1 -q \
        -v person="$ENTRA_ADMIN" <<< "SELECT * FROM pgaadauth_create_principal(:'person', false, false);"
fi
owner_psql postgres -v person="$ENTRA_ADMIN" <<< 'GRANT sales_orders_person TO :"person";'

step "6/8 Building the schema (all migrations) and applying permissions"
python3 -m venv /tmp/sales-orders-venv
/tmp/sales-orders-venv/bin/pip install -q -e .
SALES_ORDERS_DATABASE_URL="postgresql://sales_orders_owner:${SALES_ORDERS_OWNER_PASSWORD}@${SERVER_FQDN}:5432/${DATABASE}?sslmode=require" \
    /tmp/sales-orders-venv/bin/sales-orders migrate
owner_psql "$DATABASE" -f db/bootstrap/grants.sql

step "7/8 Switching on production identity mode (only your personal login can approve)"
owner_psql "$DATABASE" -c "UPDATE sales.policy_setting SET numeric_value = 1 WHERE setting_key = 'require_personal_login'"

step "8/8 Closing the firewall and putting the delete lock on"
cleanup
SALES_ORDERS_APPLY_LOCK=true az deployment group create -g "$RESOURCE_GROUP" -n "sales-orders-lock-$(date +%Y%m%d-%H%M%S)" \
    -f infra/main.bicep -p infra/main.prod.bicepparam -o none
unset SALES_ORDERS_OWNER_PASSWORD
rm -f /tmp/sales-orders-outputs.json

printf '\n\033[32mDONE.\033[0m The Sales Orders database is built.\n'
echo "  Server:     $SERVER_FQDN"
echo "  Key Vault:  $VAULT (holds the three service passwords)"
echo "  Protection: delete lock on; firewall closed; backups kept 35 days, copied to UK West"
echo "Copy these three lines back to Claude."
