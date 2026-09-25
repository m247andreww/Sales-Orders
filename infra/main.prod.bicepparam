using './main.bicep'

// Production parameters, used by infra/deploy.sh. Nothing secret or personal is stored here: the
// script supplies the Entra object id and a freshly generated password through the environment.
param environmentName = 'prod'
param location = 'uksouth'
param entraAdminPrincipalName = readEnvironmentVariable('SALES_ORDERS_ENTRA_ADMIN', 'andrew.whitford@managed.co.uk')
param entraAdminObjectId = readEnvironmentVariable('SALES_ORDERS_ENTRA_OBJECT_ID')
param administratorPassword = readEnvironmentVariable('SALES_ORDERS_OWNER_PASSWORD')
param applyDeleteLock = bool(readEnvironmentVariable('SALES_ORDERS_APPLY_LOCK', 'true'))
param allowedIpRanges = [
  // Standing access (e.g. the office) goes here; deploy.sh opens a temporary rule for itself.
  // { name: 'office', start: '<office public IP>', end: '<office public IP>' }
]
