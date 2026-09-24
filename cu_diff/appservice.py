"""Single-process App Service entrypoint; never use the local CLI as a public server."""

import atexit
import json
import os
from pathlib import Path

from .hosting import CloudBoundary
from .demo_auth import DemoAuth
from .web import MAX_BYTES, create_app


def application(environ=None):
    env = os.environ if environ is None else environ
    cloud = CloudBoundary(env["CU_PUBLIC_ORIGIN"], env["CU_TENANT_ID"],
                          frozenset(env["CU_ALLOWED_PRINCIPALS"].split(",")))
    if cloud.host != env.get("WEBSITE_HOSTNAME"):
        raise ValueError("Public origin must match the App Service hostname")
    mode = env.get("CU_WEB_AUTH_MODE", "entra")
    if mode not in ("entra", "demo"):
        raise ValueError("CU_WEB_AUTH_MODE must be entra or demo")
    if mode == "entra" and env.get("WEBSITE_AUTH_ENABLED", "").lower() != "true":
        raise ValueError("App Service Easy Auth must be enabled before startup")
    demo = DemoAuth(env["CU_DEMO_USERNAME"], env["CU_DEMO_PASSWORD_HASH"]) if mode == "demo" else None
    config = json.loads(env["CU_CONFIG_JSON"])
    if not isinstance(config, dict):
        raise ValueError("CU_CONFIG_JSON must be a JSON object")
    config["auth"] = {"mode": "managed_identity"}
    app = create_app(config, Path("/home/cu-review/data"), Path("/home/cu-review/cache"),
                     allow_azure=True, cloud=cloud, demo_auth=demo)
    atexit.register(app.extensions["review_store"].close)
    return app


def main():
    from waitress import serve
    app = application()
    serve(app, host="0.0.0.0", port=8000, threads=4,
          max_request_body_size=MAX_BYTES, channel_timeout=60)


if __name__ == "__main__":
    main()
