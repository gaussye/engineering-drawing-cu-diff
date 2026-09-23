"""Pinned-deployment JSON completions with durable, fail-closed billing guards.

An ambiguous POST or failed local cache write leaves <key>.pending.json in
cache/model-comparison. Review that exact request with the service before
explicitly removing that marker to authorize another potentially billable POST.
Never clear markers automatically. Cached HTTP responses, including refusals and
invalid completions, are retained and revalidated without resubmission.
"""

import json
import math
import os
import time
from pathlib import Path

from .client import CUError, CacheMiss, Client, canonical, digest, save_json


PROTOCOL_VERSION = "azure-openai-v1-json-comparison-1"


def _reject_constant(value):
    raise ValueError("Non-JSON constant")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _load_json(text):
    return json.loads(text, parse_constant=_reject_constant,
                      object_pairs_hook=_unique_object)


def _response_content(raw) -> tuple[dict, dict]:
    if not isinstance(raw, dict) or "error" in raw:
        raise CUError("Model completion returned an invalid or error response")
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise CUError("Model completion must contain exactly one choice")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise CUError("Model completion did not finish with stop")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise CUError("Model completion has no valid message")
    if message.get("refusal") is not None:
        raise CUError("Model completion was refused")
    if message.get("tool_calls") or message.get("function_call"):
        raise CUError("Model completion returned tool calls")
    content = message.get("content")
    if not isinstance(content, str):
        raise CUError("Model completion content is not JSON text")
    try:
        parsed = _load_json(content)
    except (ValueError, TypeError, RecursionError):
        raise CUError("Model completion content is malformed JSON") from None
    if not isinstance(parsed, dict):
        raise CUError("Model completion JSON must be an object")
    if not isinstance(raw.get("model"), str) or not raw["model"]:
        raise CUError("Model completion has no actual model provenance")
    return parsed, {
        "response_model": raw["model"], "finish_reason": choice["finish_reason"],
        "usage": raw.get("usage"),
    }


