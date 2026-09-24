"""Per-run consumption journal and explicit, provenance-bearing price estimates."""

from copy import deepcopy
from collections import deque
from decimal import Decimal
from importlib.resources import files
from inspect import getattr_static
import json
import math
import threading
from urllib.parse import urlsplit


VERSION = "usage-cost-v1"
FIELDS = ("cu_pages", "contextualization_tokens", "input_tokens",
          "cached_input_tokens", "cache_write_tokens", "output_tokens", "model_tokens")
STAGES = {
    "cu_full_old": "全页CU · 旧版", "cu_full_new": "全页CU · 新版",
    "cu_crop_old": "局部CU · 旧版", "cu_crop_new": "局部CU · 新版",
    "model_coarse": "模型 · 全页配对", "model_fine": "模型 · 词级核对",
    "model_visual": "模型 · 图形复核",
    "model_visual_presence": "模型 · 单侧对象与对侧搜索",
    "model_text_pairing": "模型 · 字段与OCR语义配对",
}
CU_METERS = {
    "documentPagesMinimal": ("文档数字提取", "pages"),
    "documentPagesBasic": ("文档OCR提取", "pages"),
    "documentPagesStandard": ("文档布局提取", "pages"),
    "contextualizationTokens": ("CU标准上下文处理", "tokens"),
    "advancedContextualizationTokens": ("CU高级上下文处理", "tokens"),
}
LATENCY_CHECKPOINT_FIELDS = frozenset({
    "engine_tbt_ms", "engine_ttft_ms", "engine_ttlt_ms", "pre_inference_ms",
    "service_tbt_ms", "service_ttft_ms", "service_ttlt_ms", "user_visible_ttft_ms",
})
COST_GROUPS = {
    "cu_analysis": ("cu_extraction", "cu_contextualization"),
    "cu_model": ("cu_model",),
    "analysis_model": ("direct_model",),
}
LAYOUT_COST_EXPLANATION = "已确认layout纯提取配置及完整页数用量；未使用CU上下文处理或CU内部生成模型。"
_JOURNAL_INIT_LOCK = threading.RLock()


def _journal_lock(client):
    # Duck-typed serial clients also use this journal; initialize their lock once.
    with _JOURNAL_INIT_LOCK:
        if getattr_static(client, "_usage_lock", None) is None:
            client._usage_lock = threading.RLock()
        if getattr_static(client, "_usage_notifications", None) is None:
            client._usage_notifications = deque()
            client._usage_notifying = False
        return client._usage_lock


def _queue_notification(client):
    """Called under the journal lock; one caller drains detached snapshots."""
    observer = (getattr(client, "usage_observer")
                if getattr_static(client, "usage_observer", None) is not None else None)
    if not callable(observer):
        return False
    client._usage_notifications.append((observer, deepcopy(client.usage_records)))
    if client._usage_notifying:
        return False
    client._usage_notifying = True
    return True


def _drain_notifications(client, lock):
    try:
        while True:
            with lock:
                if not client._usage_notifications:
                    client._usage_notifying = False
                    return
                observer, snapshot = client._usage_notifications.popleft()
            # Never acquire the Store callback lock while holding a journal lock.
            observer(snapshot)
    except BaseException:
        with lock:
            client._usage_notifying = False
        raise


def _notify(client):
    lock = _journal_lock(client)
    with lock:
        drain = _queue_notification(client)
    if drain:
        _drain_notifications(client, lock)


def begin_usage(client, service, key, cache_state, metadata):
    lock = _journal_lock(client)
    with lock:
        if not isinstance(getattr(client, "usage_records", None), list):
            client.usage_records = []
        context = getattr(client, "usage_context", {})
        entry = {
            "id": f"U{len(client.usage_records)+1:03d}", "service": service, "cache_key": key,
            "cache_state": cache_state, "outcome": "pending", "usage": None,
            "stage": context.get("stage", service), "region_index": context.get("region_index"),
            "metadata": deepcopy(metadata),
        }
        client.usage_records.append(entry)
        drain = _queue_notification(client)
    if drain:
        _drain_notifications(client, lock)
    return entry


