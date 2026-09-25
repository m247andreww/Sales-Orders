// =============================================================================
// Sales Orders: production database on Azure Database for PostgreSQL – Flexible Server.
//
// Deploy with (see docs/deployment.md for the full runbook):
//   bash infra/deploy.sh      (Azure Cloud Shell; builds the server, roles, schema and permissions)
//
// Design (ADR 0002):
//   * UK South primary; geo-redundant backups (UK West pair), so all data stays in the UK.
//   * Microsoft Entra ID sign-in for people (the CFO signs in personally; the database then
//     records that login as the actor and allows approvals, see migration 0003).
//   * One password login for the application/migrations, its secret held in Key Vault.
//   * TLS 1.2+ enforced; public access restricted to named IP ranges only.
//   * btree_gist extension allow-listed (required by the credit-terms constraint).
//   * CanNotDelete lock: the server cannot be deleted without first removing the lock.
//   * The Entra administrator may read and set Key Vault secrets (the stored passwords).
//   * Monthly spending alert (budget) emailed to the Entra administrator.
// =============================================================================

targetScope = 'resourceGroup'

@description('Azure region. UK South keeps data in the UK.')
param location string = 'uksouth'

@description('Environment name used in resource names, e.g. prod or test.')
@allowed(['prod', 'test'])
param environmentName string = 'prod'

@description('Globally unique server name (lower case, 3-63 chars).')
@minLength(3)
@maxLength(63)
param serverName string = 'psql-salesorders-${environmentName}-${uniqueString(resourceGroup().id)}'

@description('Compute size. Burstable B1ms is sufficient for order volumes (~1,600 orders a year).')
param skuName string = 'Standard_B1ms'

@allowed(['Burstable', 'GeneralPurpose', 'MemoryOptimized'])
param skuTier string = 'Burstable'

@description('Storage in GB (auto-grow is enabled).')
@minValue(32)
param storageSizeGB int = 32

@description('Point-in-time restore window in days (7-35).')
@minValue(7)
@maxValue(35)
param backupRetentionDays int = 35

@description('Zone-redundant high availability. Doubles compute cost; not available on Burstable.')
param highAvailability bool = false

@description('Password login used ONLY by migrations and the application service.')
param administratorLogin string = 'sales_orders_owner'

@secure()
@description('Password for administratorLogin. Supplied at deploy time; stored in Key Vault.')
param administratorPassword string

@description('Object ID of the Entra ID user or group that administers the server.')
param entraAdminObjectId string

@description('Apply the CanNotDelete lock. deploy.sh turns it off only while it sets the server up.')
param applyDeleteLock bool = true

@description('Display name / UPN of the Entra ID administrator, e.g. andrew.whitford@managed.co.uk.')
param entraAdminPrincipalName string

@allowed(['User', 'Group', 'ServicePrincipal'])
param entraAdminPrincipalType string = 'User'

@description('Public IP ranges allowed to connect, e.g. [{ name: \'office\', start: \'203.0.113.10\', end: \'203.0.113.10\' }].')
param allowedIpRanges array = []

@description('Monthly spending alert for this resource group, in the billing currency (GBP).')
@minValue(1)
param monthlyBudget int = 50

@description('Who is emailed at 80% and 100% of the monthly budget.')
param budgetContactEmails array = [entraAdminPrincipalName]

@description('First day of the month the budget starts (defaults to this month).')
param budgetStartDate string = utcNow('yyyy-MM-01')

@description('Tags applied to every resource.')
param tags object = {
  application: 'sales-orders'
  environment: environmentName
  owner: 'finance'
  dataClassification: 'confidential-financial'
}

var databaseName = 'sales_orders'
var keyVaultName = take('kv-so-${environmentName}-${uniqueString(resourceGroup().id)}', 24)

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-salesorders-${environmentName}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 90
  }
}

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: '16'
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Enabled'
      tenantId: subscription().tenantId
    }
    storage: {
      storageSizeGB: storageSizeGB
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: 'Enabled'
    }
    highAvailability: {
      mode: highAvailability ? 'ZoneRedundant' : 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource entraAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: server
  name: entraAdminObjectId
  properties: {
    principalName: entraAdminPrincipalName
    principalType: entraAdminPrincipalType
    tenantId: subscription().tenantId
  }
}

resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'azure.extensions'
  properties: {
    value: 'BTREE_GIST'
    source: 'user-override'
  }
  dependsOn: [entraAdmin]
}

resource secureTransport 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'require_secure_transport'
  properties: {
    value: 'ON'
    source: 'user-override'
  }
  dependsOn: [extensions]
}

resource minTls 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'ssl_min_protocol_version'
  properties: {
    value: 'TLSv1.2'
    source: 'user-override'
  }
  dependsOn: [secureTransport]
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8' // the documented Azure default; sorts English text as en_GB does
  }
  dependsOn: [minTls]
}

@batchSize(1)
resource firewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = [
  for range in allowedIpRanges: {
    parent: server
    name: range.name
    properties: {
      startIpAddress: range.start
      endIpAddress: range.end
    }
    dependsOn: [database]
  }
]

resource serverLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: server
  name: 'to-log-analytics'
  properties: {
    workspaceId: logs.id
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

resource ownerSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'sales-orders-owner-password'
  properties: {
    value: administratorPassword
    contentType: 'PostgreSQL password for ${administratorLogin}'
  }
}

// Key Vault Secrets Officer for the Entra administrator: read the stored passwords, set new ones.
resource vaultSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, entraAdminObjectId, 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
    )
    principalId: entraAdminObjectId
    principalType: entraAdminPrincipalType
  }
}

// Spending alert: emails at 80% (actual) and 100% (forecast) of the monthly budget.
resource budget 'Microsoft.Consumption/budgets@2023-05-01' = {
  name: 'sales-orders-monthly'
  properties: {
    category: 'Cost'
    amount: monthlyBudget
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: budgetContactEmails
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: budgetContactEmails
      }
    }
  }
}

resource deleteLock 'Microsoft.Authorization/locks@2020-05-01' = if (applyDeleteLock) {
  scope: server
  name: 'protect-sales-orders-database'
  properties: {
    level: 'CanNotDelete'
    notes: 'Financial system of record. Remove only with CFO approval.'
  }
  dependsOn: [firewall, serverLogs]
}

output serverFqdn string = server.properties.fullyQualifiedDomainName
output databaseName string = databaseName
output keyVaultName string = vault.name
output ownerSecretName string = ownerSecret.name
