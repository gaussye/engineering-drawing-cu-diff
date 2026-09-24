# App Service deployment

Status: Blocked - awaiting paid resource and region approval

## 1. Objective
Deploy the existing engineering drawing review application to Azure App Service
with a public HTTPS URL, protected access, and managed identity access to the
existing approved Azure AI resource. Do not deploy customer data or local caches.

## 2. Workspace
Existing Python application; inspect cloud runtime and authentication requirements.

## 3. Azure context
Use the existing approved subscription (record actual IDs only in private deployment
state). Proposed region: West US, same region as the existing AI resource.
Existing plans belong to unrelated applications and will not be changed or reused.
Create a dedicated resource group, Linux Basic B2 plan (one instance), and web app.
Public retail price checked 2026-09-24: Linux B2 West US USD 0.036/hour,
approximately USD 26.28/730-hour month, excluding AI, egress, and taxes.
User approval prompt could not be answered (user unavailable); no paid resources,
Entra registrations, or role assignments may be created yet.
Read-only quota discovery subsequently found West US B1/B2/B3 limit 0 (usage -1,
unavailable). West US cannot currently host this proposed plan.
Alternative: West US 2 B2 limit 10 / usage 0; Linux B2 USD 0.034/hour, approximately
USD 24.82/730-hour month. This is a different storage region and also needs approval.

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
- [x] Select hosting resources and access protection (pending approval)
- [x] Implement explicit cloud boundary, principal binding, and managed identity credentials
- [x] Finish deployment configuration and allowlisted package tooling
- [x] Run local verification

## 6. Deployment
- [ ] Validate preparation
- [ ] Deploy application and scoped AI roles
- [ ] Verify HTTPS, protected access, and managed identity authorization

## 7. Validation Proof
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
Not deployed. No public URL created. Blocked on billed resource/region approval.
West US quota is zero; West US 2 is an unapproved alternative.
