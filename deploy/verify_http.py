"""Verify public health and fail-closed authentication without credentials."""

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, *, html=False):
    headers = {"Accept": "text/html" if html else "application/json"}
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.build_opener(NoRedirect()).open(request, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.code, response.headers.get("Location", ""), response.read(16384)


def verify(origin, tenant, *, attempts=60, delay=5, auth_mode="entra", health_only=False):
    if auth_mode not in ("entra", "demo"):
        raise ValueError("Unknown authentication mode")
    parsed = urllib.parse.urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
            or parsed.fragment or parsed.username or parsed.port not in (None, 443)):
        raise ValueError("Expected a public HTTPS origin")
    last = "not checked"
    expected_health = {"local_only": False, "status": "ok"}
    if auth_mode == "demo":
        expected_health["auth_mode"] = "demo"
    for attempt in range(attempts):
        try:
            status, _, content = fetch(origin + "/api/health")
            last = f"HTTP {status}"
            if status == 200 and json.loads(content) == expected_health:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last = type(error).__name__
        if attempt + 1 < attempts:
            time.sleep(delay)
    else:
        raise RuntimeError("Public application health did not pass: " + last)
    results = {"health": 200}
    if health_only:
        return results
    if auth_mode == "demo":
        # Gateway changes can lag behind ARM read-back and application health.
        last = "not checked"
        for attempt in range(attempts):
            try:
                status, _, content = fetch(origin + "/login", html=True)
                last = f"HTTP {status}"
                if status == 200:
                    if b'name="csrf"' not in content or b'type="password"' not in content:
                        raise RuntimeError("Demo login returned HTTP 200 without the expected form")
                    break
                if status not in (301, 302, 303, 307, 308, 401, 403, 502, 503, 504):
                    raise RuntimeError("Demo login failed: " + last)
            except (urllib.error.URLError, TimeoutError) as error:
                last = type(error).__name__
            if attempt + 1 < attempts:
                time.sleep(delay)
        else:
            raise RuntimeError("Demo login form did not become available: " + last)
        results["demo_login"] = status
        results["demo_login_attempts"] = attempt + 1
    for path in ("/", "/api/bootstrap"):
        status, location, _ = fetch(origin + path, html=True)
        if status in (301, 302, 303, 307, 308):
            destination = urllib.parse.urlsplit(urllib.parse.urljoin(origin, location))
            if (destination.scheme != "https" or destination.netloc != parsed.netloc
                    or destination.path != ("/login" if auth_mode == "demo" else "/.auth/login/aad")
                    or (auth_mode == "demo" and path != "/")):
                raise RuntimeError("Anonymous request redirected outside the application login boundary")
        elif status not in (401, 403):
            raise RuntimeError(f"Anonymous {path} was not protected: HTTP {status}")
        results[path] = status
    if auth_mode == "demo":
        return results
    status, location, _ = fetch(origin + "/.auth/login/aad", html=True)
    destination = urllib.parse.urlsplit(location)
    if (status != 302 or destination.scheme != "https"
            or destination.netloc != "login.microsoftonline.com"
            or not destination.path.startswith("/" + tenant + "/")):
        raise RuntimeError("Tenant-specific Entra login initiation failed")
    results["entra_login"] = status
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--auth-mode", choices=("entra", "demo"), default="entra")
    parser.add_argument("--health-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.origin, args.tenant, auth_mode=args.auth_mode, health_only=args.health_only)))
