"""Opt-in real Chromium form/cookie regression against a loopback TLS fixture."""

import os
from pathlib import Path
import tempfile
import socket
import threading
import unittest
from unittest.mock import Mock

if os.environ.get("CU_BROWSER_TESTS") != "1":
    raise unittest.SkipTest("Set CU_BROWSER_TESTS=1 to run Chromium tests")

from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

from cu_diff.demo_auth import DemoAuth
from cu_diff.hosting import CloudBoundary
from cu_diff.web import create_app


class DemoBrowserTests(unittest.TestCase):
    def test_real_browser_login_logout_and_mobile_layout(self):
        with socket.socket() as socket_:
            socket_.bind(("127.0.0.1", 0))
            port = socket_.getsockname()[1]
        origin = f"https://127.0.0.1:{port}"
        password = "synthetic-browser-fixture-only"
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright:
            root = Path(directory)
            app = create_app({"completion_model": "synthetic"}, root / "data", root / "cache",
                cloud=Mock(spec=CloudBoundary, origin=origin, host=f"127.0.0.1:{port}"),
                demo_auth=DemoAuth("demo", generate_password_hash(password, method="scrypt:32768:8:1")))
            server = make_server("127.0.0.1", port, app, ssl_context="adhoc", threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport={"width": 1200, "height": 850}, ignore_https_errors=True)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin)
                page.wait_for_load_state("networkidle")
                self.assertEqual(page.url, origin + "/login")
                page.get_by_label("用户名", exact=True).fill("demo")
                page.get_by_label("密码", exact=True).fill("wrong")
                with page.expect_response(lambda response: response.request.method == "POST") as failed_login:
                    page.get_by_role("button", name="进入图纸对比").click()
                self.assertEqual(failed_login.value.status, 401,
                                 failed_login.value.text() + " origin=" + str(failed_login.value.request.header_value("origin")))
                page.get_by_role("alert").wait_for()
                self.assertIn("用户名或密码错误", page.get_by_role("alert").inner_text())
                page.get_by_label("用户名", exact=True).fill("demo")
                page.get_by_label("密码", exact=True).fill(password)
                page.get_by_role("button", name="进入图纸对比").click()
                page.get_by_text("会话已连接", exact=True).wait_for()
                self.assertTrue(page.locator("#logout-button").is_visible())
                page.locator("#logout-button").click()
                page.wait_for_url(origin + "/login")
                page.set_viewport_size({"width": 390, "height": 844})
                self.assertTrue(page.get_by_label("密码", exact=True).is_visible())
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > window.innerWidth"))
                self.assertEqual(errors, [])
            finally:
                browser.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                app.extensions["review_store"].close()
