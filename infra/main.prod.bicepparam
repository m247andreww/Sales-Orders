using './main.bicep'

// Production parameters, used by infra/deploy.sh. Nothing secret or personal is stored here: the
// script supplies the Entra object id and a freshly generated password through the environment.
param environmentName = 'prod'
param location = 'uksouth'
param entraAdminPrincipalName = readEnvironmentVariable('SALES_ORDERS_ENTRA_ADMIN', 'andrew.whitford@managed.co.uk')
param entraAdminObjectId = readEnvironmentVariable('SALES_ORDERS_ENTRA_OBJECT_ID')
param administratorPassword = readEnvironmentVariable('SALES_ORDERS_OWNER_PASSWORD')
param applyDeleteLock = bool(readEnvironmentVariable('SALES_ORDERS_APPLY_LOCK', 'true'))
param jobImage = readEnvironmentVariable('SALES_ORDERS_JOB_IMAGE', '')
// Google Sheet ids (not secret: reading them needs the sharing granted to the job's Google account).
param registerSheetId = '1Jh5ZvYoGnXD73G8Erwy-fZJf-GmJfhYAN795eWuaXO0' // AW SOs
param registerRange = 'Register'
param referenceSheetId = '1_HSn7xqwmG9XJ1NMXO5FRQ8pNHTKvuNBuDtvqcA_2rM' // Sales Orders - Reference
param allowedIpRanges = [
  // Standing access (e.g. the office) goes here; deploy.sh opens a temporary rule for itself.
  // { name: 'office', start: '<office public IP>', end: '<office public IP>' }
]
