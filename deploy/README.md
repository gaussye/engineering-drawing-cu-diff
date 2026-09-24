# Dedicated Azure App Service deployment

These tools prepare and execute a deployment. Adding the files alone does not
create resources. A dedicated paid B2 plan requires explicit approval.
Do not run the execution command until a subscription owner approves the recurring
charge and the operator has reviewed the target IDs.

## Boundaries

- Azure public cloud, one dedicated **Linux B2** instance, Python **3.11**,
  AlwaysOn, startup `python -m cu_diff.appservice`.
- Oryx installs the generated `requirements.txt` containing `.[appservice]`.
  The source ZIP is not a run-from-package deployment.
- Jobs and review sessions live in process memory. **No scale-out, multiple
  workers, deployment slots, or shared plan.** Restarting/redeploying loses live
  jobs/sessions. Persistent customer files use `/home/cu-review/data`; analysis
  cache uses `/home/cu-review/cache`, never the deployed `wwwroot`.
- Existing AI resource and model deployments are reused. The script does not
  create analyzers, configure default models, or mutate AI model deployments.
- Entra EasyAuth requires the specified tenant and operator **object ID**.
  The only anonymous route is `/api/health`. All other unauthenticated requests
  redirect browsers to Entra login or return 401/403 to API clients.
  Application authorization remains a second boundary.
- The app's **system-assigned** identity receives only these two assignments,
  at the **AI account resource scope**, not the resource group/subscription:
  Content Understanding Reader (`379c52cb-64de-498c-8b5b-c6170d6c49d4`) and
  Cognitive Services OpenAI User (`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`).
- A separate **user-assigned** identity, named `<AppName>-auth`, is attached only
  to this app and used only for secretless EasyAuth client assertions. Never attach
  it to another resource or give it AI roles.
- EasyAuth's hybrid `code+id_token` login requires ID-token issuance enabled on
  the registration even when using a federated client assertion. Implicit
  access-token issuance remains disabled; no client secret is created.

## Prerequisites

Use PowerShell 7.2+ and Python 3.11+, plus a current Azure CLI. Sign in beforehand
to the target tenant. The current CLI tenant and the selected subscription tenant
must match `TenantId`; the script does not change your CLI default subscription.
The script targets ARM by explicit subscription/resource IDs.

The operator needs:

1. Permission to create/manage a resource group (if absent), App Service plan,
   app, and user-assigned identity in the specified subscription.
2. Role-assignment write permission **at the existing AI account** for the two
   named roles; ordinary Contributor alone is insufficient.
3. Entra permission to create an application registration and service principal,
   and manage its federated credentials. Tenant policy may require an admin.
4. Available Linux B2 quota/capacity in the selected region and an app name that
   is globally available. The Azure CLI `quota` extension must be available for
   the read-only preflight; install it separately if needed.
5. Working access from App Service to the existing AI endpoint. This recipe
   does not create private endpoints, VNet integration, DNS, or firewall rules.

Prepare an ignored `local\config.json` containing the existing AI endpoint,
model deployment mapping, analyzer/extraction settings, and pricing configuration.
Do not include API keys, bearer tokens, passwords, or client secrets. Credential-like
fields are rejected; managed identity supplies tokens. The endpoint is checked
against the specified AI account before any cloud writes. Do not use deployment
tooling to upload customer drawings or comparison outputs.

## Interfaces and local validation

Run from the repository root. Required parameters:

| Parameter | Meaning |
| --- | --- |
| `SubscriptionId` | Target subscription GUID |
| `ResourceGroup` | New or existing containing resource group |
| `Location` | Azure region code, for example `eastus2`; choose explicitly |
| `AppName` | Globally unique web app name |
| `PlanName` | Dedicated plan name; must not host any other app |
| `AiResourceId` | Full existing `Microsoft.CognitiveServices/accounts` ARM ID |
| `TenantId` | Single workforce tenant GUID |
| `OperatorObjectId` | Permitted user's object ID in that tenant, not client ID/email |
| `DeploymentId` | Operator-generated GUID used as the persistent ownership marker |

