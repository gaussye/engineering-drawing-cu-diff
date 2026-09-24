# App Service deployment

Status: Deployed

## Current change: demo login
User requested fixed username/password login for this demo on 2026-09-24.
Keep the existing West US 2 app/plan and system managed identity unchanged.
Add explicit opt-in demo authentication with a generated fixed account, password
hash in App Settings, opaque server-side sessions, CSRF, expiry, and login limits.
Credentials stay in ignored operator-local files; never package or commit them.
Deploy protected application code before disabling platform Entra enforcement;
the application must fail closed without valid demo credential configuration.
Retain the Entra mode for later production use. Verify real browser login/logout,
wrong-password denial, anonymous API rejection, and existing application defaults.
Preparation complete: explicit demo mode, salted scrypt hash, login/logout UI,
eight-hour opaque sessions, ten attempts/minute limit, bounded challenge storage,
and per-login review ownership. Real loopback-TLS Chromium login/logout passes.
The native form needs Referrer-Policy same-origin on /login; no-referrer sends
Origin null in Chromium, so retaining the strict Origin check required this fix.
Source-only packager includes the new auth module/form and excludes credentials.
Deployment retains the Entra gate until new-process demo health is observed.
An existing Windows Set-Acl privilege issue was resolved with an access-only
icacls restriction plus explicit read-back; fresh-directory protection verified.
Existing target/subscription/region/system identity rechecked read-only.
No new capacity or AI model changes; exact target and fixed demo credentials remain
in ignored operator-only local files.
Final lifecycle refinement: retired/expired logins release unused review slots,
without interrupting in-flight uploads or AI jobs. Completed retired sessions are
removed from memory on subsequent cleanup; persisted files retain normal TTL.

## 1. Objective
Deploy the existing engineering drawing review application to Azure App Service
with a public HTTPS URL, protected access, and managed identity access to the
existing approved Azure AI resource. Do not deploy customer data or local caches.

## 2. Workspace
Existing Python application; inspect cloud runtime and authentication requirements.

## 3. Azure context
Use the existing approved subscription (record actual IDs only in private deployment
state). Selected region: West US 2, after West US quota was found unavailable.
Existing plans belong to unrelated applications and will not be changed or reused.
Create a dedicated resource group, Linux Basic B2 plan (one instance), and web app.
Public retail price checked 2026-09-24: Linux B2 West US USD 0.036/hour,
approximately USD 26.28/730-hour month, excluding AI, egress, and taxes.
User explicitly approved creating resources and deploying to an available region
on 2026-09-24 at 18:25 +08:00. Select West US 2 dedicated Linux B2, one instance,
in the existing approved subscription. Earlier unavailable approval is superseded.
Read-only quota discovery subsequently found West US B1/B2/B3 limit 0 (usage -1,
unavailable). West US cannot currently host this proposed plan.
Alternative: West US 2 B2 limit 10 / usage 0; Linux B2 USD 0.034/hour, approximately
USD 24.82/730-hour month. This alternative is now authorized by the user.

## 4. Architecture and recipe
Recipe: AZCLI, to integrate the existing application without template replacement,
explicitly sequence Entra/managed-identity configuration, and whitelist deploy files.
Python 3.11 Linux App Service with Waitress, one process/instance and Always On.
Public HTTPS URL protected by single-tenant Entra Easy Auth, initially allow only
the signed-in operator. User-assigned managed identity with federated credential
for secretless Easy Auth; system identity for AI access.
Grant Content Understanding Reader and OpenAI User only on the existing AI resource.
No model deployment changes, CU analyzer creation, shared default mutations, or API keys.
Private runtime configuration via app settings; private data/cache under persistent
App Service /home, outside wwwroot. No historical local data will be uploaded.
Jobs/session state remains in memory: service restart requires refreshing/reuploading;
do not scale out until shared job/session storage is implemented.
Use explicit cloud origin, secure cookies, principal-bound sessions, fail-closed
Easy Auth checks, original CSRF protection, and local-only defaults unchanged.
Verify with synthetic fixtures and minimal AI smoke calls, never customer documents.

## 5. Preparation
- [x] Inspect runtime, dependencies, authentication, session and storage model
- [x] Select hosting resources and access protection (approved)
- [x] Implement explicit cloud boundary, principal binding, and managed identity credentials
- [x] Finish deployment configuration and allowlisted package tooling
- [x] Run local verification

## 6. Deployment
- [x] Validate preparation
- [x] Deploy application and scoped AI roles
- [x] Verify HTTPS, protected access, and managed identity authorization

