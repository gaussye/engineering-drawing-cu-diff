#requires -Version 7.2
<#
.SYNOPSIS
Prepare or explicitly execute a dedicated, single-instance App Service deployment.
.DESCRIPTION
Without -Execute this script performs no Azure calls. Execution additionally requires
-ApprovePaidResources, including on reruns. It never deletes resources, creates AI
analyzers, changes model deployments, or prints configuration/credentials.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9_.()-]{1,90}$')][string]$ResourceGroup,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]+$')][string]$Location,
    [Parameter(Mandatory)][ValidatePattern('^[a-z][a-z0-9-]{2,49}$')][string]$AppName,
    [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9-]{3,60}$')][string]$PlanName,
    [Parameter(Mandatory)][ValidatePattern('^/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]+/providers/Microsoft.CognitiveServices/accounts/[^/]+$')][string]$AiResourceId,
    [Parameter(Mandatory)][guid]$TenantId,
    [Parameter(Mandatory)][guid]$OperatorObjectId,
    [Parameter(Mandatory)][guid]$DeploymentId,
    [string]$ConfigFile = 'local\config.json',
    [string]$Python = 'python',
    [switch]$Execute,
    [switch]$ApprovePaidResources
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$owner = "cu-review:$DeploymentId"
$identityName = "$AppName-auth"
$registrationName = "$AppName-auth-$DeploymentId"
$base = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"
$siteId = "$base/providers/Microsoft.Web/sites/$AppName"
$planId = "$base/providers/Microsoft.Web/serverfarms/$PlanName"
$identityId = "$base/providers/Microsoft.ManagedIdentity/userAssignedIdentities/$identityName"
$arm = 'https://management.azure.com'
$webVersion = '2023-12-01'
$work = $null
$siteTouched = $false

if (-not $Execute) {
    Write-Host "PREVIEW ONLY: no Azure calls, resources, role assignments, or deployments."
    Write-Host "Subscription: $SubscriptionId; group: $ResourceGroup; region: $Location"
    Write-Host "Dedicated Linux B2 plan: $PlanName; one instance; app: $AppName"
    Write-Host "AI role scope: $AiResourceId"
    Write-Host "Single tenant: $TenantId; permitted operator: $OperatorObjectId"
    Write-Host "Ownership marker: $owner"
    Write-Host "Execution requires BOTH -Execute and -ApprovePaidResources."
    return
}
if (-not $ApprovePaidResources) {
    throw 'Paid-resource approval is required. No Azure calls were made.'
}
foreach ($id in @($SubscriptionId, $TenantId, $OperatorObjectId, $DeploymentId)) {
    if ($id -eq [guid]::Empty) { throw 'Empty GUID parameters are forbidden.' }
}

function Invoke-Az([string[]]$Arguments) {
    # Never echo command arguments or raw errors: app settings may contain private data.
    $result = & az @Arguments --only-show-errors --output json 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI operation failed ($($Arguments[0])). Inspect Azure activity logs; do not enable secret-bearing debug logs."
    }
    if ($result) { return ($result -join "`n" | ConvertFrom-Json -AsHashtable) }
}

function Invoke-Json([string]$Method, [string]$Url, $Body = $null) {
    $arguments = @('rest', '--method', $Method, '--url', $Url)
    if ($null -ne $Body) {
        $path = Join-Path $work 'request.json'
        [IO.File]::WriteAllText($path, ($Body | ConvertTo-Json -Depth 40 -Compress))
        $arguments += @('--body', "@$path", '--headers', 'Content-Type=application/json')
    }
    return Invoke-Az $arguments
}

function Assert-Owned($Resource, [string]$ExpectedId) {
    if ($null -eq $Resource) { return }
    if ($Resource.id -ine $ExpectedId -or -not $Resource['tags'] -or $Resource.tags['cuDeploymentOwner'] -cne $owner) {
        throw "Refusing to change an unowned resource: $ExpectedId"
    }
    if ($Resource.location.Replace(' ', '') -ine $Location) {
        throw "Existing resource location differs: $ExpectedId"
    }
}

