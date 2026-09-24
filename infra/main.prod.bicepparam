using './main.bicep'

// Production parameters. The password is NOT stored here: pass it at deploy time, e.g.
//   az deployment group create ... -p infra/main.prod.bicepparam -p administratorPassword="$(openssl rand -base64 32)"
param environmentName = 'prod'
param location = 'uksouth'
param entraAdminPrincipalName = 'andrew.whitford@managed.co.uk'
param entraAdminObjectId = '<Entra object id of Andrew Whitford: az ad user show --id andrew.whitford@managed.co.uk --query id -o tsv>'
param administratorPassword = readEnvironmentVariable('SALES_ORDERS_OWNER_PASSWORD')
param allowedIpRanges = [
  // { name: 'mk-office', start: '<office public IP>', end: '<office public IP>' }
]