## 7. Validation Proof
### Final demo lifecycle/recovery validation, 2026-09-24 19:49+08:00
- Reinvoked azure-validate after final lifecycle refinement.
- Same affected suite including Chromium: 89 tests, 29.898s, OK (5 Windows
  symlink privilege skips); wheel build and diff check passed.
- Deployment HTTP/package suite after adding bounded gateway-propagation waits:
  23 tests, OK (5 skips). Tests reject missing demo process marker, wrong forms,
  anonymous API access, and exhausted waits; transient login-gateway denial retries.
- Initial rollout reached demo-process health but failed its immediate login-form
  check after gateway switch; failure correctly disabled public networking.
  ARM read-back confirms demo mode, HTTPS enabled, EasyAuth disabled, app Running.
  The verifier now waits for the actual form before checking protected routes;
  deployment success still requires live verification, not ARM configuration alone.
- Deployment parameters, owned resources, subscription/region, AI role scope, and
  source-only artifact boundaries are unchanged from the prior validation.

### Demo authentication update, 2026-09-24 19:39+08:00
All validation checks pass under azure-validate:
- [x] Azure CLI 2.81.0 and authenticated subscription/tenant confirmed.
- [x] Existing app is Running, HTTPS-only, West US 2; system identity unchanged.
- [x] PowerShell AST parses; demo-mode preview makes no Azure calls.
- [x] Actual Protect-Directory helper creates a verified operator-only Windows ACL.
- [x] Applicable resource-group/inherited policy query returned no assignments.
- [x] Static role verification: unchanged CU Reader + OpenAI User at AI account,
  bound to system identity, not demo user or login identity.
- [x] Bicep/template what-if/Docker: not applicable to existing imperative AZCLI
  source deployment; no new infrastructure template or capacity changes.
- [x] `CU_BROWSER_TESTS=1 python -m unittest tests.test_demo_browser
  tests.test_demo_auth tests.test_hosting tests.test_deployment_http
  tests.test_deployment_package tests.test_web tests.test_pdf_export_web
  tests.test_web_browser -q`: 88 tests, 34.343s, OK; 5 Windows symlink privilege
  skips, with mocked reparse/link rejection also exercised.
- [x] Chromium against actual loopback TLS: wrong password denied, correct login,
  Secure cookie continuity, protected bootstrap, logout, mobile form layout.
- [x] `node --check cu_diff/static/app.js` and `git diff --check` passed.
- [x] `python -m pip wheel . --no-deps --wheel-dir local/demo-wheel --quiet`
  passed using declared isolated build dependencies. The initial no-build-isolation
  probe lacked setuptools; normal isolated build resolved that dependency.
- [x] Real source package: 34 allowlisted files, including login form/module.
  Wheel includes login form. Credential files are ignored and operator-only.
Live browser authentication and post-switch authorization remain required before
claiming deployment success. No customer files or billed AI requests used here.

### Approved execution preflight, 2026-09-24 18:25+08:00 onward
- Azure CLI 2.81.0 authenticated to the approved subscription and tenant.
- `az quota show` / `az quota usage show` reconfirmed West US 2 B2 limit 10, usage 0.
- `az group exists` returned false for the dedicated target group; registration
  discovery returned no colliding application. Stable identifiers saved only in
  ignored `local/appservice-target.json`.
- Applicable policy assignments read at subscription/inherited scopes: SQL/Arc,
  data protection, and open-source databases; no relevant location/SKU denial found.
- Static role IDs checked against live role definitions: CU Reader has analyze and
  result-read actions; OpenAI User has completion actions; assignments are scoped
  to the existing AI resource, with principalType ServicePrincipal. Operator has
  inherited Owner/User Access Administrator permissions.
- Script AST parses; actual local config passes secret-field screening;
  Windows-safe Azure CLI wrapper successfully performs account and Graph queries.
- `python -m unittest tests.test_hosting tests.test_deployment_package -q`:
  25 tests, OK (5 Windows symlink-privilege skips). Wheel build and diff check pass.
- Deployment is imperative AZCLI/provider REST, not Bicep/ARM template execution:
  Bicep compilation, template what-if, AZD environment, and Docker build are not
  applicable. Source allowlist/package and explicit ownership/approval checks are
  the deployment dry-run validation. Azure provider runtime validation remains
  mandatory during actual creates; failures stop without deleting unrelated data.

