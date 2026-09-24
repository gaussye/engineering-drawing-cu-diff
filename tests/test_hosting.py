import base64
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pymupdf

from cu_diff.client import Client, CUError
from cu_diff.hosting import CloudBoundary

if importlib.util.find_spec("flask") is None:
    raise unittest.SkipTest("Install .[appservice] to run hosted web tests")

from cu_diff.appservice import application
from cu_diff.web import create_app


ORIGIN = "https://synthetic.azurewebsites.net"
TENANT = "11111111-1111-1111-1111-111111111111"
USER = "22222222-2222-2222-2222-222222222222"
OTHER = "33333333-3333-3333-3333-333333333333"


def identity(user=USER, tenant=TENANT, **changes):
    value = {"auth_typ": "aad", "claims": [
        {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": tenant},
        {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": user}]}
    value.update(changes)
    return base64.b64encode(json.dumps(value).encode()).decode()


class CloudBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.boundary = CloudBoundary(ORIGIN, TENANT, frozenset((USER, OTHER)))
        self.app = create_app({"completion_model": "synthetic"}, root / "data", root / "cache",
                              cloud=self.boundary)
        self.client = self.app.test_client()
        self.headers = {"X-MS-CLIENT-PRINCIPAL": identity()}

    def tearDown(self):
        self.app.extensions["review_store"].close()
        self.temp.cleanup()

    def test_cloud_rejects_anonymous_or_wrong_identity_everywhere_except_health(self):
        for path in ("/", "/static/app.js", "/api/bootstrap", "/api/jobs/not-a-job"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, base_url=ORIGIN).status_code, 403)
        health = self.client.get("/api/health", base_url=ORIGIN)
        self.assertEqual(health.get_json(), {"status": "ok", "local_only": False})
        self.assertNotIn("Set-Cookie", health.headers)
        for header in ("invalid", identity(tenant=OTHER), identity(user=TENANT),
                       identity(auth_typ="github"), identity(claims=None),
                       identity(claims=[{"typ": "oid", "val": []}]),
                       identity(claims=["invalid"]), "x" * 16385):
            with self.subTest(header=header[:30]):
                result = self.client.get("/api/bootstrap", base_url=ORIGIN,
                                         headers={"X-MS-CLIENT-PRINCIPAL": header})
                self.assertEqual(result.status_code, 403)

    def test_secure_cookie_csrf_and_principal_bound_session(self):
        boot = self.client.get("/api/bootstrap", base_url=ORIGIN, headers=self.headers)
        self.assertEqual(boot.status_code, 200)
        cookie = boot.headers["Set-Cookie"]
        for required in ("__Host-cu_review=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(required, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertEqual(boot.headers["Strict-Transport-Security"], "max-age=31536000")
        self.assertEqual(self.client.post("/api/compare", base_url=ORIGIN,
                                          headers=self.headers, json={"revision": 0}).status_code, 403)
        headers = self.headers | {"X-CSRF-Token": boot.get_json()["csrf_token"], "Origin": ORIGIN}
        self.assertEqual(self.client.post("/api/compare", base_url=ORIGIN,
                                          headers=headers, json={"revision": 0}).status_code, 400)
        self.assertEqual(self.client.get("/api/bootstrap", base_url=ORIGIN,
                         headers={"X-MS-CLIENT-PRINCIPAL": identity(user=OTHER)}).status_code, 403)
        self.assertEqual(len(self.app.extensions["review_store"].sessions), 1)

    def test_forwarded_headers_cannot_override_host_or_origin(self):
        for headers, url in (
            (self.headers | {"X-Forwarded-Host": self.boundary.host}, "https://wrong.example"),
            (self.headers | {"Origin": "https://wrong.example"}, ORIGIN),
            (self.headers | {"Sec-Fetch-Site": "cross-site"}, ORIGIN),
        ):
            self.assertEqual(self.client.get("/api/bootstrap", base_url=url, headers=headers).status_code, 403)

    def test_authorized_upload_and_preview_remain_private_between_browser_sessions(self):
        boot = self.client.get("/api/bootstrap", base_url=ORIGIN, headers=self.headers).get_json()
        headers = self.headers | {"Origin": ORIGIN, "X-CSRF-Token": boot["csrf_token"],
                                  "X-Filename": "synthetic.pdf"}
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=300, height=200)
            page.insert_text((30, 50), "SYNTHETIC CLOUD FIXTURE")
            data = pdf.tobytes()
        uploaded = self.client.put("/api/documents/old", base_url=ORIGIN, headers=headers,
                                   data=data, content_type="application/pdf")
        self.assertEqual(uploaded.status_code, 200)
        document = uploaded.get_json()["document"]
        path = f"/api/documents/{document['id']}/pages/1?width=300"
        preview = self.client.get(path, base_url=ORIGIN, headers=self.headers)
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.data.startswith(b"\x89PNG"))
        other_browser = self.app.test_client()
        other_browser.get("/api/bootstrap", base_url=ORIGIN, headers=self.headers)
        self.assertEqual(other_browser.get(path, base_url=ORIGIN, headers=self.headers).status_code, 404)
        self.assertEqual(self.app.extensions["review_store"].jobs, {})

    def test_cloud_configuration_rejects_insecure_origins_and_empty_allowlist(self):
        for url in ("http://synthetic.azurewebsites.net", ORIGIN + "/path", ORIGIN + "?query",
                    ORIGIN + "#fragment", "https://user@synthetic.azurewebsites.net",
                    "https://127.0.0.1", ORIGIN + ":8080"):
            with self.assertRaises(ValueError):
                CloudBoundary(url, TENANT, frozenset((USER,)))
        with self.assertRaises(ValueError):
            CloudBoundary(ORIGIN, TENANT, frozenset())
        with self.assertRaises(ValueError):
            CloudBoundary(ORIGIN, TENANT, frozenset(("not-an-id",)))

    def test_conflicting_claims_fail_closed(self):
        for conflicting in ("tid", "oid"):
            with self.assertRaises(ValueError):
                self.boundary.authenticate(identity(claims=[
                    {"typ": "tid", "val": TENANT}, {"typ": "oid", "val": USER},
                    {"typ": conflicting, "val": OTHER}]))


class AppServiceEntrypointTests(unittest.TestCase):
    def env(self):
        return {"CU_PUBLIC_ORIGIN": ORIGIN, "CU_TENANT_ID": TENANT,
                "CU_ALLOWED_PRINCIPALS": USER, "WEBSITE_HOSTNAME": "synthetic.azurewebsites.net",
                "WEBSITE_AUTH_ENABLED": "True",
                "CU_CONFIG_JSON": json.dumps({"auth": {"mode": "azure_cli"}})}

    def test_startup_requires_platform_hostname_and_easy_auth(self):
        for setting, value in (("WEBSITE_HOSTNAME", "wrong.example"),
                               ("WEBSITE_AUTH_ENABLED", "false"),
                               ("CU_ALLOWED_PRINCIPALS", ""), ("CU_CONFIG_JSON", "[]")):
            with self.subTest(setting=setting), patch("cu_diff.appservice.create_app") as create:
                with self.assertRaises(ValueError):
                    application(self.env() | {setting: value})
                create.assert_not_called()

    def test_cloud_forces_deterministic_identity_and_private_persistent_directories(self):
        app = Mock()
        app.extensions = {"review_store": Mock()}
        with patch("cu_diff.appservice.create_app", return_value=app) as create, \
                patch("cu_diff.appservice.atexit.register") as register:
            self.assertIs(application(self.env()), app)
            args, kwargs = create.call_args
            self.assertEqual(args[0]["auth"], {"mode": "managed_identity"})
            self.assertEqual(args[1:], (Path("/home/cu-review/data"), Path("/home/cu-review/cache")))
            self.assertTrue(kwargs["allow_azure"])
            self.assertEqual(kwargs["cloud"].allowed_principals, frozenset((USER,)))
            register.assert_called_once_with(app.extensions["review_store"].close)


@unittest.skipUnless(importlib.util.find_spec("azure") and importlib.util.find_spec("azure.identity"),
                     "Install .[appservice] for managed identity tests")
class ManagedIdentityTests(unittest.TestCase):
    def client(self, mode="managed_identity"):
        return Client({"endpoint": "https://synthetic.services.ai.azure.com", "extraction_profile": "layout",
                       "auth": {"mode": mode}})

    def test_identity_uses_ai_scope_caches_then_refreshes_before_expiry(self):
        with patch("azure.identity.ManagedIdentityCredential") as factory, \
                patch("cu_diff.client.subprocess.run") as cli, patch("cu_diff.client.time.time") as now:
            factory.return_value.get_token.side_effect = [
                SimpleNamespace(token="synthetic-token-1", expires_on=1000),
                SimpleNamespace(token="synthetic-token-2", expires_on=2000)]
            now.return_value = 500
            client = self.client()
            self.assertEqual(client._authenticate(), "synthetic-token-1")
            self.assertEqual(client._authenticate(), "synthetic-token-1")
            now.return_value = 900
            self.assertEqual(client._authenticate(), "synthetic-token-2")
            factory.assert_called_once_with()
            self.assertEqual(factory.return_value.get_token.call_count, 2)
            factory.return_value.get_token.assert_called_with("https://cognitiveservices.azure.com/.default")
            cli.assert_not_called()

    def test_identity_failure_does_not_fall_back_to_cli_or_disclose_details(self):
        from azure.core.exceptions import ClientAuthenticationError
        with patch("azure.identity.ManagedIdentityCredential") as factory, \
                patch("cu_diff.client.subprocess.run") as cli:
            factory.return_value.get_token.side_effect = ClientAuthenticationError("private diagnostic")
            with self.assertRaises(CUError) as raised:
                self.client()._authenticate()
            self.assertNotIn("private diagnostic", str(raised.exception))
            cli.assert_not_called()
        with self.assertRaises(ValueError):
            self.client("invalid")