function Assert-BasicQuota([object[]]$Quotas) {
    $matches = @($Quotas | Where-Object {
        $_['properties'] -and $_.properties['name'] -and $_.properties.name['value'] -ieq 'B2'
    })
    if ($matches.Count -ne 1) {
        throw 'Cannot identify exactly one B2 quota. Ask the subscription owner to verify Microsoft.Web Basic SKU quota in the selected region; no resources were changed.'
    }
    $limit = $matches[0].properties['limit']
    $value = 0L
    if (-not $limit -or -not [long]::TryParse([string]$limit['value'], [ref]$value) -or $value -le 0) {
        throw 'Selected region has zero or unavailable B2 quota. Ask the subscription owner to verify/approve quota and region separately; this script will not request quota or provision resources.'
    }
    # Quota entitlement is not available capacity. In particular, usage -1 means
    # unavailable telemetry, never spare capacity or a negative consumption.
}

function Protect-Directory([string]$Path) {
    $null = New-Item -ItemType Directory -Path $Path
    if ($IsWindows) {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $acl.SetOwner($sid)
        $acl.SetAccessRuleProtection($true, $false)
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $Path -AclObject $acl
    } else {
        [IO.File]::SetUnixFileMode($Path, [IO.UnixFileMode]::UserRead -bor
            [IO.UnixFileMode]::UserWrite -bor [IO.UnixFileMode]::UserExecute)
    }
}