Optional parameters: `ConfigFile` (default `local\config.json`, relative to repository
root), `Python` (default `python`), `Execute`, and `ApprovePaidResources`.
Keep `DeploymentId` and all resource names for later reruns. Losing them is not a
reason to adopt or delete a resource automatically.

```powershell
# Synthetic packaging tests; never contacts Azure.
python -m unittest discover -s tests -p test_deployment_package.py -v

# Build and inspect a source-only archive. Output must not already exist.
python .\deploy\package_appservice.py --output .\local\appservice-review.zip
python -m zipfile -l .\local\appservice-review.zip
```

The ZIP contains only top-level `cu_diff\*.py`, direct `static\*.html`, `*.css`,
`*.js`, direct `pricing\*.json`, `pyproject.toml`, and generated `requirements.txt`.
It includes no PDFs, images, `.env`, local configuration, customer artifacts,
cache, `.git`, `.venv`, build products, or tests. Nested package directories are
not traversed. Symlinks and Windows reparse points in selected paths are rejected.
Adding package subdirectories/assets in future requires an explicit whitelist
change and a test; do not replace this builder with a repository-wide ZIP command.
Archives have stable file ordering/timestamps. Dependencies are version-range
resolved by pip, not locked byte-for-byte; review dependency upgrades separately.

Prepare parameter values (replace every placeholder):

```powershell
$deployment = @{
    SubscriptionId = '<subscription-guid>'
    ResourceGroup = '<dedicated-or-existing-rg>'
    Location = '<region-code>'
    AppName = '<globally-unique-app-name>'
    PlanName = '<dedicated-b2-plan-name>'
    AiResourceId = '/subscriptions/<ai-subscription-guid>/resourceGroups/<ai-rg>/providers/Microsoft.CognitiveServices/accounts/<existing-ai-account>'
    TenantId = '<tenant-guid>'
    OperatorObjectId = '<user-object-guid>'
    DeploymentId = '<new-guid-retain-for-reruns>'
}

# Safe preview: no Azure calls, no packaging, no config read, no writes.
.\deploy\Deploy-AppService.ps1 @deployment

# ONLY after explicit cost/provisioning approval:
.\deploy\Deploy-AppService.ps1 @deployment -Execute -ApprovePaidResources
```

`Execute` alone fails before any Azure call. Both switches are required even on a
rerun, so a missing resource cannot silently create a new billable replacement.
No Azure execution is part of local unit testing.

## Execution, isolation, and recovery

Before writing to Azure, the script validates the package/config, verifies the
tenant/AI endpoint, checks Microsoft.Web B2 quota in the selected region, and
checks ownership of existing named resources. A missing, ambiguous, unavailable,
or zero B2 limit stops execution before any writes. It never requests or raises
quota. A positive limit is entitlement, **not a capacity guarantee**; usage `-1`
means unavailable telemetry and must not be interpreted as available capacity.
The operator must resolve quota/region approval separately. It permits
an existing containing resource group, but never retags or deletes it. The plan,
site, and authentication identity must carry the exact
`cuDeploymentOwner=cu-review:<DeploymentId>` marker; the registration's `notes`
must carry the same marker. Matching names alone do not confer ownership.
An owned existing plan must already be Linux B2, capacity one, with no other apps.
The script refuses to resize, move, adopt, or delete unrelated resources.

The initial site ARM PUT sets `publicNetworkAccess=Disabled`, including on
reruns. Only after app settings and EasyAuth are configured and EasyAuth's
critical settings are read back does the script enable public networking.
This is needed for SCM ZIP deployment; the app is then protected by EasyAuth
even while the new package is building. Configuration errors before that point
leave networking disabled. A later failure attempts to disable networking again.
If that recovery fails, an explicit warning requires operator inspection.
Existing owned-app updates therefore have downtime; this is not a zero-downtime
release mechanism.

The single-tenant application registration redirects to
`https://<actual-app-hostname>/.auth/login/aad/callback`; the actual ARM-reported
hostname is used (including regional/hash suffixes). Its federated credential has:

