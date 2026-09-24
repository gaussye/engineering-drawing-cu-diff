"""Loopback-only engineering drawing review server. No infrastructure deployment."""

import argparse
import atexit
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from io import BytesIO
import json
import logging
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import unquote
import uuid

from flask import Flask, Response, g, jsonify, request, send_file
import pymupdf
from werkzeug.exceptions import HTTPException

from .analysis_options import AnalysisOptions
from .client import CacheMiss, Client, CUError, digest, save_json
from .compare import compare_responses
from .timing import JobTiming, STAGES
from .web_evidence import response_geometry_matches, web_result


MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 20
MAX_PAGE_PT = 14400
SESSION_TTL = 24 * 3600
MAX_SESSIONS = 16
PDF_LOCK = threading.Lock()
LOGGER = logging.getLogger(__name__)


class WebError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status
        super().__init__(message)


@dataclass
class Document:
    id: str
    name: str
    path: Path
    analysis_path: Path
    sha256: str
    pages: list[dict]

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "sha256": self.sha256,
                "pages": self.pages, "page_count": len(self.pages),
                "rotation_normalized": any(page["rotation"] for page in self.pages)}


@dataclass
class Session:
    id: str
    csrf: str
    directory: Path
    documents: dict[str, Document] = field(default_factory=dict)
    revision: int = 0
    last_used: float = field(default_factory=time.time)
    job_id: str | None = None
    uploading: bool = False