### Earlier preparation evidence
2026-09-24: `python -m unittest tests.test_hosting tests.test_web tests.test_client
tests.test_analysis_options -q` passed 68 tests, with expected negative-path logs.
Managed identity tests use synthetic SDK responses; no cloud identity test or
customer document submission occurred.
Read-only Azure checks: account and signed-in principal confirmed; existing plans
listed; exact Content Understanding Reader/OpenAI User permissions queried;
`az quota list` and `az quota usage list` for Microsoft.Web West US and West US 2.
This is preparation evidence only, not deployment validation approval.
`CU_BROWSER_TESTS=1 python -m unittest discover -s tests -q`: 643 tests,
442.713 seconds, OK with 5 skips. `python -m pip wheel . --no-deps` succeeded.
`git diff --check` passed. No paid inference was run.
Additional hosted upload/preview/session-isolation test: `python -m unittest
tests.test_hosting -q` passed all 10 tests. Existing loopback health remains OK.
Linux Python 3.11 dependency wheel resolution succeeded with manylinux_2_28,
manylinux2014, and manylinux_2_17 tags. A manylinux2014-only probe was insufficient
for the existing PyMuPDF >=1.28 requirement; deployment needs modern glibc >=2.28.
The azure-validate skill was invoked; its plan-approval prerequisite is not met,
so deployment validation cannot be marked Validated and azure-deploy is blocked.
Final `python -m unittest tests.test_hosting tests.test_deployment_package -q`:
25 tests, OK with 5 Windows symlink-privilege skips (mocked reparse/link rejection
also tested). PowerShell AST parsing passed. Deployment script invocation with
synthetic identifiers and no execution switches returned preview-only and made
no Azure calls. Real-source allowlisted ZIP validation passed.
Actual Oryx build, target glibc ABI, Entra sign-in, managed identity token issuance,
AI role propagation, and public HTTPS must still be tested after approved deployment.

## 8. Deployment Results
### Fixed-account demo update, 2026-09-24 19:58+08:00
Successfully published to the same owned West US 2 App Service and B2 instance.
Final OneDeploy status 4, complete=true, active=true. Exact deployment ID and
target mode are recorded in ignored operator-local state.

- Public `/` redirects to the Chinese `/login` form; Microsoft sign-in is no
  longer required. Anonymous and forged-identity protected API/static requests
  return 401. The login form became available after seven readiness probes
  following gateway change, confirming a real propagation delay.
- Real Chromium: wrong password returns 401; correct fixed credentials enter
  the application. The secure host-only HttpOnly/SameSite cookie works.
- Authenticated bootstrap is 200; default Sol/cache OFF remain unchanged.
- A generated synthetic PDF uploaded and rendered successfully. A separately
  authenticated browser using the same account cannot access its preview (404).
- Logout succeeds; protected bootstrap and replay of the revoked login cookie
  both return 401. Mobile login layout and browser JavaScript checks passed.
- System identity is unchanged. Live role assignments are still CU Reader and
  OpenAI User, scoped only to the existing AI account. No model/analyzer changes,
  no new AI calls, and no customer files uploaded during this login verification.
- Credentials are fixed and retained in an ignored operator-only local directory.
  Only the salted password hash is in App Settings; neither password nor hash
  is in Git or the source-only deployment ZIP.
- Local verification summary and login screenshot remain ignored. Shared demo
  access has no MFA or individual attribution; Entra configuration is retained
  for a future explicit switch back.

### Original Entra deployment
2026-09-24: deployment completed in West US 2. Dedicated Linux B2, one instance,
one app, Python 3.11.15. Final OneDeploy status 4, complete=true, active=true.
Actual endpoint/resource identifiers are in ignored local deployment state and
the user handoff, not baked into generic source configuration.

- Public HTTPS health returns 200 and `{"local_only":false,"status":"ok"}`.
- Anonymous API access and forged EasyAuth principal headers return 401.
- A Chromium browser reaches the tenant-specific Microsoft sign-in page.
- Entra registration is single-tenant, has zero password credentials, enables
  ID tokens for EasyAuth hybrid login, and disables implicit access tokens.
- Secretless federation was verified by exchanging the login identity's
  assertion; no token or credential was printed or persisted.
- Live system identity has CU Reader and OpenAI User at the AI account only.
  Actual synthetic PDF CU analysis and separate Sol/Astra/Luna calls all succeeded
  from the deployed application's identity; no customer drawings were uploaded.
- Live bootstrap preserves default Sol/cache off and user Sol rates 2/0.20/10.
- Final targeted tests: 31 tests OK (5 Windows symlink-privilege skips).
- Initial CLI Linux startup tracking falsely failed after successful Oryx build
  and briefly triggered fail-closed public-network isolation. Deployment script
  now disables that tracker and verifies the actual HTTP boundary. Rerun succeeded.
- Interactive completion of the operator's sign-in still requires the operator;
  no password or MFA was collected, bypassed, or automated.

Operational constraint: one process/instance; restart loses active browser
sessions/jobs. Data and billed-response guards persist privately under /home.