def finish_usage(client, entry, raw=None, *, outcome="response_received", cache_state=None):
    lock = _journal_lock(client)
    with lock:
        entry["outcome"] = outcome
        if cache_state:
            entry["cache_state"] = cache_state
        if isinstance(raw, dict):
            latest = raw.get("usage")
            if isinstance(latest, dict) and latest:
                entry["usage"] = deepcopy(latest)
                entry["usage_incomplete"] = False
            elif entry.get("usage") is not None:
                entry["usage_incomplete"] = True
            else:
                entry["usage"] = deepcopy(latest)
            if isinstance(raw.get("model"), str):
                entry["metadata"]["response_model"] = raw["model"]
        drain = _queue_notification(client)
    if drain:
        _drain_notifications(client, lock)


def _number(value):
    return type(value) in (int, float) and 0 <= value <= 2**53-1 and math.isfinite(value)


def _json_usage(value):
    if isinstance(value, dict):
        return {key: _json_usage(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_usage(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if type(value) is int and abs(value) > 2**53-1:
        return str(value)
    return value


def validate_pricing(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("pricing must be an object")
    currency = value.get("currency", "USD")
    region = value.get("region")
    semantics = value.get("cu_input_includes_cached")
    rates = value.get("rates", [])
    if (not isinstance(currency, str) or len(currency) != 3
            or not currency.isascii() or not currency.isalpha() or not currency.isupper()
            or region is not None and (not isinstance(region, str) or not region.strip())
            or semantics is not None and type(semantics) is not bool or not isinstance(rates, list)):
        raise ValueError("Invalid pricing currency, region, rates or CU input-token semantics")
    snapshot = value.get("snapshot")
    if snapshot is not None:
        if snapshot != "azure-retail-westus-2026-09-23":
            raise ValueError("Unknown bundled pricing snapshot")
        bundled = json.loads(files("cu_diff").joinpath("pricing", snapshot+".json").read_text(encoding="utf-8"))
        if bundled["currency"] != currency:
            raise ValueError("Pricing snapshot currency differs from configured currency")
        rates = [{**bundled["defaults"], "source": bundled["sources"][r["source_group"]], **r}
                 for r in bundled["rates"]] + rates
    checked = []
    for rate in rates:
        if not isinstance(rate, dict):
            raise ValueError("Each pricing rate must be an object")
        if (not isinstance(rate.get("key"), str) or not rate["key"]
                or not _number(rate.get("price")) or not _number(rate.get("unit_quantity"))
                or rate["unit_quantity"] <= 0 or rate.get("currency") != currency
                or rate.get("unit") not in ("pages", "tokens")
                or not isinstance(rate.get("as_of"), str) or not rate["as_of"]):
            raise ValueError("Each rate needs a key, unit price/basis, currency, unit and as_of date")
        source_kind = rate.get("source_kind", "reference")
        if source_kind not in ("reference", "user_provided"):
            raise ValueError("Invalid pricing source_kind")
        if not isinstance(rate.get("source"), str) or not rate["source"].strip():
            raise ValueError("Pricing source must be a nonempty string")
        if source_kind != "user_provided":
            source = urlsplit(rate["source"])
            if source.scheme != "https" or not source.hostname or source.username or source.password:
                raise ValueError("Pricing sources must be HTTPS URLs without credentials")
        for field in ("region", "sku", "model_version"):
            if rate.get(field) is not None and not isinstance(rate[field], str):
                raise ValueError(f"Invalid rate {field}")
        if rate.get("context_tier") not in (None, "short", "long"):
            raise ValueError("Invalid rate context_tier")
        for field in ("min_input_tokens", "max_input_tokens"):
            if rate.get(field) is not None and not _number(rate[field]):
                raise ValueError(f"Invalid rate {field}")
        if (rate.get("min_input_tokens") is not None and rate.get("max_input_tokens") is not None
                and rate["min_input_tokens"] > rate["max_input_tokens"]):
            raise ValueError("Invalid rate context interval")
        conditions = ("key", "region", "sku", "model_version", "min_input_tokens", "max_input_tokens", "context_tier")
        checked = [old for old in checked if any(old.get(k) != rate.get(k) for k in conditions)]
        checked.append(dict(rate, source_kind=source_kind))
    return {"currency": currency, "region": region, "rates": checked,
            "cu_input_includes_cached": semantics}


def _deployment(meta, model=None):
    if model is None:
        deployment = meta.get("deployment")
        version = meta.get("deployment_version", "")
    else:
        deployment = meta.get("model_deployments", {}).get(model)
        version = meta.get("deployment_versions", {}).get(deployment, "")
    parts = version.rsplit(":", 2) if isinstance(version, str) else []
    if len(parts) != 3:
        return model or meta.get("model") or deployment, None, None, deployment
    return model or parts[0], parts[1], parts[2], deployment


def _rates(pricing, key, unit, *, version=None, sku=None, prompt=None, service=None):
    matches = []
    for rate in pricing["rates"]:
        if rate["key"] != key or rate["unit"] != unit:
            continue
        if rate.get("region") is not None and rate["region"] != pricing["region"]:
            continue
        if rate.get("sku") is not None and rate["sku"] != sku:
            continue
        if rate.get("model_version") is not None and rate["model_version"] != version:
            continue
        low, high = rate.get("min_input_tokens"), rate.get("max_input_tokens")
        if (low is not None and (prompt is None or prompt < low)
                or high is not None and (prompt is None or prompt > high)):
            continue
        # CU reports aggregate model tokens, not the length of each internal prompt.
        if service == "cu" and low is not None and low > 0:
            continue
        matches.append(rate)
    return matches


def _model_free_layout(entry):
    meta, usage = entry["metadata"], entry.get("usage")
    page_keys = {key for key in CU_METERS if key.startswith("documentPages")}
    return (
        entry["service"] == "cu" and meta.get("extraction_profile") == "layout"
        and not meta.get("model_deployments") and not meta.get("deployment_versions")
        and meta.get("selected_completion_model") is None
        and entry.get("outcome") == "response_received" and not entry.get("usage_incomplete")
        and isinstance(usage, dict) and bool(page_keys & usage.keys())
        and usage.keys() <= page_keys | {"tokens"}
        and ("tokens" not in usage or usage["tokens"] == {})
        and all(_number(value) for key, value in usage.items() if key in page_keys)
    )


def _normalize(entry, pricing):
    metrics = {k: 0 for k in FIELDS}
    meters, warnings = [], []
    usage, service, meta = entry.get("usage"), entry["service"], entry["metadata"]

    def meter(key, label, quantity, unit, category, *, quantity_range=None, eligible=True, **dimensions):
        quantity = quantity if _number(quantity) else None
        candidates = _rates(pricing, key, unit, service=service, **dimensions) if eligible else []
        rate = candidates[0] if len(candidates) == 1 else None
        cost = None
        if quantity == 0:
            cost = 0.
        elif quantity is not None and rate:
            cost = float(Decimal(str(quantity))*Decimal(str(rate["price"]))/Decimal(str(rate["unit_quantity"])))
        bounds = None
        quantities = [quantity, quantity] if quantity is not None else quantity_range
        if quantity == 0:
            bounds = {"min": 0., "max": 0.}
        elif quantities and candidates:
            amounts = [float(Decimal(str(q))*Decimal(str(r["price"]))/Decimal(str(r["unit_quantity"])))
                       for q in quantities for r in candidates]
            bounds = {"min": min(amounts), "max": max(amounts)}
        reason = ("uncertain_usage_semantics" if quantity_range else "missing_usage" if quantity is None else
                  "ambiguous_rate" if len(candidates) > 1 and quantity else
                  "missing_rate" if rate is None and quantity else None)
        meters.append({"key": key, "label": label, "quantity": quantity, "unit": unit, "category": category,
                       "quantity_range": quantities if quantity is None else None,
                       "rate": rate, "rate_candidates": candidates if len(candidates) > 1 else [],
                       "estimated_cost": cost, "estimated_cost_range": bounds, "reason": reason})

    if not isinstance(usage, dict) or not usage:
        for key in FIELDS:
            metrics[key] = None if service == "cu" or key not in ("cu_pages", "contextualization_tokens") else 0
        meter(service+".unknown", "服务未返回可用用量", None, "unknown",
              "cu_extraction" if service == "cu" else "direct_model")
        if service == "cu":
            meter("cu.model.unknown", "CU内部模型用量未确认", None, "tokens", "cu_model")
        notes = ["服务未返回可用用量；不能按零消耗或零费用计算。"]
        if entry.get("outcome") == "previous_pending":
            notes.append("历史模型请求仍待确认，本次已阻止重复提交；没有续发模型请求。")
        return metrics, meters, notes, "missing"
    if service == "cu":
        model_free = _model_free_layout(entry)
        pages = [k for k in CU_METERS if k.startswith("documentPages") and k in usage]
        contexts = [k for k in CU_METERS if k.endswith("ContextualizationTokens") or k == "contextualizationTokens"]
        metrics["cu_pages"] = sum(usage[k] for k in pages) if pages and all(_number(usage[k]) for k in pages) else None
        present_contexts = [k for k in contexts if k in usage]
        metrics["contextualization_tokens"] = (sum(usage[k] for k in present_contexts)
            if present_contexts and all(_number(usage[k]) for k in present_contexts)
            else 0 if model_free else None)
        for key, (label, unit) in CU_METERS.items():
            if key in usage:
                meter("cu."+key, label, usage[key], unit,
                      "cu_extraction" if unit == "pages" else "cu_contextualization")
        if not pages:
            meter("cu.pages.unknown", "CU页数未提供", None, "pages", "cu_extraction")
        if not present_contexts and not model_free:
            meter("cu.context.unknown", "CU上下文处理用量未提供", None, "tokens", "cu_contextualization")
        tokens = usage.get("tokens")
        grouped = {}
        if isinstance(tokens, dict):
            for key, value in tokens.items():
                suffix = next((s for s in ("-cached-input", "-input", "-output") if key.endswith(s)), None)
                if suffix:
                    grouped.setdefault(key[:-len(suffix)], {})[suffix[1:]] = value
                else:
                    meter("cu.token."+key, "未识别的CU模型token："+key, value, "tokens", "cu_model")
                    warnings.append("CU返回未识别的模型token计量项，未推测其价格。")
        if not grouped and not model_free:
            metrics.update(input_tokens=None, cached_input_tokens=None, output_tokens=None, model_tokens=None)
            meter("cu.model.unknown", "CU内部模型token未提供", None, "tokens", "cu_model")
        for model, counts in grouped.items():
            raw_input, cached, output = counts.get("input"), counts.get("cached-input", 0), counts.get("output", 0)
            _, version, sku, _ = _deployment(meta, model)
            ordinary, total_input = None, None
            ordinary_range = None
            if _number(raw_input) and _number(cached):
                if cached == 0 or pricing["cu_input_includes_cached"] is False:
                    ordinary, total_input = raw_input, raw_input+cached
                elif pricing["cu_input_includes_cached"] is True and cached <= raw_input:
                    ordinary, total_input = raw_input-cached, raw_input
                else:
                    warnings.append("CU输入token是否包含缓存输入尚未核实，未重复计费或擅自扣减。")
                    if cached <= raw_input and pricing["cu_input_includes_cached"] is None:
                        ordinary_range = [raw_input-cached, raw_input]
            for kind, count in (("input", ordinary), ("cached_input", cached), ("output", output)):
                meter(f"model.{model}.{kind}", f"CU内部 {model} · {kind}", count, "tokens", "cu_model",
                      quantity_range=ordinary_range if kind == "input" else None,
                      version=version, sku=sku, prompt=(total_input if total_input is not None else
                          raw_input+cached if _number(raw_input) and _number(cached) else None))
            for key, value in (("input_tokens", total_input), ("cached_input_tokens", cached), ("output_tokens", output)):
                metrics[key] = metrics[key]+value if metrics[key] is not None and _number(value) else None
        if grouped:
            metrics["model_tokens"] = (metrics["input_tokens"]+metrics["output_tokens"]
                                      if metrics["input_tokens"] is not None and metrics["output_tokens"] is not None else None)
        for key in set(usage)-set(CU_METERS)-{"tokens"}:
            meter("cu."+key, "未识别的CU计量项："+key, usage[key], "unknown", "cu_extraction")
            warnings.append("CU返回额外计量项，未将其当作零费用。")
    else:
        prompt, output = usage.get("prompt_tokens"), usage.get("completion_tokens")
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0 if details is None else None
        written = details.get("cache_write_tokens", 0) if isinstance(details, dict) else 0 if details is None else None
        if not _number(prompt) or not _number(cached) or cached > prompt:
            prompt = cached = None
        if not _number(output):
            output = None
        model, version, sku, _ = _deployment(meta)
        actual = meta.get("response_model")
        identity_matches = not (actual and model and actual not in (model, f"{model}-{version}"))
        if not identity_matches:
            warnings.append("实际返回模型与配置版本不一致，未套用该部署的版本单价。")
        metrics.update(input_tokens=prompt, cached_input_tokens=cached, output_tokens=output,
                       cache_write_tokens=written if _number(written) else None,
                       model_tokens=prompt+output if prompt is not None and output is not None else None)
        ordinary = prompt-cached if prompt is not None else None
        if not _number(written) or written:
            ordinary = None
            warnings.append("服务返回缓存写入token；其与普通输入的计费重叠口径尚未核实，未重复相加。")
            meter(f"model.{model}.cache_write", f"直接调用 {model} · cache_write", written, "tokens", "direct_model",
                  version=version, sku=sku, prompt=prompt, eligible=identity_matches)
        for kind, count in (("input", ordinary),
                            ("cached_input", cached), ("output", output)):
            meter(f"model.{model}.{kind}", f"直接调用 {model} · {kind}", count, "tokens", "direct_model",
                  version=version, sku=sku, prompt=prompt, eligible=identity_matches)
        if _number(usage.get("total_tokens")) and metrics["model_tokens"] != usage["total_tokens"]:
            warnings.append("服务总token与输入/输出分项不一致，以分项展示并标记用量不完整。")
            meter("model.total_mismatch", "服务token合计不一致", None, "tokens", "direct_model")
        recognized = {
            "": {"prompt_tokens", "completion_tokens", "total_tokens",
                 "prompt_tokens_details", "completion_tokens_details"},
            "prompt_tokens_details": {"cached_tokens", "cache_write_tokens", "audio_tokens"},
            "completion_tokens_details": {"reasoning_tokens", "audio_tokens",
                                          "accepted_prediction_tokens", "rejected_prediction_tokens"},
        }
        latency = usage.get("latency_checkpoint")
        # Only the observed, closed millisecond schema is known to be non-billing.
        if (isinstance(latency, dict) and latency.keys() == LATENCY_CHECKPOINT_FIELDS
                and all(_number(value) for value in latency.values())):
            recognized[""].add("latency_checkpoint")
        for group, names in recognized.items():
            counts = usage.get(group) if group else usage
            if isinstance(counts, dict):
                for key in counts.keys() - names:
                    path = f"{group}.{key}" if group else key
                    meter("model.unknown."+path, "未识别的模型计量项："+path,
                          None, "unknown", "direct_model")
                    warnings.append("模型返回额外计量项，未将其当作零费用。")
    if any(m["quantity"] is None for m in meters):
        warnings.append("部分计量项缺失、无效或口径未核实；合计不是完整用量。")
    if entry.get("outcome") == "operation_incomplete":
        meter(service+".incomplete", "操作未完成；已返回用量可能不是最终值", None, "unknown",
              "cu_extraction" if service == "cu" else "direct_model")
        if service == "cu":
            meter("cu.model.incomplete", "CU操作未完成；内部模型用量仍待确认", None, "unknown", "cu_model")
        warnings.append("保留已返回的用量，但操作失败/超时，最终费用仍待核对。")
    if entry.get("usage_incomplete"):
        meter("usage.final_missing", "保留上次返回计数；最终用量未提供", None, "unknown",
              "cu_extraction" if service == "cu" else "direct_model")
        if service == "cu":
            meter("cu.model.final_missing", "最终CU内部模型用量未提供", None, "unknown", "cu_model")
        warnings.append("后续响应缺少用量；保留此前计数但不视为完整最终用量。")
    status = "invalid" if any(m["quantity"] is None for m in meters) else "reported"
    return metrics, meters, warnings, status


def _cost_summary(meters):
    known = sum(m["estimated_cost"] for m in meters if m["estimated_cost"] is not None)
    estimated = known if all(m["estimated_cost"] is not None for m in meters) else None
    bounds = ({key: sum(m["estimated_cost_range"][key] for m in meters) for key in ("min", "max")}
              if all(m["estimated_cost_range"] is not None for m in meters) else None)
    return {
        "estimated_cost": estimated, "estimated_cost_range": bounds, "known_cost": known,
        "unpriced_meters": sum(m["reason"] in ("missing_rate", "ambiguous_rate") for m in meters),
        "unknown_usage_calls": int(any(m["quantity"] is None for m in meters)),
        "status": "not_used" if not meters else "complete" if estimated is not None else "partial",
    }


def _current_breakdown(entry, meters, current):
    result = {
        group: _cost_summary([m for m in meters if m["category"] in categories] if current else [])
        for group, categories in COST_GROUPS.items()
    }
    if current and _model_free_layout(entry):
        result["cu_model"].update(status="not_applicable", explanation=LAYOUT_COST_EXPLANATION)
        result["cu_analysis"]["explanation"] = LAYOUT_COST_EXPLANATION
    return result


def _merge_cost_summary(target, source):
    for key in ("known_cost", "unpriced_meters", "unknown_usage_calls"):
        target[key] += source[key]
    target["estimated_cost"] = (target["estimated_cost"] + source["estimated_cost"]
                               if target["estimated_cost"] is not None and source["estimated_cost"] is not None else None)
    target["estimated_cost_range"] = (
        {key: target["estimated_cost_range"][key] + source["estimated_cost_range"][key] for key in ("min", "max")}
        if target["estimated_cost_range"] is not None and source["estimated_cost_range"] is not None else None)
    priorities = {"not_used": 0, "not_applicable": 1, "complete": 2, "partial": 3}
    if priorities[source["status"]] > priorities[target["status"]]:
        target["status"] = source["status"]
    if target["status"] == "not_applicable":
        target["explanation"] = LAYOUT_COST_EXPLANATION
    else:
        target.pop("explanation", None)


def _total_costs(components):
    return {
        "known_cost": sum(c["known_cost"] for c in components),
        "estimated_cost": (sum(c["estimated_cost"] for c in components)
                           if all(c["estimated_cost"] is not None for c in components) else None),
        "estimated_cost_range": (
            {key: sum(c["estimated_cost_range"][key] for c in components) for key in ("min", "max")}
            if all(c["estimated_cost_range"] is not None for c in components) else None),
    }


def usage_report(records, pricing=None):
    pricing = validate_pricing(pricing)
    journal_available = isinstance(records, list)
    entries, warnings, dates, sources = [], [], set(), []
    buckets = {name: {**{k: 0 for k in FIELDS}, "known_cost": 0., "estimated_cost": 0.,
                      "estimated_cost_range": {"min": 0., "max": 0.},
                      "unpriced_meters": 0, "unknown_usage_calls": 0} for name in ("current", "reused")}
    requests = {"new": 0, "cached": 0, "resumed": 0, "unknown": 0}
    current_breakdown = {group: _cost_summary([]) for group in COST_GROUPS}
    records = records if journal_available else []
    new_keys = {(e["service"], e["cache_key"]) for e in records
                if e["cache_state"] not in ("cached", "resumed", "not_submitted")}
    reused_keys = set()
    for entry in records:
        state = entry["cache_state"]
        if state == "not_submitted":
            continue
        if state in requests:
            requests[state] += 1
        else:
            requests["unknown"] += 1
        metrics, meters, notes, status = _normalize(entry, pricing)
        known = sum(m["estimated_cost"] for m in meters if m["estimated_cost"] is not None)
        complete = all(m["estimated_cost"] is not None for m in meters)
        estimated = known if complete else None
        bounds = ({"min": sum(m["estimated_cost_range"]["min"] for m in meters),
                   "max": sum(m["estimated_cost_range"]["max"] for m in meters)}
                  if all(m["estimated_cost_range"] is not None for m in meters) else None)
        current = state not in ("cached", "resumed")
        breakdown = _current_breakdown(entry, meters, current)
        current_costs = _total_costs(list(breakdown.values()))
        identity = entry["service"], entry["cache_key"]
        counted = current or identity not in new_keys | reused_keys
        if not current:
            reused_keys.add(identity)
        bucket = buckets["current" if current else "reused"]
        if counted:
            if current:
                for group in COST_GROUPS:
                    _merge_cost_summary(current_breakdown[group], breakdown[group])
            for key in FIELDS:
                bucket[key] = bucket[key]+metrics[key] if bucket[key] is not None and metrics[key] is not None else None
            bucket["known_cost"] += known
            bucket["estimated_cost"] = (bucket["estimated_cost"]+estimated
                                       if bucket["estimated_cost"] is not None and estimated is not None else None)
            bucket["estimated_cost_range"] = (
                {k: bucket["estimated_cost_range"][k]+bounds[k] for k in ("min", "max")}
                if bucket["estimated_cost_range"] is not None and bounds is not None else None)
            bucket["unpriced_meters"] += sum(m["reason"] in ("missing_rate", "ambiguous_rate") for m in meters)
            bucket["unknown_usage_calls"] += status != "reported"
        meta = entry["metadata"]
        model, _, _, deployment = _deployment(meta, meta.get("selected_completion_model") if entry["service"] == "cu" else None)
        entries.append({
            "id": entry["id"], "service": entry["service"], "stage": STAGES.get(entry["stage"], entry["stage"]),
            "region_index": entry.get("region_index"), "cache_state": state, "usage_status": status,
            "outcome": entry.get("outcome"), "model": model, "deployment": deployment,
            "metrics": metrics, "meters": meters, "current_cost": current_costs["estimated_cost"],
            "current_breakdown": breakdown,
            "raw_usage": _json_usage(entry.get("usage")),
            "reference_cost": estimated, "known_cost": known, "counted_in_summary": counted,
            "reference_cost_range": bounds, "current_cost_range": current_costs["estimated_cost_range"],
            "warnings": notes+([] if counted else ["该缓存用量已在本次其他记录中包含，历史合计不重复计算。"]),
        })
        dates.update(m["rate"]["as_of"] for m in meters if m["rate"] is not None)
        dates.update(r["as_of"] for m in meters for r in m["rate_candidates"])
        for m in meters:
            for rate in ([m["rate"]] if m["rate"] is not None else m["rate_candidates"]):
                source = {key: rate[key] for key in ("source_kind", "source", "as_of")}
                if source not in sources:
                    sources.append(source)
    if not journal_available:
        buckets["current"].update({k: None for k in FIELDS})
        buckets["current"].update(estimated_cost=None, estimated_cost_range=None, unknown_usage_calls=1)
        for group in current_breakdown.values():
            group.update(estimated_cost=None, estimated_cost_range=None, unknown_usage_calls=1,
                         status="partial", explanation="本次调用记录缺失，无法确认该项用量。")
        warnings.append("未提供本次调用记录，无法估算完整用量与费用。")
    buckets["current"].update(_total_costs(list(current_breakdown.values())))
    warnings.extend([
        "费用为所配置单价的估算，不是Azure账单；不含税、汇率换算、协议折扣或其他资源费用。",
        "本地缓存命中/续接旧操作不新增提交；历史用量仅供参考，不重复计入本次新增费用。",
        "直接模型缓存输入包含在prompt_tokens中；CU原始input的缓存口径未核实时另行标记。推理token已包含于输出，不另行相加。",
        "CU提取与上下文处理收费和CU内部Foundry模型token收费分开；直接模型请求另列，不能漏算或重复计费。",
        "CU页数包括全页提取和局部裁剪复读，不是上传PDF的物理页数。",
        "缺失用量、未知单价或未核实的计费口径不按零处理；已知金额仅是小计。",
        "费用区间仅枚举已取得单价/已知计量口径的场景，不是Azure账单保证或完整费用上限。",
    ])
    status = ("unavailable" if not journal_available else "complete" if all(
        b["estimated_cost"] is not None for b in buckets.values()) else "partial")
    return {"version": VERSION, "currency": pricing["currency"], "status": status,
            "price_as_of": min(dates) if dates else None,
            "price_sources": sources,
            "summary": {"requests": requests, **buckets, "current_breakdown": current_breakdown},
            "entries": entries, "warnings": warnings}