- issuer `https://login.microsoftonline.com/<TenantId>/v2.0`;
- subject the authentication identity's **principal ID**;
- audience `api://AzureADTokenExchange`.

`OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID` contains the authentication identity's
**client ID** (not the app registration ID), is marked slot-sticky, and is also
EasyAuth's `registration.clientSecretSettingName`. There is no client secret.
The Entra app registration's client ID is used separately as the authentication
provider client ID. Token storage is disabled.

Runtime settings are `CU_CONFIG_JSON`, `CU_PUBLIC_ORIGIN`, `CU_TENANT_ID`,
`CU_ALLOWED_PRINCIPALS` (a comma-separated OID list; this script supplies one),
`CU_DATA_DIR`, and `CU_CACHE_DIR`. `WEBSITE_HOSTNAME` is provided by App Service
and must match `CU_PUBLIC_ORIGIN` in the fail-closed entrypoint.
The entrypoint also requires the platform-provided `WEBSITE_AUTH_ENABLED=true`;
the script enables EasyAuth itself rather than faking this marker in app settings.
The script replaces the owned site's app settings with its managed settings.
Do not use it to preserve unrelated custom settings.

Configuration is sent via an owner-only ephemeral directory under ignored
`local\deploy-<random-guid>`, with restricted Windows ACLs (or Unix permissions),
never inside the ZIP or in CLI argument values. CLI output/errors are suppressed;
configuration/tokens are not printed. The directory is removed in `finally`.
If the operator forcibly kills the process, inspect and remove that specific
leftover directory securely. Avoid terminal transcription, Azure CLI `--debug`,
and logging app settings. Authorized Azure administrators can still read app
settings; they are not a substitute for a secret vault.

Writes are not transactional. On failure, owned plan/identity/registration/role
assignments may remain, and the B2 plan keeps billing even with the app stopped
or network disabled. Resolve the reported permission/configuration issue and rerun
with the **same** parameters. Allow for Entra federation and RBAC propagation.
No automatic deletion or rollback of resources is attempted. A human must review
cost and authorize eventual cleanup explicitly.

## Required post-deployment checks

Every target needs live validation. The script disables Azure CLI's Linux startup
tracking (`--track-status false`), because that poller can fail after Oryx has
successfully deployed and the app is healthy. It still requires the deployment
command to succeed, then runs `verify_http.py` to check actual public health,
anonymous access denial, and tenant-specific login initiation. Failed checks
isolate the owned app; they are never treated as deployment success.

For a separate non-billed HTTP check:

```powershell
python .\deploy\verify_http.py --origin https://<hostname> --tenant <tenant-guid>
```

Before sharing the endpoint, the approved operator must additionally verify:

1. Deployment/Oryx succeeded, the startup process is healthy, and
   `https://<hostname>/api/health` returns the expected minimal health response.
2. Anonymous access to `/` and protected API routes redirects to Entra or returns 401/403; no
   drawings/results can be retrieved anonymously.
3. The allowed user can sign in, while another user from the same tenant cannot
   access the app (403). A different tenant is rejected. Test a fresh browser
   session, not only an existing authentication cookie.
4. HTTPS-only/TLS, AlwaysOn, B2 capacity one, no extra app on the plan, and no
   unexpected identities or inherited role assignments. Keep the authentication
   identity exclusive to this app; do not manually reuse it elsewhere.
5. An approved synthetic test drawing works through the existing AI deployment;
   no analyzer/model mutation is needed. Avoid customer data for the initial test.
6. `/home/cu-review` persistence, retention/deletion procedures, backup needs,
   monitoring, and ongoing B2/storage/AI cost are accepted by the operator.

Health checks alone do not prove Entra sign-in, RBAC propagation, AI access, or
authorization correctness. Network-restricted AI accounts, tenant consent
policy, CLI behavior, and Azure regional capacity still need live confirmation.

References: [App Service Entra authentication and managed-identity federation](https://learn.microsoft.com/azure/app-service/configure-authentication-provider-aad),
[Python App Service build automation](https://learn.microsoft.com/azure/app-service/configure-language-python),
[ZIP deployment](https://learn.microsoft.com/azure/app-service/deploy-zip).
