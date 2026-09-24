import unittest
from unittest.mock import patch

from deploy.verify_http import verify


ORIGIN = "https://synthetic.azurewebsites.net"
TENANT = "11111111-1111-1111-1111-111111111111"
HEALTH = (200, "", b'{"local_only":false,"status":"ok"}')
DENIED = (401, "", b"")
LOGIN = (302, f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize", b"")


class DeploymentHttpTests(unittest.TestCase):
    def test_real_health_and_protected_login_are_all_required(self):
        with patch("deploy.verify_http.fetch", side_effect=[HEALTH, DENIED, DENIED, LOGIN]):
            self.assertEqual(verify(ORIGIN, TENANT, attempts=1),
                             {"health": 200, "/": 401, "/api/bootstrap": 401, "entra_login": 302})

    def test_protected_browser_redirect_allowed_but_not_external_or_http(self):
        redirect = (302, "/.auth/login/aad?post_login_redirect_uri=%2F", b"")
        with patch("deploy.verify_http.fetch", side_effect=[HEALTH, redirect, DENIED, LOGIN]):
            self.assertEqual(verify(ORIGIN, TENANT, attempts=1)["/"], 302)
        for location in ("https://unapproved.example/", ORIGIN.replace("https:", "http:") + "/.auth/login/aad"):
            with self.subTest(location=location), patch("deploy.verify_http.fetch",
                    side_effect=[HEALTH, (302, location, b"")]):
                with self.assertRaises(RuntimeError):
                    verify(ORIGIN, TENANT, attempts=1)

    def test_anonymous_application_access_is_failure(self):
        with patch("deploy.verify_http.fetch", side_effect=[HEALTH, (200, "", b"app")]):
            with self.assertRaisesRegex(RuntimeError, "not protected"):
                verify(ORIGIN, TENANT, attempts=1)

    def test_transient_startup_retry_does_not_accept_wrong_or_local_health(self):
        for invalid in ((503, "", b""), (200, "", b'{"local_only":true,"status":"ok"}'),
                        (200, "", b"not JSON")):
            with self.subTest(invalid=invalid), patch("deploy.verify_http.fetch",
                    side_effect=[invalid, HEALTH, DENIED, DENIED, LOGIN]), patch("deploy.verify_http.time.sleep"):
                self.assertEqual(verify(ORIGIN, TENANT, attempts=2)["health"], 200)
        with patch("deploy.verify_http.fetch", return_value=(503, "", b"")):
            with self.assertRaisesRegex(RuntimeError, "health did not pass"):
                verify(ORIGIN, TENANT, attempts=1)

    def test_wrong_tenant_login_is_failure(self):
        with patch("deploy.verify_http.fetch",
                   side_effect=[HEALTH, DENIED, DENIED, (302, "https://login.microsoftonline.com/common/authorize", b"")]):
            with self.assertRaisesRegex(RuntimeError, "Tenant-specific"):
                verify(ORIGIN, TENANT, attempts=1)

    def test_insecure_origin_rejected_before_requests(self):
        with patch("deploy.verify_http.fetch") as fetch:
            for origin in ("http://synthetic.azurewebsites.net", "https://", ORIGIN + "/path",
                           "https://user@synthetic.azurewebsites.net", ORIGIN + ":8000"):
                with self.assertRaises(ValueError):
                    verify(origin, TENANT, attempts=1)
            fetch.assert_not_called()
