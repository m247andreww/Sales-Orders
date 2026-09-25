// =============================================================================
// The nightly sync job (ADR 0005): a scheduled Azure Container Apps job next to the database.
//
//   * Private network: the job runs in its own subnet and reaches the database through a private
//     endpoint, so the database firewall stays closed to the internet.
//   * No passwords in configuration: the job's managed identity reads them from Key Vault.
//   * The image is built from this repository inside Azure (ACR Tasks) by deploy.sh.
//   * Alert: emails the CFO when a day passes without a successful run.
// =============================================================================

param location string
param environmentName string
param tags object
param logsWorkspaceName string
param serverName string
param keyVaultName string
param alertEmail string

@description('Container image for the job, e.g. <registry>.azurecr.io/sales-orders:<commit>. Empty: build the rest, no job yet.')
param jobImage string = ''

@description('Nightly run time, UTC (02:15 UTC = 02:15 GMT / 03:15 BST).')
param cronExpression string = '15 2 * * *'

param registerSheetId string
param registerRange string
param referenceSheetId string

var suffix = uniqueString(resourceGroup().id)

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logsWorkspaceName
}

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: serverName
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// ------------------------------------------------------------------ private network
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-salesorders-${environmentName}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.42.0.0/24'] }
    subnets: [
      {
        name: 'snet-jobs'
        properties: {
          addressPrefix: '10.42.0.0/27'
          delegations: [{ name: 'container-apps', properties: { serviceName: 'Microsoft.App/environments' } }]
        }
      }
      {
        name: 'snet-private-endpoints'
        properties: { addressPrefix: '10.42.0.32/28' }
      }
    ]
  }
}

resource privateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource privateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: privateDns
  name: 'link-${vnet.name}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

resource dbEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${serverName}'
  location: location
  tags: tags
  properties: {
    subnet: { id: vnet.properties.subnets[1].id }
    privateLinkServiceConnections: [
      {
        name: 'postgres'
        properties: { privateLinkServiceId: server.id, groupIds: ['postgresqlServer'] }
      }
    ]
  }
}

resource dbEndpointDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: dbEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{ name: 'postgres', properties: { privateDnsZoneId: privateDns.id } }]
  }
}

// ------------------------------------------------------------------ image registry and identity
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'crsalesorders${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource jobIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-salesorders-job-${environmentName}'
  location: location
  tags: tags
}

var acrPullRole = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var keyVaultSecretsUserRole = '4633458b-17de-408a-b874-0445c86b69e6'

resource pullImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, jobIdentity.id, acrPullRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRole)
    principalId: jobIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource readSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, jobIdentity.id, keyVaultSecretsUserRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRole)
    principalId: jobIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ------------------------------------------------------------------ job environment and job
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-salesorders-${environmentName}'
  location: location
  tags: tags
  properties: {
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    vnetConfiguration: {
      infrastructureSubnetId: vnet.properties.subnets[0].id
      internal: true
    }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

var secretNames = {
  'pg-password': 'sales-orders-app-password'
  'google-key': 'google-service-account'
  'xero-client-id': 'xero-client-id'
  'xero-client-secret': 'xero-client-secret'
}

resource job 'Microsoft.App/jobs@2024-03-01' = if (!empty(jobImage)) {
  name: 'caj-salesorders-nightly'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${jobIdentity.id}': {} }
  }
  dependsOn: [pullImages, readSecrets, dbEndpointDns, privateDnsLink]
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 1800
      replicaRetryLimit: 1
      registries: [{ server: registry.properties.loginServer, identity: jobIdentity.id }]
      secrets: [
        for s in items(secretNames): {
          name: s.key
          keyVaultUrl: '${vault.properties.vaultUri}secrets/${s.value}'
          identity: jobIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'nightly-sync'
          image: jobImage
          command: ['sales-orders', 'nightly-sync']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'PGHOST', value: server.properties.fullyQualifiedDomainName }
            { name: 'PGDATABASE', value: 'sales_orders' }
            { name: 'PGUSER', value: 'sales_orders_app' }
            { name: 'PGSSLMODE', value: 'require' }
            { name: 'PGPASSWORD', secretRef: 'pg-password' }
            { name: 'SALES_ORDERS_ACTOR', value: 'nightly-sync' }
            { name: 'SALES_ORDERS_REGISTER_SHEET_ID', value: registerSheetId }
            { name: 'SALES_ORDERS_REGISTER_RANGE', value: registerRange }
            { name: 'SALES_ORDERS_REFERENCE_SHEET_ID', value: referenceSheetId }
            { name: 'SALES_ORDERS_GOOGLE_KEY', secretRef: 'google-key' }
            { name: 'SALES_ORDERS_XERO_CLIENT_ID', secretRef: 'xero-client-id' }
            { name: 'SALES_ORDERS_XERO_CLIENT_SECRET', secretRef: 'xero-client-secret' }
          ]
        }
      ]
    }
  }
}

// ------------------------------------------------------------------ alert: no successful run in a day
resource alertEmailGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-salesorders-${environmentName}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'salesorders'
    enabled: true
    emailReceivers: [{ name: 'cfo', emailAddress: alertEmail, useCommonAlertSchema: true }]
  }
}

resource missedRunAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = if (!empty(jobImage)) {
  name: 'alert-salesorders-nightly-missed'
  location: location
  tags: tags
  properties: {
    displayName: 'Sales Orders nightly sync: no successful run in the last day'
    description: 'The nightly sync prints NIGHTLY SYNC OK when every step succeeds. Check: sales-orders job-status.'
    severity: 2
    enabled: true
    scopes: [logs.id]
    evaluationFrequency: 'PT6H'
    windowSize: 'P1D'
    skipQueryValidation: true // the log table appears only after the first run
    criteria: {
      allOf: [
        {
          query: 'ContainerAppConsoleLogs_CL | where Log_s has "NIGHTLY SYNC OK"'
          timeAggregation: 'Count'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    actions: { actionGroups: [alertEmailGroup.id] }
    autoMitigate: true
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output jobName string = empty(jobImage) ? '' : job.name
