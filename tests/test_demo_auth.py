import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from werkzeug.security import generate_password_hash

from cu_diff.appservice import application
from cu_diff.demo_auth import COOKIE, LOGIN_TTL, MAX_ATTEMPTS, MAX_SESSIONS, DemoAuth, DemoAuthError
from cu_diff.hosting import CloudBoundary
from cu_diff.web import create_app
from deploy.create_demo_login import generate


ORIGIN = "https://synthetic.azurewebsites.net"
TENANT = "11111111-1111-1111-1111-111111111111"
USER = "22222222-2222-2222-2222-222222222222"
PASSWORD = "synthetic-test-password-not-a-real-credential"


class DemoLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash(PASSWORD, method="scrypt:32768:8:1")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.auth = DemoAuth("demo", self.password_hash)
        self.app = create_app({"completion_model": "synthetic"}, root / "data", root / "cache",
                              cloud=CloudBoundary(ORIGIN, TENANT, frozenset((USER,))), demo_auth=self.auth)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["review_store"].close()
        self.temp.cleanup()

    def challenge(self, client=None):
        client = client or self.client
        response = client.get("/login", base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        return re.search(rb'name="csrf" value="([^"]+)"', response.data)[1].decode()

    def login(self, client=None, **data):
        client = client or self.client
        form = {"csrf": self.challenge(client), "username": "demo", "password": PASSWORD} | data
        return client.post("/login", data=form, base_url=ORIGIN, headers={"Origin": ORIGIN})

    def test_anonymous_cannot_access_api_assets_documents_or_forged_entra_identity(self):
        self.assertEqual(self.client.get("/", base_url=ORIGIN).location, "/login")
        self.assertEqual(self.client.get("/api/health", base_url=ORIGIN).json["auth_mode"], "demo")
        for path in ("/api/bootstrap", "/api/jobs/fake", "/api/documents/fake/pages/1",
                     "/static/app.js", "/static/login.html", "/.auth/login/aad"):
            self.assertEqual(self.client.get(path, base_url=ORIGIN,
                             headers={"X-MS-CLIENT-PRINCIPAL": "forged"}).status_code, 401)
        self.assertEqual(self.client.post("/api/compare", base_url=ORIGIN).status_code, 401)
        self.assertEqual(self.client.get("/login", base_url=ORIGIN,
                                        headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_success_rotates_session_sets_secure_cookie_and_preserves_csrf(self):
        csrf = self.challenge()
        old_cookie = self.client.get_cookie(COOKIE, domain="synthetic.azurewebsites.net").value
        response = self.client.post("/login", base_url=ORIGIN,
                                    data={"csrf": csrf, "username": "demo", "password": PASSWORD})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, "/")
        self.assertIsNone(self.auth.get(old_cookie))
        cookie = response.headers.getlist("Set-Cookie")[0]
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={LOGIN_TTL}"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Domain=", cookie)
        with self.client.get("/", base_url=ORIGIN) as page:
            self.assertEqual(page.status_code, 200)
        boot = self.client.get("/api/bootstrap", base_url=ORIGIN)
        self.assertEqual(boot.json["auth_mode"], "demo")
        self.assertNotIn(self.password_hash.encode(), boot.data)
        self.assertEqual(self.client.post("/api/logout", base_url=ORIGIN).status_code, 403)
        self.assertEqual(self.client.post("/api/compare", base_url=ORIGIN,
            json={"revision": 0}, headers={"X-CSRF-Token": boot.json["csrf_token"]}).status_code, 400)

    def test_wrong_credentials_generic_errors_no_secret_echo_and_rate_limit(self):
        csrf = self.challenge()
        for number in range(MAX_ATTEMPTS):
            form = {"csrf": csrf, "username": "demo" if number % 2 else "unknown", "password": "incorrect"}
            response = self.client.post("/login", base_url=ORIGIN, data=form)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(b"incorrect", response.data)
            self.assertNotIn(self.password_hash.encode(), response.data)
        response = self.client.post("/login", base_url=ORIGIN,
                                    data={"csrf": csrf, "username": "demo", "password": PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "60")
        with patch("cu_diff.demo_auth.time.time", return_value=time.time() + 61):
            self.assertEqual(self.client.post("/login", base_url=ORIGIN,
                data={"csrf": csrf, "username": "demo", "password": PASSWORD}).status_code, 303)

    def test_login_csrf_and_size_limit(self):
        self.assertEqual(self.login(csrf="wrong").status_code, 403)
        self.assertEqual(self.client.post("/login", base_url=ORIGIN, data="x" * 4097).status_code, 413)
        with patch("cu_diff.demo_auth.check_password_hash") as check:
            self.assertEqual(self.app.test_client().post("/login", base_url=ORIGIN,
                data={"username": "demo", "password": PASSWORD}).status_code, 403)
            check.assert_not_called()

    def test_logout_revokes_cookie_and_a_new_login_cannot_reuse_review_session(self):
        self.assertEqual(self.login().status_code, 303)
        boot = self.client.get("/api/bootstrap", base_url=ORIGIN).json
        login_cookie = self.client.get_cookie(COOKIE, domain="synthetic.azurewebsites.net").value
        review_cookie = self.client.get_cookie("__Host-cu_review", domain="synthetic.azurewebsites.net").value
        response = self.client.post("/api/logout", base_url=ORIGIN,
                                    headers={"X-CSRF-Token": boot["csrf_token"]})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.auth.get(login_cookie))
        self.assertEqual(len(self.app.extensions["review_store"].sessions), 0)
        self.client.set_cookie(COOKIE, login_cookie, domain="synthetic.azurewebsites.net")
        self.assertEqual(self.client.get("/api/bootstrap", base_url=ORIGIN).status_code, 401)
        self.assertEqual(self.login().status_code, 303)
        self.client.set_cookie("__Host-cu_review", review_cookie, domain="synthetic.azurewebsites.net")
        reset = self.client.get("/api/bootstrap", base_url=ORIGIN)
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json["documents"], {"old": None, "new": None})
        self.assertNotEqual(self.client.get_cookie("__Host-cu_review", domain="synthetic.azurewebsites.net").value,
                            review_cookie)

    def test_expired_logins_release_review_capacity_but_running_jobs_are_retained(self):
        self.login()
        self.client.get("/api/bootstrap", base_url=ORIGIN)
        store = self.app.extensions["review_store"]
        previous = next(iter(store.sessions.values()))
        previous.job_id = "synthetic-running-job"
        store.jobs[previous.job_id] = {"status": "running"}
        now = time.time()
        with patch("cu_diff.demo_auth.time.time", return_value=now + LOGIN_TTL + 1):
            self.assertEqual(self.login().status_code, 303)
            self.assertEqual(self.client.get("/api/bootstrap", base_url=ORIGIN).status_code, 200)
            self.assertTrue(previous.retired)
            self.assertIn(previous.id, store.sessions)
            store.jobs[previous.job_id]["status"] = "succeeded"
            store.cleanup()
            self.assertNotIn(previous.id, store.sessions)
            self.assertNotIn(previous.job_id, store.jobs)
            self.assertEqual(len(store.sessions), 1)

    def test_independent_logins_cannot_borrow_review_cookie(self):
        self.login()
        self.client.get("/api/bootstrap", base_url=ORIGIN)
        cookie = self.client.get_cookie("__Host-cu_review", domain="synthetic.azurewebsites.net").value
        other = self.app.test_client()
        self.login(other)
        other.set_cookie("__Host-cu_review", cookie, domain="synthetic.azurewebsites.net")
        self.assertEqual(other.get("/api/bootstrap", base_url=ORIGIN).status_code, 403)

    def test_expiry_and_bounded_session_memory(self):
        self.login()
        with patch("cu_diff.demo_auth.time.time", return_value=time.time() + LOGIN_TTL + 1):
            self.assertEqual(self.client.get("/api/bootstrap", base_url=ORIGIN).status_code, 401)
        for _ in range(MAX_SESSIONS):
            self.auth.issue()
        with self.assertRaises(DemoAuthError):
            self.auth.issue()
        with patch("cu_diff.demo_auth.time.time", return_value=time.time() + 601):
            self.auth.issue()
            self.assertEqual(len(self.auth.sessions), 1)

    def test_entrypoint_demo_requires_valid_explicit_credentials_and_keeps_identity(self):
        env = {"CU_PUBLIC_ORIGIN": ORIGIN, "CU_TENANT_ID": TENANT, "CU_ALLOWED_PRINCIPALS": USER,
               "WEBSITE_HOSTNAME": "synthetic.azurewebsites.net", "CU_CONFIG_JSON": "{}",
               "WEBSITE_AUTH_ENABLED": "false", "CU_WEB_AUTH_MODE": "demo",
               "CU_DEMO_USERNAME": "demo", "CU_DEMO_PASSWORD_HASH": self.password_hash}
        app = Mock()
        app.extensions = {"review_store": Mock()}
        with patch("cu_diff.appservice.create_app", return_value=app) as create, \
                patch("cu_diff.appservice.atexit.register"):
            application(env)
            self.assertIsInstance(create.call_args.kwargs["demo_auth"], DemoAuth)
            self.assertEqual(create.call_args.args[0]["auth"], {"mode": "managed_identity"})
        for change in ({"CU_DEMO_PASSWORD_HASH": "plaintext"}, {"CU_DEMO_USERNAME": ""},
                       {"CU_WEB_AUTH_MODE": "invalid"}):
            with self.assertRaises(ValueError), patch("cu_diff.appservice.create_app") as create:
                application(env | change)
            create.assert_not_called()
        with self.assertRaises(KeyError):
            application({key: value for key, value in env.items() if key != "CU_DEMO_PASSWORD_HASH"})
        with self.assertRaises(ValueError):
            create_app({}, Path(self.temp.name) / "other", Path(self.temp.name) / "cache", demo_auth=self.auth)

    def test_generator_separates_plaintext_handoff_and_hash_only_settings(self):
        directory = Path(self.temp.name) / "private"
        generate(directory)
        credentials = json.loads((directory / "credentials.json").read_text())
        settings = json.loads((directory / "settings.json").read_text())
        self.assertEqual(set(settings), {"username", "password_hash"})
        self.assertNotIn(credentials["password"], json.dumps(settings))
        auth = DemoAuth(**settings)
        token, session = auth.issue()
        auth.login(token, session.csrf, credentials["username"], credentials["password"])
        with self.assertRaises(FileExistsError):
            generate(directory)