def _read_response(path: Path, provenance: dict, observer=None) -> tuple[dict, dict]:
    try:
        saved = _load_json(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or saved.get("state") != "response_received":
            raise ValueError()
        metadata = saved["metadata"]
        if not isinstance(metadata, dict):
            raise ValueError()
        if set(metadata) != set(provenance) | {"elapsed_seconds"}:
            raise ValueError()
        if canonical({key: metadata[key] for key in provenance}) != canonical(provenance):
            raise ValueError()
        elapsed = metadata["elapsed_seconds"]
        if (type(elapsed) not in (int, float) or not math.isfinite(elapsed)
                or elapsed < 0):
            raise ValueError()
        raw = saved["response"]
        if saved["response_sha256"] != digest(canonical(raw)):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise CUError("Corrupt model comparison cache; review it before retrying") from None
    if observer:
        observer(raw)
    parsed, response_metadata = _response_content(raw)
    return parsed, dict(metadata, **response_metadata)


def _sanitize_events(client: Client, first: int) -> None:
    # Client.request currently includes the service error body on HTTP failures.
    # Keep only transport metadata, never model prompts, output or error bodies.
    allowed = {"method", "path", "status", "request_id", "service_request_id",
               "latency_seconds"}
    for index in range(first, len(client.events)):
        event = client.events[index]
        client.events[index] = {
            key: value for key, value in event.items() if key in allowed
        } if isinstance(event, dict) else {}


def complete_json(client: Client, cache: Path, body: dict, *,
                  allow_submit: bool = True) -> tuple[dict, dict]:
    """Return a JSON object and actual provenance, never a success fallback.

    Output schema validation belongs to the caller. Only strict-json-schema
    request metadata and the completion envelope are validated here. Cache-only
    calls perform no requests. No POST is retried, including explicit HTTP errors.
    """
    from .usage import begin_usage, finish_usage

    if not isinstance(body, dict):
        raise CUError("Model completion request must be an object")
    model = body.get("model")
    deployments = client.config.get("model_deployments")
    versions = client.config.get("deployment_versions")
    comparison = client.config.get("model_comparison", {})
    if not isinstance(comparison, dict):
        raise CUError("Invalid comparison model configuration")
    selected = comparison.get("deployment")
    if selected is not None:
        version = comparison.get("deployment_version")
        if (not isinstance(selected, str) or not selected.strip() or model != selected
                or not isinstance(version, str) or not version.strip()):
            raise CUError("Requested model must match the explicitly versioned comparison deployment")
    elif (not isinstance(model, str) or not model
            or not isinstance(deployments, dict) or model not in deployments.values()
            or not isinstance(versions, dict) or not versions.get(model)):
        raise CUError("Requested model is not an explicitly configured versioned deployment")
    else:
        if comparison.get("deployment_version") is not None:
            raise CUError("Comparison deployment version requires an explicit deployment")
        version = versions[model]
    response_format = body.get("response_format")
    if not isinstance(response_format, dict):
        raise CUError("Model completion requires strict json_schema response_format")
    schema = response_format.get("json_schema")
    if (response_format.get("type") != "json_schema" or not isinstance(schema, dict)
            or schema.get("strict") is not True or not isinstance(schema.get("schema"), dict)
            or not isinstance(schema.get("name"), str) or not schema["name"]):
        raise CUError("Model completion requires strict json_schema response_format")
    try:
        # Snapshot the request so hashing and submission use exactly the same JSON.
        request_body = _load_json(json.dumps(body, ensure_ascii=False, allow_nan=False))
        provenance = {
            "protocol_version": PROTOCOL_VERSION,
            "endpoint": client.endpoint,
            "model": model, "deployment": model,
            "deployment_version": version,
            "request_sha256": digest(canonical(request_body)),
        }
        provenance = _load_json(json.dumps(provenance, allow_nan=False))
        key = digest(canonical(provenance))
    except (ValueError, TypeError, RecursionError):
        raise CUError("Model completion request or provenance is not valid JSON") from None
    directory = cache / "model-comparison"
    response_path = directory / f"{key}.response.json"
    pending_path = directory / f"{key}.pending.json"
    if response_path.exists():
        usage_entry = begin_usage(client, "model", key, "cached", provenance)
        parsed, metadata = _read_response(response_path, provenance,
                                         lambda raw: finish_usage(client, usage_entry, raw))
        return parsed, dict(metadata, cache_hit=True, cache_key=key)
    if pending_path.exists():
        usage_entry = begin_usage(client, "model", key, "resumed", provenance)
        finish_usage(client, usage_entry, outcome="previous_pending")
        raise CUError(
            f"Model request pending or outcome unknown; review and explicitly clear "
            f"only {pending_path.name} before resubmitting"
        )
    if not allow_submit:
        raise CacheMiss("No matching model comparison cache; cache-only mode cannot submit")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Exclusive creation, unlike replace, arbitrates concurrent identical POSTs.
        with pending_path.open("x", encoding="utf-8") as marker:
            json.dump({"state": "pending", "cache_key": key,
                       "provenance": provenance}, marker, ensure_ascii=False)
            marker.flush()
            os.fsync(marker.fileno())
    except FileExistsError:
        usage_entry = begin_usage(client, "model", key, "resumed", provenance)
        finish_usage(client, usage_entry, outcome="previous_pending")
        raise CUError(
            f"Model request pending; review {pending_path.name} before resubmitting"
        ) from None
    except (OSError, ValueError, TypeError):
        raise CUError("Cannot persist model request marker; no request submitted") from None
    # Another caller may have finished between our first read and marker creation.
    if response_path.exists():
        usage_entry = begin_usage(client, "model", key, "cached", provenance)
        parsed, metadata = _read_response(response_path, provenance,
                                         lambda raw: finish_usage(client, usage_entry, raw))
        try:
            pending_path.unlink()
        except OSError:
            pass
        return parsed, dict(metadata, cache_hit=True, cache_key=key)
    first_event = len(client.events)
    start = time.monotonic()
    usage_entry = begin_usage(client, "model", key, "new", provenance)
    try:
        raw, _ = client.request(
            "POST", client.endpoint + "/openai/v1/chat/completions", request_body
        )
    except Exception as error:
        finish_usage(client, usage_entry, outcome="request_failed_or_unknown", cache_state="unknown")
        status = getattr(error, "status", None)
        raise CUError(
            f"Model request failed; outcome may be unknown. Review and explicitly clear "
            f"only {pending_path.name} before resubmitting",
            status if type(status) is int else None,
        ) from None
    finally:
        _sanitize_events(client, first_event)
    metadata = dict(provenance, elapsed_seconds=round(time.monotonic() - start, 6))
    finish_usage(client, usage_entry, raw)
    try:
        save_json(response_path, {
            "state": "response_received", "metadata": metadata,
            "response": raw, "response_sha256": digest(canonical(raw)),
        })
    except (OSError, ValueError, TypeError, RecursionError):
        raise CUError(
            f"Model response could not be cached; do not resubmit. Review "
            f"{pending_path.name}"
        ) from None
    try:
        pending_path.unlink()
    except OSError:
        # The durable response takes precedence over a leftover marker.
        pass
    parsed, response_metadata = _response_content(raw)
    return parsed, dict(metadata, **response_metadata, cache_hit=False, cache_key=key)
