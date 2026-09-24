"""GA REST client; Entra via Azure CLI, no shared default mutations."""

import base64
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .schema import definition, extraction_profile, layout_diagnostics


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class CUError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CacheMiss(CUError):
    """No completed or resumable operation matches the requested provenance."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CUError("Refusing HTTP redirect of authenticated Azure request")


class Client:
    supports_parallel_cu = True

    def __init__(self, config: dict):
        self.extraction_profile = extraction_profile(config)
        self.config = config
        self.endpoint = config["endpoint"].rstrip("/")
        parsed = urllib.parse.urlsplit(self.endpoint)
        if (parsed.scheme != "https" or parsed.username or parsed.query
                or parsed.path or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError("Endpoint must be an HTTPS origin, without path or credentials")
        if not parsed.hostname or not parsed.hostname.endswith(
            (".services.ai.azure.com", ".cognitiveservices.azure.com")
        ):
            raise ValueError("Only public Azure AI resource endpoints are accepted")
        self.api_version = config.get("api_version", "2025-11-01")
        self.timeout = config.get("timeout_seconds", 1800)
        if self.extraction_profile == "engineering" and not config.get("deployment_versions"):
            raise ValueError("Record actual deployment model versions/SKUs for cache provenance")
        self.events: list[dict] = []
        self.usage_records: list[dict] = []
        self._usage_local = threading.local()
        self.usage_context: dict = {}
        self.usage_observer = None
        self._usage_lock = threading.RLock()
        self._auth_lock = threading.RLock()
        self._cu_locks_lock = threading.RLock()
        self._cu_locks = {}
        self._cu_unknown = set()
        self._token = ""
        self._token_at = 0.0

    def _authenticate(self) -> str:
        with self._auth_lock:
            return self._authenticate_locked()

    def _authenticate_locked(self) -> str:
        if time.monotonic() - self._token_at > 2400 or not self._token:
            az = shutil.which("az")
            if not az:
                raise CUError("Azure CLI missing. Install it and run az login.")
            proc = subprocess.run(
                [az, "account", "get-access-token", "--resource",
                 "https://cognitiveservices.azure.com", "--query", "accessToken", "-o", "tsv"],
                capture_output=True, text=True, check=False,
            )
            if proc.returncode:
                raise CUError("Azure CLI Entra authentication failed; run az login.")
            self._token = proc.stdout.strip()
            self._token_at = time.monotonic()
        return self._token

    @property
    def usage_context(self):
        return getattr(self._usage_local, "context", {})

    @usage_context.setter
    def usage_context(self, context):
        self._usage_local.context = deepcopy(context)

    @contextmanager
    def usage_scope(self, context):
        previous = self.usage_context
        self.usage_context = context
        try:
            yield
        finally:
            self.usage_context = previous

    def url(self, path: str) -> str:
        return f"{self.endpoint}/contentunderstanding/{path}?api-version={self.api_version}"

    def request(self, method: str, url: str, body: dict | None = None,
                extra_headers: dict | None = None) -> tuple[dict, dict]:
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(self.endpoint).netloc:
            raise CUError("Refusing to send Azure token to another origin")
        request_id = str(uuid.uuid4())
        headers = {"Authorization": "Bearer " + self._authenticate(),
                   "Content-Type": "application/json", "x-ms-client-request-id": request_id}
        headers.update(extra_headers or {})
        request = urllib.request.Request(
            url, data=canonical(body) if body is not None else None,
            headers=headers, method=method,
        )
        start = time.monotonic()
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=180) as response:
                data = response.read()
                result = json.loads(data) if data else {}
                self.events.append({
                    "method": method, "path": urllib.parse.urlsplit(url).path,
                    "status": response.status, "request_id": request_id,
                    "service_request_id": response.headers.get("apim-request-id"),
                    "latency_seconds": round(time.monotonic() - start, 3),
                })
                return result, dict(response.headers)
        except urllib.error.HTTPError as error:
            # Never log request bodies, authorization headers or raw customer responses.
            message = error.read().decode("utf-8", errors="replace")
            self.events.append({"method": method, "status": error.code,
                                "request_id": request_id, "error_body": message})
            raise CUError(f"Azure CU HTTP {error.code}: {message}", error.code) from error
        except urllib.error.URLError as error:
            raise CUError(f"Azure CU transport failure: {error.reason}") from error

    def poll(self, url: str, *, usage_entry=None) -> dict:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            response, headers = self.request("GET", url)
            if usage_entry is not None and "usage" in response:
                from .usage import finish_usage
                finish_usage(self, usage_entry, response, outcome="poll_response")
            status = response.get("status", "").lower()
            if status in ("succeeded", "ready"):
                return response
            if status in ("failed", "canceled", "cancelled"):
                raise CUError("Azure CU operation failed: " + json.dumps(response.get("error", response)))
            time.sleep(min(float(headers.get("Retry-After", "3")), 30))
        raise CUError("Azure CU polling timed out; rerun to resume the saved operation, not rebill")

    def ensure_analyzer(self, *, allow_create: bool = True) -> tuple[str, dict]:
        profile = extraction_profile(self.config)
        if profile == "engineering":
            base, _ = self.request("GET", self.url("analyzers/prebuilt-document"))
            supported = base.get("supportedModels", {}).get("completion", [])
            if self.config["completion_model"] not in supported:
                raise CUError(
                    f"Requested model {self.config['completion_model']} is not supported by "
                    f"CU prebuilt-document on this resource. Supported: {', '.join(supported)}. "
                    "No model substitution or resource changes performed."
                )
        # CU validates defaults at creation, before request-level overrides exist.
        # The built-in alias lets each analyze request select its approved deployment.
        spec = definition("prebuilt-analyzer-completion", profile=profile)
        prefix = "engineering.evidence." if profile == "engineering" else "engineering.layout."
        analyzer_id = prefix + digest(canonical(spec))[:16]
        url = self.url("analyzers/" + analyzer_id)
        try:
            actual, _ = self.request("GET", url)
        except CUError as error:
            if error.status != 404:
                raise
            if not allow_create:
                raise CUError(
                    "The configured analyzer does not exist. Web mode never creates analyzers; "
                    "use an approved CLI setup before running the web application."
                ) from error
            actual, headers = self.request("PUT", url, spec, {"If-None-Match": "*"})
            operation = next((v for k, v in headers.items() if k.lower() == "operation-location"), None)
            if operation:
                self.poll(operation)
            actual, _ = self.request("GET", url)
        if actual.get("status", "").lower() != "ready":
            raise CUError(f"Analyzer {analyzer_id} is not ready")
        if profile == "layout":
            self._validate_layout_dependencies(actual)
        for key in ("models", "fieldSchema"):
            actual_value = actual.get(key)
            if profile == "layout" and (
                    actual_value is None
                    or key == "fieldSchema" and actual_value == {"fields": {}}
                    or key == "models" and actual_value == {"embedding": "prebuilt-analyzer-embedding"}):
                actual_value = {}
            if actual_value != spec[key]:
                raise CUError(f"Existing analyzer {key} differs from expected immutable definition")
        for key, value in spec["config"].items():
            if actual.get("config", {}).get(key) != value:
                raise CUError(f"Existing analyzer config differs: {key}")
        return analyzer_id, actual

    @staticmethod
    def _validate_layout_dependencies(actual):
        # GA inherits this embedding alias even for an empty custom field schema.
        # It is acceptable only without any configured consumer of embeddings.
        for key in ("knowledgeSources", "trainingData"):
            if actual.get(key) not in (None, [], {}):
                raise CUError(f"Layout analyzer must not configure {key}")
        config = actual.get("config", {})
        for key in ("enableSegment", "enableChunking", "enableEmbeddings"):
            if config.get(key, False) is not False:
                raise CUError(f"Layout analyzer must not enable {key}")
        if config.get("contentCategories") not in (None, [], {}):
            raise CUError("Layout analyzer must not configure contentCategories")

    def analyze(self, path: Path, cache: Path, analyzer_id: str,
                analyzer: dict, *, allow_submit: bool = True) -> tuple[dict, dict]:
        profile = extraction_profile(self.config)
        binary = path.read_bytes()
        deployments = {}
        if profile == "engineering":
            deployments = dict(self.config["model_deployments"])
            deployments["prebuilt-analyzer-completion"] = deployments[self.config["completion_model"]]
        provenance = {
            "document_sha256": digest(binary), "byte_length": len(binary),
            "endpoint": self.endpoint, "api_version": self.api_version,
            "analyzer_id": analyzer_id, "analyzer": analyzer,
            "model_deployments": deployments,
            "selected_completion_model": self.config["completion_model"] if profile == "engineering" else None,
            "deployment_versions": self.config["deployment_versions"] if profile == "engineering" else {},
            "processing_location": self.config["processing_location"],
        }
        if profile == "layout":
            provenance["extraction_profile"] = "layout"
        key = digest(canonical(provenance))
        identity = (cache.resolve(), key)
        with self._cu_locks_lock:
            lock = self._cu_locks.setdefault(identity, threading.RLock())
        with lock:
            return self._analyze_cached(
                binary, cache, analyzer_id, deployments, provenance, key, allow_submit)

    def _analyze_cached(self, binary, cache, analyzer_id, deployments, provenance, key,
                        allow_submit):
        from .usage import begin_usage, finish_usage

        raw_path = cache / f"{key}.response.json"
        meta_path = cache / f"{key}.metadata.json"
        pending_path = cache / f"{key}.operation.json"
        usage_metadata = {k: provenance[k] for k in (
            "model_deployments", "selected_completion_model", "deployment_versions")}
        if provenance.get("extraction_profile") == "layout":
            usage_metadata["extraction_profile"] = "layout"
        if raw_path.exists() and meta_path.exists():
            usage_entry = begin_usage(self, "cu", key, "cached", usage_metadata)
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            finish_usage(self, usage_entry, raw)
            if raw.get("status", "").lower() != "succeeded":
                raise CUError("Cache contains an unsuccessful operation")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if provenance.get("extraction_profile") == "layout":
                meta["extraction_contract"] = layout_diagnostics(raw)
                self._validate_layout_geometry(meta)
            return raw, dict(meta, cache_hit=True, cache_key=key)
        start = time.monotonic()
        if pending_path.exists():
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            location = pending["operation_location"]
            started_at = pending["started_at"]
            usage_entry = begin_usage(self, "cu", key, "resumed", usage_metadata)
        else:
            if not allow_submit:
                raise CacheMiss(
                    "No matching CU cache. This local server is cache-only; restart with "
                    "--allow-azure-upload only after approving the configured Azure processing boundary."
                )
            identity = (cache.resolve(), key)
            with self._cu_locks_lock:
                if identity in self._cu_unknown:
                    raise CUError("CU submission outcome unknown in this client; "
                                  "inspect the earlier operation before resubmitting")
            started_at = datetime.now(timezone.utc).isoformat()
            body = {
                "inputs": [{"data": base64.b64encode(binary).decode("ascii"),
                            "mimeType": "application/pdf", "name": "drawing.pdf"}],
            }
            if provenance.get("extraction_profile") != "layout":
                body["modelDeployments"] = deployments
            url = self.url(f"analyzers/{analyzer_id}:analyze")
            url += "&processingLocation=" + urllib.parse.quote(self.config["processing_location"])
            usage_entry = begin_usage(self, "cu", key, "new", usage_metadata)
            received = False
            with self._cu_locks_lock:
                self._cu_unknown.add(identity)
            try:
                _, headers = self.request("POST", url, body)
                received = True
            finally:
                if not received:
                    finish_usage(self, usage_entry, outcome="request_failed_or_unknown", cache_state="unknown")
            location = next((v for k, v in headers.items() if k.lower() == "operation-location"), None)
            if not location:
                raise CUError("Accepted operation did not provide Operation-Location")
            save_json(pending_path, {"operation_location": location, "started_at": started_at})
            with self._cu_locks_lock:
                self._cu_unknown.discard(identity)
        completed = False
        try:
            raw = self.poll(location, usage_entry=usage_entry)
            completed = True
        finally:
            if not completed:
                finish_usage(self, usage_entry, outcome="operation_incomplete")
        finish_usage(self, usage_entry, raw)
        if not raw.get("result", {}).get("contents"):
            raise CUError("Succeeded operation has no contents; cannot report no changes")
        meta = dict(provenance, started_at=started_at, operation_location=location,
                    elapsed_this_run_seconds=round(time.monotonic() - start, 3),
                    usage=raw.get("usage"), cache_key=key, cache_hit=False)
        if provenance.get("extraction_profile") == "layout":
            meta["extraction_contract"] = layout_diagnostics(raw)
        save_json(raw_path, raw)
        save_json(meta_path, meta)
        pending_path.unlink()
        self._validate_layout_geometry(meta)
        return raw, meta

    @staticmethod
    def _validate_layout_geometry(metadata):
        contract = metadata.get("extraction_contract")
        if contract is not None and not contract["geometry_valid"]:
            raise CUError("Layout extraction is missing valid page geometry; "
                          "the response is retained in cache, not treated as no changes")
