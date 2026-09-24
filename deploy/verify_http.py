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
        return response.code, response.headers.get("Location", ""), response.read(4096)


def verify(origin, tenant, *, attempts=30, delay=5):
    parsed = urllib.parse.urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
            or parsed.fragment or parsed.username or parsed.port not in (None, 443)):
        raise ValueError("Expected a public HTTPS origin")
    last = "not checked"
    for attempt in range(attempts):
        try:
            status, _, content = fetch(origin + "/api/health")
            last = f"HTTP {status}"
            if status == 200 and json.loads(content) == {"local_only": False, "status": "ok"}:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last = type(error).__name__
        if attempt + 1 < attempts:
            time.sleep(delay)
    else:
        raise RuntimeError("Public application health did not pass: " + last)
    results = {"health": 200}
    for path in ("/", "/api/bootstrap"):
        status, location, _ = fetch(origin + path, html=True)
        if status in (301, 302, 303, 307, 308):
            destination = urllib.parse.urlsplit(urllib.parse.urljoin(origin, location))
            if (destination.scheme != "https" or destination.netloc != parsed.netloc
                    or destination.path != "/.auth/login/aad"):
                raise RuntimeError("Anonymous request redirected outside the application login boundary")
        elif status not in (401, 403):
            raise RuntimeError(f"Anonymous {path} was not protected: HTTP {status}")
        results[path] = status
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
    args = parser.parse_args()
    print(json.dumps(verify(args.origin, args.tenant)))