try {
    $null = Get-Command az -ErrorAction Stop
    $null = Get-Command $Python -ErrorAction Stop
    $configPath = if ([IO.Path]::IsPathRooted($ConfigFile)) { $ConfigFile } else { Join-Path $root $ConfigFile }
    try {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json -AsHashtable
    } catch {
        throw 'Cannot read runtime config as JSON. Contents suppressed.'
    }
    if ($config -isnot [System.Collections.IDictionary] -or -not $config['endpoint']) {
        throw 'Runtime config must be a JSON object with an endpoint.'
    }
    # Credentials belong to managed identity, never an uploaded runtime config.
    function Assert-NoSecrets($Value) {
        if ($Value -is [System.Collections.IDictionary]) {
            foreach ($key in $Value.Keys) {
                if ($key -match '(?i)(secret|password|token|api.?key|credential|connection.?string)') {
                    # Token-count limits are settings, not credentials.
                    if ($key -notin @('max_completion_tokens', 'max_tokens')) {
                        throw 'Remove credential-like fields from runtime config; deployment uses managed identity.'
                    }
                }
                Assert-NoSecrets $Value[$key]
            }
        } elseif ($Value -is [array]) {
            foreach ($entry in $Value) { Assert-NoSecrets $entry }
        }
    }
    Assert-NoSecrets $config
    $local = Join-Path $root 'local'
    if (-not (Test-Path -LiteralPath $local)) { $null = New-Item -ItemType Directory -Path $local }
    $ancestor = Get-Item -LiteralPath $local
    while ($null -ne $ancestor) {
        if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Deployment workspace must not traverse links/junctions.'
        }
        $ancestor = $ancestor.Parent
    }
    $work = Join-Path $local ("deploy-" + [guid]::NewGuid())
    Protect-Directory $work
    $zip = Join-Path $work 'app.zip'
    & $Python (Join-Path $PSScriptRoot 'package_appservice.py') --root $root --output $zip >$null
    if ($LASTEXITCODE -ne 0) { throw 'Source-only package validation failed.' }

    $account = Invoke-Az @('account', 'show', '--subscription', "$SubscriptionId")
    $currentAccount = Invoke-Az @('account', 'show')
    if ($account.tenantId -ine "$TenantId" -or $account.environmentName -ne 'AzureCloud') {
        throw 'Subscription tenant mismatch or unsupported cloud (only Azure public cloud is supported).'
    }
    if ($currentAccount.tenantId -ine "$TenantId") {
        throw 'Azure CLI current tenant must match TenantId for Microsoft Graph operations. Sign in to that tenant first.'
    }
    $quotaScope = "/subscriptions/$SubscriptionId/providers/Microsoft.Web/locations/$Location"
    try {
        $quotas = @(Invoke-Az @('quota', 'list', '--scope', $quotaScope))
    } catch {
        throw 'Cannot read Microsoft.Web quota. Install the Azure CLI quota extension if missing and ask the subscription owner to verify B2 quota in the selected region. No resources were changed.'
    }
    Assert-BasicQuota $quotas
    $ai = Invoke-Json 'GET' "$arm${AiResourceId}?api-version=2023-05-01"
    try {
        $endpoint = [uri]$config.endpoint
    } catch {
        throw 'Runtime config endpoint is not a valid URI. Value suppressed.'
    }
    if ($endpoint.Scheme -ne 'https' -or $endpoint.UserInfo -or $endpoint.Query -or $endpoint.Fragment) {
        throw 'Runtime endpoint must be a credential-free HTTPS Azure AI endpoint.'
    }
    $aiHosts = @()
    if ($ai.properties['endpoint']) { $aiHosts += ([uri]$ai.properties.endpoint).DnsSafeHost }
    if ($ai.properties['endpoints']) {
        foreach ($value in $ai.properties.endpoints.Values) { $aiHosts += ([uri]$value).DnsSafeHost }
    }
    if ($ai.properties['customSubDomainName']) {
        $aiHosts += "$($ai.properties.customSubDomainName).services.ai.azure.com"
        $aiHosts += "$($ai.properties.customSubDomainName).cognitiveservices.azure.com"
        $aiHosts += "$($ai.properties.customSubDomainName).openai.azure.com"
    }
    if ($endpoint.DnsSafeHost -notin $aiHosts) { throw 'Config endpoint does not belong to the supplied AI resource.' }
    $groupExists = Invoke-Az @('group', 'exists', '--subscription', "$SubscriptionId", '--name', $ResourceGroup)
    $resources = @()
    if ($groupExists) {
        $resources = @(Invoke-Az @('resource', 'list', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroup))
    }
    $site = $resources | Where-Object { $_.id -ieq $siteId }
    $plan = $resources | Where-Object { $_.id -ieq $planId }
    $identity = $resources | Where-Object { $_.id -ieq $identityId }
    Assert-Owned $site $siteId
    Assert-Owned $plan $planId
    Assert-Owned $identity $identityId
    if ($plan) {
        $plan = Invoke-Json 'GET' "$arm${planId}?api-version=$webVersion"
        if ($plan.sku.name -ne 'B2' -or $plan.sku.capacity -ne 1 -or -not $plan.properties.reserved) {
            throw 'Owned plan is not dedicated Linux B2 with capacity 1; refusing to resize it.'
        }
        $apps = Invoke-Json 'GET' "$arm${planId}/sites?api-version=$webVersion"
        if ($apps['nextLink'] -or @($apps.value | Where-Object { $_.id -ine $siteId }).Count) {
            throw 'Plan hosts other apps; refusing to alter a shared plan.'
        }
    }
    if ($site) {
        $site = Invoke-Json 'GET' "$arm${siteId}?api-version=$webVersion"
        if ($site.properties.serverFarmId -ine $planId) { throw 'App belongs to a different plan.' }
        $assigned = if ($site['identity']) { $site.identity['userAssignedIdentities'] } else { $null }
        if ($assigned -and @($assigned.Keys | Where-Object { $_ -ine $identityId }).Count) {
            throw 'App has unrelated user-assigned identities; refusing to replace them.'
        }
    }
    $registrations = @(Invoke-Az @('ad', 'app', 'list', '--filter', "displayName eq '$registrationName'"))
    if ($registrations.Count -gt 1) { throw 'Ambiguous app registration name.' }
    $registration = if ($registrations.Count) { $registrations[0] } else { $null }
    if ($registration -and ($registration['notes'] -cne $owner -or $registration.signInAudience -ne 'AzureADMyOrg')) {
        throw 'Existing app registration is not owned by this single-tenant deployment.'
    }

    # No Azure writes occur before local validation and ownership preflight above.
    if (-not $groupExists) {
        $null = Invoke-Az @('group', 'create', '--subscription', "$SubscriptionId", '--name', $ResourceGroup,
            '--location', $Location, '--tags', "cuDeploymentOwner=$owner")
    }
    $tags = @{ cuDeploymentOwner = $owner }
    if (-not $plan) {
        $null = Invoke-Json 'PUT' "$arm${planId}?api-version=$webVersion" @{
            location = $Location; tags = $tags; kind = 'linux'
            sku = @{ name = 'B2'; tier = 'Basic'; capacity = 1 }
            properties = @{ reserved = $true }
        }
    }
    if (-not $identity) {
        $null = Invoke-Json 'PUT' "$arm${identityId}?api-version=2023-01-31" @{ location = $Location; tags = $tags }
    }
    $identity = Invoke-Json 'GET' "$arm${identityId}?api-version=2023-01-31"
    $siteTouched = $true
    $site = Invoke-Json 'PUT' "$arm${siteId}?api-version=$webVersion" @{
        location = $Location; tags = $tags; kind = 'app,linux'
        identity = @{ type = 'SystemAssigned, UserAssigned'; userAssignedIdentities = @{ $identityId = @{} } }
        properties = @{
            serverFarmId = $planId; httpsOnly = $true; publicNetworkAccess = 'Disabled'
            siteConfig = @{
                linuxFxVersion = 'PYTHON|3.11'; alwaysOn = $true; numberOfWorkers = 1
                appCommandLine = 'python -m cu_diff.appservice'; ftpsState = 'Disabled'
                minTlsVersion = '1.2'; scmMinTlsVersion = '1.2'; healthCheckPath = '/api/health'
            }
        }
    }
    $hostname = $site.properties.defaultHostName
    if ($hostname -notmatch '^[a-zA-Z0-9.-]+\.azurewebsites\.net$') { throw 'Unexpected Azure hostname.' }
    $origin = "https://$hostname"
    $redirect = "$origin/.auth/login/aad/callback"
    if (-not $registration) {
        $registration = Invoke-Json 'POST' 'https://graph.microsoft.com/v1.0/applications' @{
            displayName = $registrationName; notes = $owner; signInAudience = 'AzureADMyOrg'
            web = @{ redirectUris = @($redirect); implicitGrantSettings = @{ enableIdTokenIssuance = $false; enableAccessTokenIssuance = $false } }
        }
    } elseif (@($registration.web.redirectUris).Count -ne 1 -or $registration.web.redirectUris[0] -cne $redirect) {
        throw 'Existing registration callback differs; refusing to replace it.'
    }
    $clientId = $registration.appId
    $servicePrincipals = @(Invoke-Az @('ad', 'sp', 'list', '--filter', "appId eq '$clientId'"))
    if (-not $servicePrincipals.Count) {
        $null = Invoke-Az @('ad', 'sp', 'create', '--id', $clientId)
    }
    $federationUrl = "https://graph.microsoft.com/v1.0/applications/$($registration.id)/federatedIdentityCredentials"
    $federations = Invoke-Json 'GET' $federationUrl
    $issuer = "https://login.microsoftonline.com/$TenantId/v2.0"
    if ($federations.value.Count -eq 0) {
        $null = Invoke-Json 'POST' $federationUrl @{
            name = 'cu-review-appservice'; issuer = $issuer; subject = $identity.properties.principalId
            audiences = @('api://AzureADTokenExchange')
        }
    } elseif ($federations.value.Count -ne 1 -or
        $federations.value[0].issuer -cne $issuer -or
        $federations.value[0].subject -cne $identity.properties.principalId -or
        @($federations.value[0].audiences).Count -ne 1 -or
        $federations.value[0].audiences[0] -cne 'api://AzureADTokenExchange') {
        throw 'Existing federation differs; refusing to modify credentials.'
    }
    $roles = @('379c52cb-64de-498c-8b5b-c6170d6c49d4', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
    $assignmentsUrl = "$arm${AiResourceId}/providers/Microsoft.Authorization/roleAssignments"
    $assignments = Invoke-Json 'GET' "${assignmentsUrl}?api-version=2022-04-01&`$filter=atScope()"
    $aiSubscription = $AiResourceId.Split('/')[2]
    foreach ($role in $roles) {
        $roleId = "/subscriptions/$aiSubscription/providers/Microsoft.Authorization/roleDefinitions/$role"
        $existing = @($assignments.value | Where-Object {
            $_.properties.principalId -ieq $site.identity.principalId -and
            $_.properties.roleDefinitionId -ieq $roleId -and $_.properties.scope -ieq $AiResourceId
        })
        if (-not $existing.Count) {
            $assignmentId = [guid]::NewGuid()
            $null = Invoke-Json 'PUT' "${assignmentsUrl}/${assignmentId}?api-version=2022-04-01" @{
                properties = @{ roleDefinitionId = $roleId; principalId = $site.identity.principalId; principalType = 'ServicePrincipal' }
            }
        }
    }
    $settings = @{
        CU_CONFIG_JSON = ($config | ConvertTo-Json -Depth 40 -Compress)
        CU_PUBLIC_ORIGIN = $origin; CU_TENANT_ID = "$TenantId"; CU_ALLOWED_PRINCIPALS = "$OperatorObjectId"
        CU_DATA_DIR = '/home/cu-review/data'; CU_CACHE_DIR = '/home/cu-review/cache'
        OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID = $identity.properties.clientId
        WEBSITE_AUTH_AAD_ALLOWED_TENANTS = "$TenantId"
        SCM_DO_BUILD_DURING_DEPLOYMENT = 'true'; ENABLE_ORYX_BUILD = 'true'
        WEBSITES_ENABLE_APP_SERVICE_STORAGE = 'true'; PYTHONUNBUFFERED = '1'
    }
    $null = Invoke-Json 'PUT' "$arm${siteId}/config/appsettings?api-version=$webVersion" @{ properties = $settings }
    $null = Invoke-Json 'PUT' "$arm${siteId}/config/slotConfigNames?api-version=$webVersion" @{
        properties = @{ appSettingNames = @('OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID') }
    }
    $auth = @{
        platform = @{ enabled = $true; runtimeVersion = '~1' }
        globalValidation = @{
            requireAuthentication = $true; unauthenticatedClientAction = 'RedirectToLoginPage'
            redirectToProvider = 'azureActiveDirectory'; excludedPaths = @('/api/health')
        }
        identityProviders = @{
            azureActiveDirectory = @{
                enabled = $true
                registration = @{
                    clientId = $clientId; openIdIssuer = $issuer
                    clientSecretSettingName = 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
                }
                validation = @{
                    allowedAudiences = @($clientId, "api://$clientId")
                    defaultAuthorizationPolicy = @{ allowedPrincipals = @{ identities = @("$OperatorObjectId"); groups = @() } }
                }
            }
        }
        login = @{ tokenStore = @{ enabled = $false }; preserveUrlFragmentsForLogins = $false }
        httpSettings = @{ requireHttps = $true }
    }
    $null = Invoke-Json 'PUT' "$arm${siteId}/config/authsettingsV2?api-version=$webVersion" @{ properties = $auth }
    $verified = (Invoke-Json 'GET' "$arm${siteId}/config/authsettingsV2?api-version=$webVersion").properties
    $aad = $verified.identityProviders.azureActiveDirectory
    $principals = @($aad.validation.defaultAuthorizationPolicy.allowedPrincipals.identities)
    if (-not $verified.platform.enabled -or -not $verified.globalValidation.requireAuthentication -or
        $verified.globalValidation.unauthenticatedClientAction -ne 'RedirectToLoginPage' -or
        $verified.globalValidation.redirectToProvider -ne 'azureActiveDirectory' -or
        -not $verified.httpSettings.requireHttps -or -not $aad.enabled -or
        $aad.registration.clientId -ne $clientId -or $aad.registration.openIdIssuer -ne $issuer -or
        $aad.registration.clientSecretSettingName -ne 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID' -or
        @($aad.validation.defaultAuthorizationPolicy.allowedPrincipals['groups']).Where({ $null -ne $_ }).Count -ne 0 -or
        $principals.Count -ne 1 -or $principals[0] -ne "$OperatorObjectId" -or
        @($verified.globalValidation.excludedPaths).Count -ne 1 -or
        $verified.globalValidation.excludedPaths[0] -ne '/api/health') {
        throw 'EasyAuth verification failed; public access remains disabled.'
    }
    # SCM ZIP deployment needs network access. Authentication is already enforced.
    $null = Invoke-Json 'PATCH' "$arm${siteId}?api-version=$webVersion" @{ properties = @{ publicNetworkAccess = 'Enabled' } }
    $null = Invoke-Az @('webapp', 'deploy', '--subscription', "$SubscriptionId", '--resource-group', $ResourceGroup,
        '--name', $AppName, '--src-path', $zip, '--type', 'zip', '--clean', 'true', '--restart', 'true',
        '--timeout', '1800000')
    Write-Host "Deployment completed: $origin"
    Write-Host 'Verify approved and unapproved tenant users, anonymous redirects, and /api/health before sharing.'
    Write-Host 'Never scale beyond one instance. Jobs and review sessions are in memory.'
} catch {
    if ($siteTouched -and $work) {
        try {
            $null = Invoke-Json 'PATCH' "$arm${siteId}?api-version=$webVersion" @{ properties = @{ publicNetworkAccess = 'Disabled' } }
        } catch {
            Write-Warning 'Could not confirm network disable. Operator must inspect the owned app immediately.'
        }
    }
    throw
} finally {
    if ($work -and (Test-Path -LiteralPath $work)) {
        Remove-Item -LiteralPath $work -Recurse -Force
    }
}
