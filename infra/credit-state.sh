#!/usr/bin/env bash
# =============================================================================
# Create the private store that carries the credit database between runs of the daily Claude job
# (ADR 0005, redesign 2026-10-02). Run once from Azure Cloud Shell:
#
#   cd Sales-Orders && git pull && bash infra/credit-state.sh
#
# Safe to run again. It creates, in rg-salesorders-prod (UK South):
#   * a storage account: private (no public access), TLS 1.2+, HTTPS only, keys never shown;
#   * blob versioning (every saved state is kept) and 30-day soft delete (deleted states recoverable);
#   * one container, credit-state;
#   * an access link (SAS) for that container only: read/write/create, HTTPS only, valid 12 months.
# It prints the access link ONCE, for the Claude environment's credentials (SALES_ORDERS_STATE_URL).
# Nothing in Microsoft 365 is touched.
# =============================================================================
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-salesorders-prod}"
LOCATION="uksouth"
CONTAINER="credit-state"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mSTOPPED:\033[0m %s\nNothing after this point was changed. Send a screenshot of this screen.\n' "$*" >&2; exit 1; }

command -v az > /dev/null || fail "run this in Azure Cloud Shell (the az tool is missing)"
az account show -o none 2> /dev/null || fail "not signed in to Azure (Cloud Shell signs you in automatically)"
az group show -n "$RESOURCE_GROUP" -o none 2> /dev/null || fail "resource group $RESOURCE_GROUP not found (run infra/deploy.sh first)"

step "Storage account"
ACCOUNT="$(az storage account list -g "$RESOURCE_GROUP" --query "[?tags.purpose=='credit-state'].name | [0]" -o tsv)"
if [[ -z "$ACCOUNT" ]]; then
    ACCOUNT="stsocredit$(openssl rand -hex 4)"
    az storage account create -g "$RESOURCE_GROUP" -n "$ACCOUNT" -l "$LOCATION" \
        --sku Standard_GRS --kind StorageV2 --https-only true --min-tls-version TLS1_2 \
        --allow-blob-public-access false --allow-shared-key-access true \
        --tags purpose=credit-state system=sales-orders -o none
fi
echo "storage account: $ACCOUNT"

step "Keep every version; recover deletions for 30 days"
az storage account blob-service-properties update -g "$RESOURCE_GROUP" -n "$ACCOUNT" \
    --enable-versioning true --enable-delete-retention true --delete-retention-days 30 \
    --enable-container-delete-retention true --container-delete-retention-days 30 -o none

step "Container"
KEY="$(az storage account keys list -g "$RESOURCE_GROUP" -n "$ACCOUNT" --query "[0].value" -o tsv)"
az storage container create --account-name "$ACCOUNT" --account-key "$KEY" -n "$CONTAINER" \
    --public-access off -o none

step "Access link for the daily job (this container only, 12 months)"
EXPIRY="$(date -u -d '+12 months' '+%Y-%m-%dT%H:%MZ')"
SAS="$(az storage container generate-sas --account-name "$ACCOUNT" --account-key "$KEY" -n "$CONTAINER" \
    --permissions rcw --expiry "$EXPIRY" --https-only -o tsv)"
unset KEY

printf '\n\033[1mDONE.\033[0m Copy the line below into Claude (environment settings -> credentials)\n'
printf 'as SALES_ORDERS_STATE_URL. Do not paste it into a chat or email. It expires %s.\n\n' "$EXPIRY"
printf 'https://%s.blob.core.windows.net/%s?%s\n\n' "$ACCOUNT" "$CONTAINER" "$SAS"
printf 'Also allow this website in the Claude environment network settings: %s.blob.core.windows.net\n' "$ACCOUNT"