class Store:
    def __init__(self, config: dict, root: Path, cache: Path, allow_azure: bool,
                 client_factory=Client):
        from .model_compare import options
        from .usage import validate_pricing
        self.analysis_options = AnalysisOptions(config)
        default_config, _ = self.analysis_options.snapshot()
        self.model_options = options(default_config)
        self.pricing = validate_pricing(config.get("pricing"))
        self.config, self.root, self.cache = config, root.resolve(), cache.resolve()
        self.allow_azure, self.client_factory = allow_azure, client_factory
        self.lock = threading.RLock()
        self.sessions: dict[str, Session] = {}
        self.jobs: dict[str, dict] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cu-review")
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.cleanup()

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)

    def cleanup(self):
        now = time.time()
        with self.lock:
            expired = [sid for sid, s in self.sessions.items()
                       if now - s.last_used > SESSION_TTL and not s.uploading
                       and not self.active(s)]
            for sid in expired:
                session = self.sessions.pop(sid)
                if session.job_id:
                    self.jobs.pop(session.job_id, None)
            active_dirs = {s.directory for s in self.sessions.values()}
            # Only remove recognized generated leaf files, never arbitrary root contents.
            for directory in self.root.glob("web-session-*"):
                if directory.is_symlink() or not directory.is_dir() or directory in active_dirs:
                    continue
                if now - directory.stat().st_mtime <= SESSION_TTL:
                    continue
                for file in directory.iterdir():
                    if file.is_file() and not file.is_symlink() and file.suffix in (".pdf", ".json"):
                        file.unlink()
                if not any(directory.iterdir()):
                    directory.rmdir()

    def active(self, session: Session) -> bool:
        return bool(session.job_id and self.jobs.get(session.job_id, {}).get("status")
                    in ("queued", "running"))

    def session(self, cookie: str | None, create: bool = False) -> tuple[Session, bool]:
        self.cleanup()
        with self.lock:
            if cookie in self.sessions:
                session = self.sessions[cookie]
                session.last_used = time.time()
                return session, False
            if not create:
                raise WebError("会话已过期，请刷新页面后重新上传。", 401)
            if len(self.sessions) >= MAX_SESSIONS:
                raise WebError("本地会话容量已满，请清理旧会话或稍后重试。", 429)
            sid = secrets.token_urlsafe(32)
            directory = self.root / ("web-session-" + uuid.uuid4().hex)
            directory.mkdir()
            session = Session(sid, secrets.token_urlsafe(32), directory)
            self.sessions[sid] = session
            return session, True

    def document(self, session: Session, identifier: str) -> Document:
        if len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
            raise WebError("文件标识无效。", 404)
        with self.lock:
            for document in session.documents.values():
                if secrets.compare_digest(document.id, identifier):
                    return document
        raise WebError("文件不存在或已更换，请重新选择文件。", 404)

    def invalidate(self, session: Session):
        session.revision += 1
        if session.job_id:
            job = self.jobs[session.job_id]
            job["invalidated"] = True
            job.pop("result", None)
            if job["status"] not in ("queued", "running"):
                job["status"] = "stale"

    def prune_files(self, session: Session):
        if session.uploading:
            return
        kept = {p for doc in session.documents.values() for p in (doc.path, doc.analysis_path)}
        if self.active(session):
            kept.update(self.jobs[session.job_id].get("protected_paths", []))
        for file in session.directory.glob("*.pdf"):
            if file not in kept and not file.is_symlink():
                file.unlink(missing_ok=True)

    def upload(self, session: Session, role: str, data: bytes, filename: str) -> dict:
        with self.lock:
            if session.uploading:
                raise WebError("另一个文件正在上传，请稍后重试。", 409)
            session.uploading = True
            self.invalidate(session)
            session.documents.pop(role, None)
        identifier = uuid.uuid4().hex
        path = session.directory / f"{identifier}.pdf"
        normalized = session.directory / f"{identifier}.normalized.pdf"
        try:
            if not data or len(data) > MAX_BYTES:
                raise WebError("仅支持非空、最大20MB的PDF。", 413)
            if not data.startswith(b"%PDF-"):
                raise WebError("文件内容不是有效PDF，不能仅更改扩展名。")
            with PDF_LOCK:
                try:
                    pdf = pymupdf.open(stream=data, filetype="pdf")
                except (pymupdf.FileDataError, RuntimeError) as error:
                    raise WebError("PDF无法解析，文件可能损坏。") from error
                with pdf:
                    if pdf.needs_pass:
                        raise WebError("不支持加密PDF，请先在本地解除密码保护。")
                    if not 1 <= len(pdf) <= MAX_PAGES:
                        raise WebError("PDF必须为1至20个物理页。")
                    pages = []
                    rotated = False
                    for page in pdf:
                        if (page.rect.width <= 0 or page.rect.height <= 0
                                or max(page.rect.width, page.rect.height) > MAX_PAGE_PT):
                            raise WebError("PDF页面尺寸异常，拒绝生成预览。")
                        rotation = page.rotation
                        display_equivalent = True
                        if rotation:
                            proof_scale = min(1, (2_000_000 / page.rect.get_area()) ** 0.5)
                            proof_matrix = pymupdf.Matrix(proof_scale, proof_scale)
                            before = page.get_pixmap(matrix=proof_matrix, alpha=False)
                            before_hash = digest(before.samples)
                            before_size = (before.width, before.height)
                            del before
                            page.remove_rotation()
                            after = page.get_pixmap(matrix=proof_matrix, alpha=False)
                            display_equivalent = (before_size == (after.width, after.height)
                                                  and before_hash == digest(after.samples))
                            rotated = True
                        pages.append({"number": page.number + 1,
                                      "width_pt": page.rect.width, "height_pt": page.rect.height,
                                      "rotation": rotation, "analysis_rotation": 0,
                                      "display_equivalent": display_equivalent})
                    if rotated:
                        pdf.save(normalized, no_new_id=True)
            path.write_bytes(data)
            name = unquote(filename).replace("\\", "/").split("/")[-1]
            name = "".join(char for char in name if char.isprintable())[:160] or "drawing.pdf"
            doc = Document(identifier, name, path, normalized if rotated else path,
                           digest(data), pages)
            with self.lock:
                session.directory.touch()
                session.documents[role] = doc
                return {"document": doc.public(), "revision": session.revision}
        except Exception:
            # Remove this upload's leaf files, surface the error to the request handler.
            path.unlink(missing_ok=True)
            normalized.unlink(missing_ok=True)
            raise
        finally:
            with self.lock:
                session.uploading = False
                self.prune_files(session)

    def start_job(self, session: Session, revision: int, *, use_cache=True, model=None) -> dict:
        try:
            job_config, analysis_options = self.analysis_options.snapshot(use_cache, model)
        except ValueError as error:
            raise WebError(str(error)) from error
        if not use_cache and not self.allow_azure:
            raise WebError("此服务为仅缓存模式（cache-only），不能关闭缓存；未提交任何Azure请求。", 409)
        with self.lock:
            # Snapshot server pipeline switches too; never read mutable options mid-job.
            job_config.setdefault("model_comparison", {}).update(
                {key: value for key, value in self.model_options.items()
                 if key not in ("deployment", "deployment_version")})
            if revision != session.revision:
                raise WebError("文件已更换，旧版本对比请求已失效。", 409)
            if session.uploading or set(session.documents) != {"old", "new"}:
                raise WebError("请先上传原图和调整图。")
            if session.documents["old"].sha256 == session.documents["new"].sha256:
                raise WebError("两侧文件的SHA256相同：上传的是完全相同的文件字节，请更换其中一份；未调用CU。", 409)
            if self.active(session):
                raise WebError("本会话已有分析作业；更换文件后请等待旧作业停止。", 409)
            if sum(job["status"] in ("queued", "running") for job in self.jobs.values()) >= 4:
                raise WebError("本地分析队列已满，请稍后重试。", 429)
            if session.job_id:
                self.jobs.pop(session.job_id, None)
            identifier = uuid.uuid4().hex
            self.jobs[identifier] = {
                "job_id": identifier, "status": "queued", "phase": "等待分析",
                "analysis_options": analysis_options,
                "_config": job_config, "_timing": JobTiming(),
                "revision": revision, "session_id": session.id, "invalidated": False,
                "protected_paths": [path for doc in session.documents.values()
                                    for path in (doc.path, doc.analysis_path)],
            }
            self.jobs[identifier]["timing"] = self.jobs[identifier]["_timing"].snapshot()
            session.job_id = identifier
            self.executor.submit(self.run_job, session, identifier, dict(session.documents))
            return {"job_id": identifier, "status": "queued", "revision": revision,
                    "analysis_options": dict(analysis_options),
                    "timing": self.jobs[identifier]["timing"]}

    def run_job(self, session: Session, identifier: str, documents: dict[str, Document]):
        from .usage import usage_report

        job = self.jobs[identifier]
        timing = job["_timing"]
        with self.lock:
            job_config = job.pop("_config")
        from .model_compare import options
        model_options = options(job_config)
        use_cache = job["analysis_options"]["use_cache"]
        # OFF isolates all persistent results/guards, but deduplicates within this job.
        cache = self.cache if use_cache else self.cache / "uncached-jobs" / identifier
        client = None

        def usage_changed(records):
            report = usage_report(records, self.pricing)
            with self.lock:
                job["usage_cost"] = report
                job["timing"] = timing.snapshot()

        def phase(message):
            with self.lock:
                if job["invalidated"]:
                    raise WebError("文件已更换，此作业结果已失效。", 409)
                job.update(status="running", phase=message)
                job["timing"] = timing.snapshot()

        def stage(identifier, message):
            with self.lock:
                phase(message)
                timing.stage(identifier)
                job["timing"] = timing.snapshot()

        def model_stage(identifier):
            with self.lock:
                if identifier != "model_coarse" or timing.active_stage != identifier:
                    stage(identifier, STAGES[identifier])

        try:
            with self.lock:
                timing.start()
            stage("analyzer_preparation", "核对现有CU分析器与缓存")
            client = self.client_factory(job_config)
            client.usage_observer = usage_changed
            usage_changed(getattr(client, "usage_records", None))
            # Web requests may use only an existing analyzer; never create one.
            analyzer_id, analyzer = client.ensure_analyzer(allow_create=False)
            responses, metadata = {}, {}
            for role, label in (("old", "原图"), ("new", "调整图")):
                policy = "优先复用缓存" if use_cache else "不复用历史缓存"
                stage(f"cu_full_{role}", f"正在提取{label}（{policy}，请勿关闭服务）")
                client.usage_context = {"stage": f"cu_full_{role}"}
                response, meta = self.analyze_document(
                    client, documents[role], analyzer_id, analyzer, cache=cache,
                    use_cache=use_cache)
                if response.get("status", "").lower() != "succeeded":
                    raise CUError("CU未成功，不能生成无差异结果。")
                responses[role], metadata[role] = response, meta
            semantic = None
            if model_options["enabled"]:
                from .model_compare import compare_with_model
                stage("model_coarse", STAGES["model_coarse"])
                semantic = compare_with_model(
                    documents["old"].analysis_path, documents["new"].analysis_path,
                    responses["old"], responses["new"], client=client, cache=cache,
                    analyzer_id=analyzer_id, analyzer=analyzer, allow_submit=self.allow_azure,
                    pdf_lock=PDF_LOCK, progress=phase, stage_progress=model_stage)
            stage("semantic_text_pairing", "配对BOM、字段和OCR证据")
            text_pairing = model_options["enabled"] and model_options["text_pairing"]
            comparison = compare_responses(
                responses["old"], responses["new"], semantic_pairing=text_pairing)
            if text_pairing:
                from .semantic_text import resolve_text_pairing

                comparison = resolve_text_pairing(
                    comparison, responses, {role: doc.analysis_path for role, doc in documents.items()},
                    client=client, cache=cache, allow_submit=self.allow_azure,
                    pdf_lock=PDF_LOCK, progress=phase)
            stage("local_table_comparison", "核对非BOM表格的列内容与实际网格")
            from .document_tables import compare_document_tables
            with PDF_LOCK:
                tabular = compare_document_tables(
                    documents["old"].analysis_path, documents["new"].analysis_path,
                    responses["old"], responses["new"])
            stage("local_graphics_comparison", "核对本地图形")
            from .graphics import compare_graphics
            graphical = compare_graphics(
                documents["old"].analysis_path, documents["new"].analysis_path,
                responses["old"], responses["new"], pdf_lock=PDF_LOCK, progress=phase,
                include_transformations=True)
            stage("result_preparation", "整理对比结果与用量")
            public_docs = {role: doc.public() for role, doc in documents.items()}
            result = web_result(comparison, public_docs, metadata)
            result["analysis_options"] = dict(job["analysis_options"])
            if semantic is not None:
                result["items"].extend(semantic["items"])
                result["model_coverage"] = semantic["coverage"]
                result["warnings"].extend(semantic["warnings"])
            result["items"].extend(tabular["items"])
            result["table_coverage"] = tabular["coverage"]
            result["warnings"].extend(tabular["warnings"])
            result["items"].extend(graphical["items"])
            result["graphics_coverage"] = graphical["coverage"]
            usage_changed(getattr(client, "usage_records", None))
            result["usage_cost"] = job["usage_cost"]
            with self.lock:
                timing.finish(failed=job["invalidated"])
                job["timing"] = timing.snapshot()
                job.pop("_timing", None)
                result["timing"] = job["timing"]
                if job["invalidated"]:
                    job.update(status="stale", phase="文件已更换，旧结果已作废")
                else:
                    job.update(status="succeeded", phase="对比完成；结果是待复核候选", result=result)
        except (CUError, WebError, ValueError, OSError, KeyError) as error:
            with self.lock:
                timing.finish(failed=True)
                job["timing"] = timing.snapshot()
                job.pop("_timing", None)
                job.update(status="stale" if job["invalidated"] else "failed",
                           phase="对比未完成", error=str(error))
            # Only generated job ID and exception class in server logs, never document text.
            LOGGER.warning("Job %s failed (%s)", identifier, type(error).__name__)
        except Exception as error:
            with self.lock:
                timing.finish(failed=True)
                job["timing"] = timing.snapshot()
                job.pop("_timing", None)
                job.update(status="failed", phase="对比失败",
                           error=f"服务器内部错误（{type(error).__name__}）；未生成结果，请检查本地作业记录。")
            LOGGER.error("Job %s internal failure (%s)", identifier, type(error).__name__)
        finally:
            with self.lock:
                timing.finish(failed=job["status"] != "succeeded")
                job["timing"] = timing.snapshot()
                if "result" in job:
                    job["result"]["timing"] = job["timing"]
                try:
                    usage_changed(getattr(client, "usage_records", None) if client else [])
                    save_json(session.directory / f"{identifier}.json", {
                        "status": job["status"], "error": job.get("error"),
                        "revision": job["revision"],
                        "analysis_options": job["analysis_options"],
                        "timing": job["timing"],
                        "cache_namespace": str(cache),
                        "events": client.events if client else [],
                        "usage_records": getattr(client, "usage_records", []) if client else [],
                        "usage_cost": job["usage_cost"],
                    })
                except OSError as error:
                    LOGGER.error("Job %s audit write failed (%s)", identifier, type(error).__name__)
                    job.update(status="failed", phase="审计记录保存失败",
                               error="本地审计记录无法保存；请检查磁盘空间及目录权限。")
                    job.pop("result", None)
                self.prune_files(session)

    def analyze_document(self, client: Client, document: Document,
                         analyzer_id: str, analyzer: dict, *, cache=None,
                         use_cache=True) -> tuple[dict, dict]:
        cache = self.cache if cache is None else cache
        if use_cache and document.path != document.analysis_path:
            try:
                response, metadata = client.analyze(
                    document.path, cache, analyzer_id, analyzer, allow_submit=False)
            except CacheMiss:
                pass
            else:
                if (all(page["display_equivalent"] for page in document.pages)
                        and response_geometry_matches(response, document.pages)):
                    return response, dict(metadata, coordinate_basis="original-cache-displayed-page")
        response, metadata = client.analyze(
            document.analysis_path, cache, analyzer_id, analyzer, allow_submit=self.allow_azure)
        return response, dict(metadata, coordinate_basis="rotation-normalized-page")


def create_app(config: dict, data_dir: Path, cache_dir: Path, *, port: int = 8765,
               allow_azure: bool = False, client_factory=Client) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=MAX_BYTES, JSON_AS_ASCII=False)
    app.json.ensure_ascii = False
    store = Store(config, data_dir, cache_dir, allow_azure, client_factory)
    app.extensions["review_store"] = store
    origin = f"http://127.0.0.1:{port}"
    cookie_name = f"cu_review_{port}"
    assets = Path(__file__).parent / "static"

    @app.before_request
    def boundary():
        if request.host != f"127.0.0.1:{port}":
            raise WebError("仅允许指定127.0.0.1本地地址，拒绝代理或外部Host。", 403)
        if request.headers.get("Origin") not in (None, origin):
            raise WebError("拒绝跨站请求。", 403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise WebError("拒绝跨站请求。", 403)
        if request.path.startswith("/api/") and request.path != "/api/health":
            g.review_session, g.new_session = store.session(
                request.cookies.get(cookie_name), create=request.path == "/api/bootstrap")
            if request.method in ("PUT", "POST", "DELETE"):
                if not secrets.compare_digest(request.headers.get("X-CSRF-Token", "").encode(),
                                              g.review_session.csrf.encode()):
                    raise WebError("会话校验失败，请刷新页面重试。", 403)

    @app.after_request
    def headers(response):
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' blob: data:; connect-src 'self'; font-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'none'"
            ),
        })
        if getattr(g, "new_session", False):
            response.set_cookie(cookie_name, g.review_session.id, httponly=True,
                                samesite="Strict", max_age=SESSION_TTL)
        return response

    @app.errorhandler(WebError)
    def user_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        message = "文件超过20MB大小限制。" if error.code == 413 else "请求无效或资源不存在。"
        return jsonify(error=message), error.code

    @app.errorhandler(Exception)
    def internal_error(error):
        LOGGER.error("Web request failed (%s)", type(error).__name__)
        return jsonify(error=f"本地服务错误（{type(error).__name__}），请重试或检查服务状态。"), 500

    @app.get("/")
    def index():
        return send_file(assets / "index.html")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    @app.get("/static/<name>")
    def static(name):
        if name not in ("app.css", "app.js"):
            raise WebError("资源不存在。", 404)
        return send_file(assets / name)

    @app.get("/api/health")
    def health():
        return jsonify(status="ok", local_only=True)

    @app.get("/api/bootstrap")
    def bootstrap():
        session = g.review_session
        with store.lock:
            return jsonify(
                csrf_token=session.csrf, revision=session.revision,
                limits={"max_bytes": MAX_BYTES, "max_pages": MAX_PAGES,
                        "session_ttl_hours": SESSION_TTL},
                azure_enabled=allow_azure, model=config.get("completion_model"), graphics_enabled=True,
                analysis_options=store.analysis_options.bootstrap(),
                model_comparison_enabled=store.model_options["enabled"],
                semantic_text_pairing_enabled=store.model_options["enabled"] and store.model_options["text_pairing"],
                model_comparison_deployment=(
                    store.model_options["deployment"] or
                    config.get("model_deployments", {}).get(config.get("completion_model"))),
                documents={role: session.documents[role].public() if role in session.documents else None
                           for role in ("old", "new")},
                storage_notice="文件仅存本地；会话闲置24小时后于后续请求/启动时清理。CU缓存单独保留，清理说明见README。",
            )

    @app.put("/api/documents/<role>")
    def upload(role):
        if role not in ("old", "new"):
            raise WebError("文件角色无效。")
        if request.mimetype != "application/pdf":
            raise WebError("仅接受application/pdf上传。", 415)
        return jsonify(store.upload(g.review_session, role, request.get_data(),
                                    request.headers.get("X-Filename", "drawing.pdf")))

    @app.delete("/api/documents/<role>")
    def remove(role):
        if role not in ("old", "new"):
            raise WebError("文件角色无效。")
        session = g.review_session
        with store.lock:
            if session.uploading:
                raise WebError("正在处理上传，请稍后重试。", 409)
            store.invalidate(session)
            session.documents.pop(role, None)
            store.prune_files(session)
            return jsonify(revision=session.revision)

    @app.get("/api/documents/<identifier>/pages/<int:page_number>")
    def preview(identifier, page_number):
        try:
            width = int(request.args.get("width", 1600))
        except ValueError as error:
            raise WebError("预览宽度无效。") from error
        if not 300 <= width <= 2400:
            raise WebError("预览宽度必须为300至2400像素。")
        with store.lock, PDF_LOCK:
            document = store.document(g.review_session, identifier)
            if not 1 <= page_number <= len(document.pages):
                raise WebError("页码不存在。", 404)
            with pymupdf.open(document.analysis_path) as pdf:
                page = pdf[page_number - 1]
                scale = min(width / page.rect.width, (8_000_000 / page.rect.get_area()) ** 0.5)
                image = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                data = image.tobytes("png")
        return send_file(BytesIO(data), mimetype="image/png")

    @app.post("/api/compare")
    def start_compare():
        body = request.get_json()
        if not isinstance(body, dict) or type(body.get("revision")) is not int:
            raise WebError("缺少有效文件版本，请刷新页面。")
        if "model" in body and not isinstance(body["model"], str):
            raise WebError("model必须为服务端已批准的模型标识。")
        return jsonify(store.start_job(
            g.review_session, body["revision"], use_cache=body.get("use_cache", True),
            model=body.get("model"))), 202

    @app.get("/api/jobs/<identifier>")
    def job_status(identifier):
        with store.lock:
            job = store.jobs.get(identifier)
            if not job or job["session_id"] != g.review_session.id:
                raise WebError("作业不存在或不属于当前会话。", 404)
            if "_timing" in job:
                job["timing"] = job["_timing"].snapshot()
            public = {key: value for key, value in job.items()
                      if key not in ("session_id", "invalidated", "protected_paths")
                      and not key.startswith("_")}
            if job["invalidated"]:
                public.update(status="stale", phase="文件已更换，旧结果已作废")
                public.pop("result", None)
            return jsonify(public)

    @app.post("/api/export/pdf")
    def export_pdf():
        from .pdf_export import PdfExportError, render_comparison_pdf

        if len(request.get_data()) > 8 * 1024 * 1024:
            raise WebError("导出详情超过8MB，请缩小筛选范围；没有截断内容。", 413)
        body = request.get_json()
        if (not isinstance(body, dict) or type(body.get("revision")) is not int
                or not isinstance(body.get("job_id"), str)):
            raise WebError("导出请求缺少有效的作业与文件版本。")
        session = g.review_session
        with store.lock, PDF_LOCK:
            job = store.jobs.get(body["job_id"])
            if not job or job["session_id"] != session.id:
                raise WebError("作业不存在或不属于当前会话。", 404)
            if (session.uploading or session.job_id != body["job_id"] or job["invalidated"]
                    or job["status"] != "succeeded" or not job.get("result")
                    or body["revision"] != session.revision or job["revision"] != session.revision):
                raise WebError("文件或对比结果已变化，请完成当前版本对比后再导出。", 409)
            result = job["result"]
            documents = {}
            for side in ("old", "new"):
                document = session.documents.get(side)
                previous = result.get("documents", {}).get(side, {})
                if (document is None or previous.get("id") != document.id
                        or previous.get("sha256") != document.sha256):
                    raise WebError("结果与当前图纸不一致，拒绝导出旧证据。", 409)
                documents[side] = {**document.public(), "path": document.analysis_path}
            try:
                data = render_comparison_pdf(body, documents, result)
            except PdfExportError as error:
                raise WebError(str(error)) from error
        return send_file(BytesIO(data), mimetype="application/pdf", as_attachment=True,
                         download_name="engineering-comparison.pdf")

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("local") / "web-data")
    parser.add_argument("--cache-dir", type=Path, default=Path("output") / "cache")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-azure-upload", action="store_true",
                        help="Allow billed CU calls for cache misses in the approved configuration")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be 1024..65535")
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    app = create_app(config, args.data_dir, args.cache_dir, port=args.port,
                     allow_azure=args.allow_azure_upload)
    atexit.register(app.extensions["review_store"].close)
    from waitress import serve
    print(f"Local engineering review: http://127.0.0.1:{args.port}", flush=True)
    print(f"Session files: {args.data_dir.resolve()}", flush=True)
    print(f"CU cache: {args.cache_dir.resolve()}", flush=True)
    serve(app, host="127.0.0.1", port=args.port, threads=4,
          max_request_body_size=MAX_BYTES, channel_timeout=60)


if __name__ == "__main__":
    main()
