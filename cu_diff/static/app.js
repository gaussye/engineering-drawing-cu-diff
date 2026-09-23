(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const sides = ["old", "new"];
  const sideName = { old: "旧版", new: "新版" };
  const changes = {
    modified: "提取原文不同", relocated: "行号重排", interpretation_only: "仅解释差异",
    formatting_only: "仅格式差异（不计变更）",
    unpaired_old: "旧侧未配对（非确认删除）", unpaired_new: "新侧未配对（非确认新增）",
    unchanged: "提取原文相同", reconciled: "OCR 分段已合并（非原文变更）",
    visual_modified: "外观变化候选", visual_annotation: "文字/标注外观残差候选",
    visual_moved: "视图平移（非内容变更）", visual_scaled: "绘图缩放（非实物尺寸）",
    table_row_added: "表格新增行候选", table_row_removed: "表格删除行候选",
    table_cell_modified: "表格单元格文字不同",
    table_column_added: "表格新增列候选", table_column_removed: "表格删除列候选",
    table_grid_changed: "表格网格结构不同",
    annotation_occurrence_changed: "图外标注次数不同（待核）",
    model_no_text_change: "局部复读未发现文字差异",
    model_text_modified: "CU局部原文不同（模型配对待核）",
    model_review: "模型对应待复核（非确认变更）",
    model_visual_modified: "图纸填充／轮廓变化候选",
    model_visual_review: "图纸填充／轮廓变化候选（定位未解决）",
    visual_uncertain: "图形对应不确定（待复核）"
  };
  const channels = { model: "模型引导复核", schema: "结构化字段", tables: "表格专项证据", graphics: "本地图形候选", ocr: "OCR 原文", unchanged: "一致项复核" };
  const state = {
    ready: false, csrf: "", revision: 0, azure: false, graphicsEnabled: false, modelEnabled: false, generation: 0,
    limits: { max_bytes: 20971520, max_pages: 20 }, result: null, selected: null,
    comparing: false, jobController: null, jobId: null, exporting: false, exportController: null, mutationQueue: Promise.resolve(),
    old: { document: null, page: 1, zoom: "fit", epoch: 0, pending: false, loaded: false, imageEpoch: 0 },
    new: { document: null, page: 1, zoom: "fit", epoch: 0, pending: false, loaded: false, imageEpoch: 0 }
  };
  const text = (value) => value == null ? "" : typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const el = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.textContent = text(content);
    return node;
  };
  function selectDetailTab(name, focus = false) {
    for (const key of ["evidence", "usage"]) {
      const selected = key === name, tab = $(`${key}-tab`);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $(`${key}-panel`).hidden = !selected;
      if (selected && focus) tab.focus();
    }
    $("detail-meta").hidden = name !== "evidence";
  }
  for (const [index, name] of ["evidence", "usage"].entries()) {
    $(`${name}-tab`).addEventListener("click", () => selectDetailTab(name));
    $(`${name}-tab`).addEventListener("keydown", (event) => {
      const targets = { ArrowLeft: 1 - index, ArrowRight: 1 - index, Home: 0, End: 1 };
      if (!(event.key in targets)) return;
      event.preventDefault();
      selectDetailTab(["evidence", "usage"][targets[event.key]], true);
    });
  }
  const usageNumber = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const usageCount = (value) => usageNumber(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 8 }) : "未提供";
  const usageMoney = (value) => !usageNumber(value) ? "未知" : value > 0 && value < 1e-10
    ? "< US$0.0000000001"
    : `US$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 10 })}`;
  const usageRange = (min, max) => usageNumber(min) && usageNumber(max) && min <= max;
  const usageCost = (value, range, missing = "未知") => usageNumber(value) ? usageMoney(value)
    : usageRange(range?.min, range?.max) ? `${usageMoney(range.min)} – ${usageMoney(range.max)}（场景估算）` : missing;
  const usageText = (value) => value == null || value === "" ? "未提供" : text(value);
  const cacheLabels = { new: "本轮新提交", cached: "本地缓存复用", resumed: "恢复历史操作", unknown: "来源未知", not_submitted: "未提交" };
  const usageLabels = { reported: "已报告", missing: "用量缺失", invalid: "用量无效", unknown: "用量未知" };
  const meterLabels = { cu_extraction: "CU 页面提取", cu_contextualization: "CU 上下文 token", cu_model: "CU 内部模型 token", direct_model: "直接对比模型 token" };
  const unitLabels = { pages: "页", tokens: "token" };
  const tierLabels = { short: "短上下文（short）", long: "长上下文（long）" };
  const reasonLabels = {
    ambiguous_rate: "短 / 长上下文适用档位未确认（ambiguous_rate）",
    uncertain_usage_semantics: "CU 输入与缓存输入是否重叠尚未确认（uncertain_usage_semantics）"
  };
  const rateAvailable = (rate) => rate && usageNumber(rate.price) && usageNumber(rate.unit_quantity) && rate.unit_quantity > 0;
  const ratePrice = (rate, unit) => rateAvailable(rate)
    ? `${rate.currency === "USD" ? usageMoney(rate.price) : `${usageCount(rate.price)} ${usageText(rate.currency)}`} / ${usageCount(rate.unit_quantity)} ${unit}` : "待配置";
  const cacheWriteNote = (metrics) => metrics && Object.prototype.hasOwnProperty.call(metrics, "cache_write_tokens")
    ? ` · 缓存写入 ${usageCount(metrics.cache_write_tokens)} token（单列，不叠加模型总量）` : "";
  function usageSource(value) {
    const label = usageText(value);
    try {
      if (typeof value !== "string" || !/^https:\/\//i.test(value) || /[\s\\]/.test(value)) throw new Error();
      const url = new URL(value);
      if (url.protocol !== "https:" || !url.hostname || url.username || url.password) throw new Error();
      const link = el("a", "", label);
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.referrerPolicy = "no-referrer";
      return link;
    } catch (_) {
      return el("span", "", `${label}${value ? "（非安全 HTTPS 链接，仅显示文本）" : ""}`);
    }
  }
  function usageMetrics(metrics = {}, modelLabel = "模型") {
    return `CU 页数 ${usageCount(metrics?.cu_pages)} · CU 上下文 token ${usageCount(metrics?.contextualization_tokens)} · ${modelLabel} token ${usageCount(metrics?.model_tokens)}（输入 ${usageCount(metrics?.input_tokens)} / 其中服务端缓存输入 ${usageCount(metrics?.cached_input_tokens)} / 输出 ${usageCount(metrics?.output_tokens)}）${cacheWriteNote(metrics)}`;
  }
  function usageRateDetails(rate, label) {
    const section = el("section", "usage-rate-detail"), sourceLine = el("p", "", "价格来源：");
    sourceLine.append(usageSource(rate?.source));
    section.append(el("h4", "", label), sourceLine,
      el("p", "", `上下文档位：${tierLabels[rate?.context_tier] || usageText(rate?.context_tier)}`),
      el("p", "", `价格日期：${usageText(rate?.as_of)} · 区域：${usageText(rate?.region)}`),
      el("p", "", `模型版本：${usageText(rate?.model_version)} · SKU：${usageText(rate?.sku)} · 币种：${usageText(rate?.currency)}`),
      el("p", "", `计量 ID：${usageText(rate?.meter_id)} · 计量名称：${usageText(rate?.meter_name)}`));
    return section;
  }
  let usageSnapshot;
  function renderUsage(usage) {
    const snapshot = JSON.stringify(usage);
    if (usageSnapshot === snapshot) return;
    usageSnapshot = snapshot;
    const content = $("usage-content");
    const opened = new Set([...content.querySelectorAll("details[open]")].map((node) => node.id));
    const focused = content.contains(document.activeElement) ? document.activeElement.id : null;
    const scrollTop = $("usage-panel").scrollTop;
    content.replaceChildren();
    if (!usage || typeof usage !== "object") {
      content.append(el("p", "usage-empty", "暂无用量数据。开始对比后显示本轮记录；旧结果未提供用量时，不推算为零。"));
      return;
    }
    const summary = usage.summary || {}, current = summary.current || {}, reused = summary.reused || {}, requests = summary.requests || {};
    const heading = el("div", "usage-heading");
    heading.append(el("h3", "", "本轮新增用量 / USD 估算"),
      el("span", "usage-note", `记录状态：${({ complete: "完整", partial: "部分数据 / 估算不完整", unavailable: "不可用" })[usage.status] || "未提供"} · 价格日期：${usageText(usage.price_as_of)}`));
    const cards = el("div", "usage-cards");
    function card(id, title, value, note) {
      const node = el("section", "usage-card");
      node.id = id;
      node.append(el("h4", "", title), el("p", "usage-value", value), el("p", "usage-note", note));
      cards.append(node);
      return node;
    }
    card("usage-current-cost", "本轮新增估算费用",
      usageCost(current.estimated_cost, current.estimated_cost_range, "费用未完整估算"),
      `已知费用小计（不等于完整总额）：${usageNumber(current.known_cost) ? usageMoney(current.known_cost) : "未提供"} · 价格待核计量项 ${usageCount(current.unpriced_meters)} · 用量待核调用 ${usageCount(current.unknown_usage_calls)}`);
    card("usage-current-cu", "CU 页面与上下文",
      `${usageCount(current.cu_pages)} 页`,
      `CU 上下文：${usageCount(current.contextualization_tokens)} token`);
    card("usage-current-model", "模型 token（CU 内部 + 直接对比）",
      usageCount(current.model_tokens),
      `输入 ${usageCount(current.input_tokens)} · 其中服务端缓存输入 ${usageCount(current.cached_input_tokens)} · 输出 ${usageCount(current.output_tokens)}${cacheWriteNote(current)}`);
    card("usage-requests", "调用来源（不是 token 缓存）",
      `新提交 ${usageCount(requests.new)}`,
      `本地缓存 ${usageCount(requests.cached)} · 恢复 ${usageCount(requests.resumed)} · 未知 ${usageCount(requests.unknown)}`);
    content.append(heading, cards,
      el("p", "usage-note", "仅为估算，不是账单。使用公开零售价，不代表合同价；未计税费或汇率换算。未知用量或价格意味着总额不完整，已知小计不是总额。"),
      el("p", "usage-note", "规范化输入 token 包含服务端缓存输入，不重复相加；模型 token = 输入 + 输出，推理 token 已包含在输出内。缓存写入单列，不叠加模型总量。CU 原始输入与缓存输入是否重叠未确认时，规范化总量保持未知，不直接相加原始计数。本地缓存与恢复仅引用历史操作，不计入本轮新增用量或费用。"),
      el("p", "usage-note", "场景估算区间来自后端假设，不是确定费用或账单上下限；短 / 长上下文适用门槛或 CU 用量语义未确认时，不擅自选择档位，不将候选价格或场景端点相加。价格待核包含已核实单价但适用档位未确认的情况；用量待核包含已返回 API 计数但缓存重叠语义未确认的情况。"));
    const history = el("section", "usage-history");
    history.id = "usage-history";
    history.append(el("h4", "", "历史缓存 / 恢复参考 · 非本轮新增计费"),
      el("p", "", `历史参考费用：${usageCost(reused.estimated_cost, reused.estimated_cost_range)} · 已知参考小计（非总额）：${usageNumber(reused.known_cost) ? usageMoney(reused.known_cost) : "未提供"}`),
      el("p", "", "参考费用按当前配置的价格快照重估历史用量，不是原始日期的账单或当时实际支付费用。"),
      el("p", "", usageMetrics(reused)),
      el("p", "", `历史价格待核计量项 ${usageCount(reused.unpriced_meters)} · 历史用量待核调用 ${usageCount(reused.unknown_usage_calls)}`));
    content.append(history);
    if (Array.isArray(usage.warnings)) usage.warnings.forEach((warning) => content.append(el("p", "usage-warning", warning)));
    const entries = Array.isArray(usage.entries) ? usage.entries : [];
    if (!entries.length) content.append(el("p", "usage-note", "逐阶段调用明细：未提供。"));
    entries.forEach((entry, index) => {
      if (!entry || typeof entry !== "object") return;
      const call = el("details", "usage-call");
      call.id = `usage-call-${index}`;
      const service = entry.service === "cu" ? "CU 分析" : entry.service === "model" ? "直接对比模型" : usageText(entry.service);
      const metricScope = entry.cache_state === "new" ? "本轮新提交用量"
        : ["cached", "resumed"].includes(entry.cache_state) ? "历史 / 非新提交参考用量，不计入本轮新增"
          : entry.cache_state === "not_submitted" ? "未提交用量，不视为本轮新增"
            : "来源未确认用量，不据此认定本轮新增";
      const title = el("summary", "", `${service} · ${usageText(entry.stage)} · ${cacheLabels[entry.cache_state] || "来源未提供"} · 新增估算 ${usageCost(entry.current_cost, entry.current_cost_range)}`);
      title.id = `${call.id}-toggle`;
      call.append(title, el("p", "usage-call-meta",
        `调用 ${usageText(entry.id)} · 区域索引 ${usageText(entry.region_index)} · ${usageLabels[entry.usage_status] || "用量状态未提供"}\n模型 ${usageText(entry.model)} · 部署 ${usageText(entry.deployment)}\n参考费用 ${usageCost(entry.reference_cost, entry.reference_cost_range)}（引用用量按当前配置价格快照重估，非历史账单，非本轮新增） · 已知计量小计 ${usageNumber(entry.known_cost) ? usageMoney(entry.known_cost) : "未提供"}（非完整总额）`),
      el("p", "usage-note", `${metricScope}：${usageMetrics(entry.metrics, entry.service === "cu" ? "CU 内部模型" : "直接对比模型")}`));
      const raw = el("details", "usage-raw");
      raw.id = `${call.id}-raw`;
      const rawToggle = el("summary", "", "原始提供方用量（不直接求和）");
      rawToggle.id = `${raw.id}-toggle`;
      raw.append(rawToggle, el("p", "usage-note", "保留 API 原始计数以便核对；字段可能重叠，不等于规范化总量或本轮新增计费。"),
        el("pre", "", usageText(entry.raw_usage)));
      call.append(raw);
      const meters = Array.isArray(entry.meters) ? entry.meters : [];
      if (!meters.length) call.append(el("p", "usage-note", "计量与价格明细：未提供。"));
      else {
        const scroll = el("div", "usage-table-scroll");
        scroll.tabIndex = 0;
        scroll.setAttribute("role", "region");
        scroll.setAttribute("aria-label", `${service} ${usageText(entry.stage)} 计量价格表，可横向滚动`);
        const table = el("table", "usage-table"), head = el("thead"), row = el("tr"), body = el("tbody");
        table.append(el("caption", "", "计量项估算 = 用量 ÷ 计价单位数量 × 单价。引用历史操作的计量金额仅供参考；各项与总额由服务端核算，不在页面相加。"));
        for (const label of ["计量类别 / 项目", "用量", "单价 / 单位", "公式 / 估算 USD", "价格来源 / 限制"]) {
          const cell = el("th", "", label);
          cell.scope = "col";
          row.append(cell);
        }
        head.append(row);
        meters.forEach((meter, meterIndex) => {
          if (!meter || typeof meter !== "object") return;
          const rate = meter.rate, unit = unitLabels[meter.unit] || usageText(meter.unit);
          const candidates = Array.isArray(meter.rate_candidates) ? meter.rate_candidates.filter((candidate) => candidate && typeof candidate === "object") : [];
          const quantityRange = Array.isArray(meter.quantity_range) && meter.quantity_range.length === 2 &&
            usageRange(meter.quantity_range[0], meter.quantity_range[1]);
          const quantity = usageNumber(meter.quantity) ? usageCount(meter.quantity) : quantityRange
            ? `${usageCount(meter.quantity_range[0])} – ${usageCount(meter.quantity_range[1])}（场景用量）` : "未提供";
          const formula = (price) => !usageNumber(meter.quantity) && !quantityRange ? "用量未提供，无法估算"
            : !rateAvailable(price) ? "单价待配置，无法估算"
              : price.currency !== "USD" ? "币种未确认为 USD，不进行换算"
                : `${quantity} ÷ ${usageCount(price.unit_quantity)} × ${usageMoney(price.price)}`;
          const tr = el("tr"), sourceCell = el("td"), detail = el("details");
          detail.id = `${call.id}-meter-${meterIndex}`;
          const toggle = el("summary", "", "来源与计价依据");
          toggle.id = `${detail.id}-toggle`;
          detail.append(toggle);
          if (rate || !candidates.length) detail.append(usageRateDetails(rate, "单价依据"));
          candidates.forEach((candidate, candidateIndex) => detail.append(usageRateDetails(candidate,
            `候选价格 ${candidateIndex + 1} · ${tierLabels[candidate.context_tier] || usageText(candidate.context_tier)}（适用性未确认）`)));
          detail.append(el("p", "", `计量键：${usageText(meter.key)} · 说明：${reasonLabels[meter.reason] || usageText(meter.reason)}`));
          sourceCell.append(detail);
          const fee = el("td"), rateCell = el("td", "usage-rate");
          if (rateAvailable(rate) || !candidates.length) {
            rateCell.append(el("p", "", ratePrice(rate, unit)));
            if (rate?.context_tier) rateCell.append(el("p", "", tierLabels[rate.context_tier] || usageText(rate.context_tier)));
            fee.append(el("p", "", formula(rate)));
          } else {
            candidates.forEach((candidate) => {
              const tier = tierLabels[candidate.context_tier] || usageText(candidate.context_tier);
              rateCell.append(el("p", "", `${tier}：${ratePrice(candidate, unit)}`));
              fee.append(el("p", "", `${tier}场景：${formula(candidate)}`));
            });
            rateCell.append(el("p", "usage-note", "候选档位，未选择；不相加"));
          }
          fee.append(el("p", "", `计量估算：${usageCost(meter.estimated_cost, meter.estimated_cost_range)}`));
          tr.append(el("td", "", `${meterLabels[meter.category] || usageText(meter.category)} · ${usageText(meter.label)}`),
            el("td", "usage-number", `${quantity} ${unit}`), rateCell, fee, sourceCell);
          body.append(tr);
        });
        table.append(head, body);
        scroll.append(table);
        call.append(scroll);
      }
      if (Array.isArray(entry.warnings)) entry.warnings.forEach((warning) => call.append(el("p", "usage-warning", warning)));
      content.append(call);
    });
    for (const id of opened) if ($(id)) $(id).open = true;
    if (focused && $(focused)) $(focused).focus({ preventScroll: true });
    $("usage-panel").scrollTop = scrollTop;
  }
  function status(message) { $("job-status").textContent = message; }
  function error(message, retry = null) {
    $("error-message").textContent = message;
    $("error-banner").hidden = false;
    $("retry-operation").hidden = !retry;
    $("retry-operation").onclick = retry;
  }
  function clearError() {
    $("error-banner").hidden = true;
    $("retry-operation").hidden = true;
    $("retry-operation").onclick = null;
  }
  async function request(url, options = {}) {
    const { responseType, ...fetchOptions } = options;
    const headers = new Headers(options.headers);
    if (options.method && options.method !== "GET") headers.set("X-CSRF-Token", state.csrf);
    let response;
    try {
      response = await fetch(url, { ...fetchOptions, headers, credentials: "same-origin", cache: "no-store" });
    } catch (err) {
      if (err.name === "AbortError") throw err;
      throw new Error("连接中断，请检查网络后重试。");
    }
    if (response.ok && responseType === "pdf") {
      if (!(response.headers.get("Content-Type") || "").toLowerCase().startsWith("application/pdf")) {
        throw new Error("导出响应不是PDF，未下载文件。");
      }
      const blob = await response.blob();
      if (await blob.slice(0, 5).text() !== "%PDF-") throw new Error("导出PDF内容无效，未下载文件。");
      return blob;
    }
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("服务器响应异常，请稍后重试。"); }
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}），请重试。`);
    return data;
  }
  function identicalFiles() {
    return Boolean(state.old.document?.sha256) &&
      state.old.document.sha256 === state.new.document?.sha256;
  }
  const graphicsUnavailable = "图形检测未接入/未启用：当前仅展示文字与表格证据。";
  const modelDisclaimer = "模型提出对应关系，CU局部复读提供原文；不是模型文字直接作为证据";
  const modelLimits = "粗比对后按预算裁切并用CU局部复读，可能产生模型及CU费用；预算、来源或定位限制可能留下未处理区域，不保证没有遗漏。";
  function updateGraphicsAvailability() {
    const menu = $("channel-filter");
    menu.querySelector('[value="primary"]').textContent = state.graphicsEnabled ? "字段 + 表格 + 图形候选" : "字段 + 表格（默认）";
    if (state.modelEnabled) menu.querySelector('[value="primary"]').textContent = state.graphicsEnabled
      ? "模型 + 字段 + 表格 + 图形" : "模型 + 字段 + 表格";
    $("model-status").hidden = !state.modelEnabled;
    $("model-status").textContent = state.modelEnabled ? `模型引导对比已启用（服务配置）。${modelLimits}` : "";
    const option = menu.querySelector('[value="graphics"]');
    option.disabled = !state.graphicsEnabled;
    option.textContent = state.graphicsEnabled ? "本地图形候选" : "本地图形候选（未启用）";
    if (!state.graphicsEnabled && menu.value === "graphics") menu.value = "primary";
    for (const id of ["show-translation", "show-scaling"]) $(id).disabled = !state.graphicsEnabled;
    $("graphics-status").textContent = state.graphicsEnabled
      ? "本地图形检测已启用；默认只比较设计内容。平移、绘图缩放可单独勾选显示，不计内容变更。"
      : graphicsUnavailable;
    $("filter-note").textContent = state.graphicsEnabled
      ? "平移和绘图缩放默认隐藏；勾选只改变本地显示，不重新调用 CU。红色实框：实际残差；红色虚框：对侧映射的对应位置，非本侧残差；黄色虚框：复核证据或可选视图范围。缩放伴随内容修改时，内容候选仍保留。"
      : "默认显示字段与表格专项候选，不重复展示 OCR。表格与字段可能重叠，不相加为独立变更数；黄色虚框仅供复核。";
    $("coverage-content").textContent = `尚未运行对比。${state.graphicsEnabled ? "图形覆盖以本轮服务返回的统计为准。" : graphicsUnavailable}系统不会将缺失位置的证据推测成红框。`;
  }
  function updateControls() {
    const busy = sides.some((side) => state[side].pending);
    $("compare-button").disabled = !state.ready || busy || state.comparing || state.exporting ||
      !sides.every((side) => state[side].document) || identicalFiles();
    $("export-button").disabled = !state.ready || busy || state.comparing || state.exporting ||
      !state.result || !state.jobId || !sides.every((side) => state[side].document && state[side].loaded);
    $("export-button").textContent = state.exporting ? "正在导出…" : "导出 PDF";
    $("file-identity-status").classList.toggle("identical", identicalFiles());
    $("file-identity-status").textContent = identicalFiles()
      ? "两侧 SHA256 完全相同：是同一份文件字节，已阻止重复对比，请更换其中一份。"
      : sides.every((side) => state[side].document)
        ? "两侧文件 SHA256 不同（不代表每处内容都不同）；固定按 A 原图 → B 调整图比较。"
        : "上传后显示文件 SHA256，核对是否为同一份文件。";
    $("compare-button").textContent = state.comparing ? "正在对比…" : "开始对比 ↗";
    for (const side of sides) {
      const available = Boolean(state[side].document) && !state[side].pending;
      $(`${side}-upload`).disabled = !state.ready;
      $(`${side}-choose`).disabled = !state.ready;
      $(`${side}-replace`).disabled = !state.ready;
      $(`${side}-remove`).disabled = !state.ready || (!available && !state[side].pending);
      for (const name of ["page", "zoom", "fit"]) $(`${side}-${name}`).disabled = !available;
      $(`${side}-viewport`).setAttribute("aria-busy", String(state[side].pending));
    }
  }
  function invalidateResults() {
    state.generation++;
    if (state.jobController) state.jobController.abort();
    if (state.exportController) state.exportController.abort();
    state.exportController = null;
    state.exporting = false;
    state.jobId = null;
    state.jobController = null;
    state.comparing = false;
    state.result = null;
    state.selected = null;
    renderUsage(null);
    sides.forEach((side) => $(`${side}-stage`).querySelector("svg").replaceChildren());
    renderResults();
    renderDetails(null);
    $("coverage-content").textContent = `文件已更改，旧结果已清除。重新对比后显示覆盖范围。${state.graphicsEnabled ? "" : graphicsUnavailable}`;
    for (const side of sides) $(`${side}-evidence-note`).textContent = "尚无本轮对比证据。";
    updateControls();
  }
  async function bootstrap() {
    $("retry-bootstrap").hidden = true;
    clearError();
    try {
      const data = await request("/api/bootstrap");
      state.csrf = data.csrf_token;
      state.revision = data.revision;
      state.limits = data.limits;
      state.azure = Boolean(data.azure_enabled);
      state.graphicsEnabled = data.graphics_enabled === true;
      state.modelEnabled = data.model_comparison_enabled === true;
      updateGraphicsAvailability();
      state.ready = true;
      $("connection-status").textContent = "会话已连接";
      const modelLabel = state.modelEnabled && data.model_comparison_deployment
        ? `CU：${data.model || "未提供"} · 模型对比：${data.model_comparison_deployment}` : (data.model || "模型");
      $("model-tag").textContent = `${modelLabel} / ${state.azure ? "缓存优先 · 可提交 CU" : "只读缓存模式"}`;
      $("storage-notice").textContent = data.storage_notice || `会话存储期限：${data.limits.session_ttl_hours} 小时。`;
      document.querySelectorAll(".upload-limit").forEach((node) => {
        node.textContent = `PDF · 单文件最大 ${Math.round(data.limits.max_bytes / 1048576)} MB · 最多 ${data.limits.max_pages} 页`;
      });
      for (const side of sides) {
        state[side].document = data.documents?.[side] || null;
        renderDocument(side);
      }
      status(state.azure ? "请上传原图和调整图；点击对比才会提交未缓存的文件。" : "只读缓存模式：允许预览和对比已有缓存；缓存未命中会明确报错，不上传 Azure。");
      updateControls();
    } catch (err) {
      error(err.message);
      status("初始化失败，请重新连接。");
      $("connection-status").textContent = "未连接";
      $("retry-bootstrap").hidden = false;
    }
  }
  function mutate(side, file) {
    if (!state.ready) return;
    if (file && (!/\.pdf$/i.test(file.name) || (file.type && !["application/pdf", "application/octet-stream"].includes(file.type)))) {
      invalidateResults(); error("请选择 PDF 文件；无效文件未替换当前图纸，旧结果已清除。"); return;
    }
    if (file && (!file.size || file.size > state.limits.max_bytes)) {
      invalidateResults();
      error(`文件不能为空，且不得超过 ${Math.round(state.limits.max_bytes / 1048576)} MB；旧结果已清除。`); return;
    }
    clearError();
    invalidateResults();
    const epoch = ++state[side].epoch;
    state[side].pending = true;
    state[side].document = null;
    renderDocument(side);
    $(`${side}-filename`).textContent = file ? `正在上传：${file.name}` : "正在移除…";
    status(`${sideName[side]}图纸${file ? "正在上传并校验" : "正在移除"}…`);
    updateControls();
    // Serialize mutations so a late server commit cannot replace a newer file or revision.
    state.mutationQueue = state.mutationQueue.then(async () => {
      if (epoch !== state[side].epoch) return;
      try {
        const data = await request(`/api/documents/${side}`, file ? {
          method: "PUT", body: file,
          headers: { "Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name) }
        } : { method: "DELETE" });
        state.revision = Math.max(state.revision, data.revision);
        if (epoch !== state[side].epoch) return;
        state[side].document = file ? data.document : null;
        renderDocument(side);
      } catch (err) {
        if (epoch !== state[side].epoch) return;
        error(`${sideName[side]}图纸：${err.message}`, () => mutate(side, file));
        $(`${side}-filename`).textContent = "操作失败，请重试";
      } finally {
        if (epoch === state[side].epoch) {
          state[side].pending = false;
          updateControls();
          if (!sides.some((s) => state[s].pending)) {
            status(identicalFiles() ? "两侧是同一份文件，请更换其中一份后再对比。"
              : sides.every((s) => state[s].document) ? "两份图纸已就绪，请开始对比。" : "等待上传有效的旧版和新版图纸。");
          }
        }
      }
    });
  }
  function renderDocument(side) {
    const s = state[side];
    s.imageEpoch++;
    s.loaded = false;
    s.page = 1;
    const stage = $(`${side}-stage`);
    const image = stage.querySelector("img");
    image.onload = null;
    image.onerror = null;
    image.removeAttribute("src");
    stage.querySelector("svg").replaceChildren();
    stage.hidden = true;
    $(`${side}-empty`).hidden = Boolean(s.document);
    $(`${side}-filename`).textContent = s.document ? s.document.name : "未上传文件";
    $(`${side}-filename`).title = s.document ? s.document.name : "";
    const hash = s.document?.sha256;
    $(`${side}-identity`).textContent = hash ? `SHA256 ${hash.slice(0, 12)}…${hash.slice(-8)}` : "";
    $(`${side}-identity`).title = hash || "";
    const menu = $(`${side}-page`);
    menu.replaceChildren();
    if (!s.document) {
      menu.append(new Option("— / —", ""));
    } else {
      for (const page of s.document.pages) menu.append(new Option(`${page.number} / ${s.document.page_count}`, String(page.number)));
      s.page = s.document.pages[0]?.number || 1;
      menu.value = String(s.page);
      showPage(side);
    }
    updateControls();
  }
  function resizeStage(side) {
    const s = state[side];
    if (!s.document) return;
    const viewport = $(`${side}-viewport`);
    const stage = $(`${side}-stage`);
    const image = stage.querySelector("img");
    const page = s.document.pages.find((p) => p.number === s.page);
    const css = getComputedStyle(viewport);
    const room = viewport.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight);
    const rotated = Math.abs(page?.rotation || 0) % 180 === 90;
    const widthPt = (rotated ? page?.height_pt : page?.width_pt) || 612;
    stage.style.width = `${Math.max(1, s.zoom === "fit" ? room : widthPt * 96 / 72 * Number(s.zoom) / 100)}px`;
    if (s.loaded && image.naturalWidth) renderBoxes(side);
  }
  function showPage(side, scrollSelection = false) {
    const s = state[side];
    if (!s.document) return;
    const token = ++s.imageEpoch;
    const stage = $(`${side}-stage`);
    const image = stage.querySelector("img");
    s.loaded = false;
    updateControls();
    stage.hidden = true;
    stage.querySelector("svg").replaceChildren();
    $(`${side}-page`).value = String(s.page);
    $(`${side}-evidence-note`).textContent = `正在载入第 ${s.page} 页…`;
    image.onload = () => {
      if (token !== s.imageEpoch) return;
      s.loaded = true;
      stage.hidden = false;
      resizeStage(side);
      evidenceNote(side);
      if (scrollSelection || state.selected) scrollToEvidence(side);
      updateControls();
    };
    image.onerror = () => {
      if (token !== s.imageEpoch) return;
      s.loaded = false;
      stage.hidden = true;
      $(`${side}-evidence-note`).replaceChildren(el("span", "", "页面预览加载失败。 "), retryButton(() => showPage(side, scrollSelection)));
      updateControls();
    };
    image.src = `/api/documents/${encodeURIComponent(s.document.id)}/pages/${s.page}?width=1600`;
    $(`${side}-viewport`).scrollTo({ top: 0, left: 0 });
  }
  function retryButton(action) {
    const button = el("button", "", "重试");
    button.type = "button";
    button.addEventListener("click", action);
    return button;
  }
  function bounds(location) {
    let { x, y, width, height } = location;
    if (Array.isArray(location.polygon) && location.polygon.length >= 3 &&
        location.polygon.every((p) => Array.isArray(p) && p.length >= 2 && Number.isFinite(p[0]) && Number.isFinite(p[1]))) {
      const xs = location.polygon.map((p) => p[0]);
      const ys = location.polygon.map((p) => p[1]);
      x = Math.min(...xs); y = Math.min(...ys);
      width = Math.max(...xs) - x; height = Math.max(...ys) - y;
    }
    if (![x, y, width, height].every(Number.isFinite) || width <= 0 || height <= 0) return null;
    const left = Math.max(0, x), top = Math.max(0, y);
    const right = Math.min(1, x + width), bottom = Math.min(1, y + height);
    return right > left && bottom > top ? { x: left, y: top, width: right - left, height: bottom - top } : null;
  }
  function validLocations(entries, side) {
    const pages = state[side].document?.pages || [];
    return (Array.isArray(entries) ? entries : []).filter((loc) =>
      loc && pages.some((page) => page.number === loc.page) && bounds(loc));
  }
  function locations(item, side) {
    return validLocations(item?.[side]?.locations, side);
  }
  function counterpartLocations(item, side) {
    if (item?.channel !== "graphics" || !pairedDifference(item) || !item.old || !item.new ||
        item.graphics?.alignment?.accepted !== true || locations(item, side).length) return [];
    return validLocations(item[side].counterpart_locations, side)
      .filter((loc) => loc.evidence_role === "projected_counterpart" &&
        loc.from_side === (side === "old" ? "new" : "old") &&
        Number.isInteger(loc.source_location_index) && loc.source_location_index >= 0 &&
        loc.source_location_index < locations(item, loc.from_side).length);
  }
  function navigationLocations(item, side) {
    const evidence = locations(item, side);
    if (!evidence.length && item?.channel === "model") {
      const context = modelVisualItem(item) ? validLocations(item[side]?.context_locations, side) : [];
      if (context.length) return context;
      return validLocations(item.model_context?.[side]?.locations, side);
    }
    if (!evidence.length && annotationReview(item)) {
      return validLocations(item.annotation_context?.[side]?.locations, side);
    }
    if (!evidence.length && (tableRowChange(item) || documentTableChange(item))) {
      return validLocations(item.table_context?.[side]?.locations, side);
    }
    const counterparts = counterpartLocations(item, side);
    if (counterparts.length) return counterparts;
    // Context is only a navigation fallback for a paired graphical region, never a change box.
    if (evidence.length || item?.channel !== "graphics" || !item.old || !item.new) return evidence;
    return validLocations(item[side].context_locations, side);
  }
  function uncertain(item) {
    const certainty = item.match?.certainty;
    return (typeof certainty === "number" && certainty < 0.8) ||
      ["low", "uncertain", "ambiguous", "model_proposed", "低", "低确定性"].includes(String(certainty).toLowerCase());
  }
  function modelTextDifference(item) {
    return item?.channel === "model" && item.change === "model_text_modified" &&
      item.model_comparison?.status === "source_grounded" && item.model_comparison?.stage === "fine" &&
      typeof item.old?.raw_text === "string" && Boolean(item.old.raw_text.trim()) &&
      typeof item.new?.raw_text === "string" && Boolean(item.new.raw_text.trim()) &&
      item.old.raw_text !== item.new.raw_text;
  }
  function modelVisualItem(item) {
    return item?.channel === "model" && (item.model_comparison?.stage === "visual" ||
      ["model_visual_modified", "model_visual_review"].includes(item.change));
  }
  function modelVisualDifference(item) {
    return modelVisualItem(item) && item.change === "model_visual_modified" &&
      item.model_comparison?.stage === "visual" && item.model_comparison?.status === "visual_grounded" &&
      item.model_comparison?.highlight_scope === "nontext_residual_only" &&
      item.visual_comparison?.status === "localized" &&
      item.visual_comparison?.measurement_status !== "unmeasured" &&
      sides.every((side) => item[side]?.raw_text === "" &&
        Array.isArray(item[side]?.source) && item[side].source.some((source) => source?.kind === "pdf_raster"));
  }
  function pairedDifference(item) {
    return Boolean(item.old && item.new) &&
      ["modified", "relocated", "visual_modified", "visual_annotation"].includes(item.change);
  }
  function tableRowChange(item) {
    return item?.channel === "schema" && item.table_comparison?.status === "complete" &&
      ((item.change === "table_row_added" && !item.old && Boolean(item.new)) ||
       (item.change === "table_row_removed" && !item.new && Boolean(item.old)));
  }
  function documentTableChange(item) {
    if (item?.channel !== "tables" || item.table_comparison?.status !== "complete") return false;
    if (["table_cell_modified", "table_grid_changed"].includes(item.change)) return Boolean(item.old && item.new);
    return (item.change === "table_column_added" && !item.old && Boolean(item.new)) ||
      (item.change === "table_column_removed" && !item.new && Boolean(item.old));
  }
  function annotationReview(item) {
    return item?.channel === "schema" && item.change === "annotation_occurrence_changed" &&
      item.annotation_comparison?.status === "observed";
  }
  function contentDifference(item) {
    return pairedDifference(item) || tableRowChange(item) || documentTableChange(item) ||
      modelTextDifference(item) || modelVisualDifference(item);
  }
  function transformation(item) {
    return ["visual_moved", "visual_scaled"].includes(item.change);
  }
  function evidenceStyle(item) {
    return contentDifference(item) ? "" : " review-evidence";
  }
  function unpaired(item) {
    return ["unpaired_old", "unpaired_new"].includes(item.change);
  }
  function visibleItems() {
    return (state.result?.items || []).filter((item) => {
      if (!state.graphicsEnabled && item.channel === "graphics") return false;
      if (item.change === "visual_moved" && !$("show-translation").checked) return false;
      if (item.change === "visual_scaled" && !$("show-scaling").checked) return false;
      if (!$("show-interpretation").checked && item.change === "interpretation_only") return false;
      const channel = $("channel-filter").value;
      if (channel === "primary") {
        if (!["model", "schema", "tables", "graphics"].includes(item.channel)) return false;
      } else if (channel !== "all" && item.channel !== channel) return false;
      switch ($("review-filter").value) {
        case "paired": return (item.channel === "model" && item.change !== "model_no_text_change") || contentDifference(item) || annotationReview(item) || Boolean(item.old && item.new && transformation(item));
        case "formatting": return item.change === "formatting_only";
        case "review": return Boolean(item.review_required);
        case "uncertain": return uncertain(item);
        case "unpaired": return unpaired(item);
        default: return true;
      }
    });
  }
  function renderResults() {
    const list = $("results-list");
    list.replaceChildren();
    const items = visibleItems();
    const modelItems = (state.result?.items || []).filter((item) => item.channel === "model" && item.change !== "model_no_text_change");
    $("model-count").hidden = !modelItems.length;
    $("model-count").textContent = modelItems.length
      ? `模型通道：${modelItems.filter(modelTextDifference).length} 条局部原文不同候选 / ${modelItems.filter((item) => !modelTextDifference(item) && !modelVisualItem(item)).length} 条待复核；非文字：${modelItems.filter(modelVisualDifference).length} 条填充／轮廓变化候选 / ${modelItems.filter((item) => modelVisualItem(item) && !modelVisualDifference(item)).length} 条定位未解决。可能与其他通道重叠，不相加为独立变更数。` : "";
    renderResultStatus(items);
    $("result-count").textContent = state.result ? String(items.length) : "—";
    $("result-count").title = state.result ? `当前显示 ${items.length} / 总计 ${state.result.items.length}` : "";
    if (!items.length) {
      const empty = el("div", "list-empty");
      empty.append(el("span", "", "⌕"), el("h3", "", state.result ? "当前筛选下没有条目" : "等待对比结果"),
        el("p", "", state.result ? "可调整通道或复核范围。" : "上传两份图纸后，点击「开始对比」。"));
      list.append(empty);
      return;
    }
    const groups = [...new Set(items.map((item) => item.channel))];
    if (groups.includes("model")) groups.splice(0, 0, ...groups.splice(groups.indexOf("model"), 1));
    for (const channel of groups) {
      const grouped = items.filter((item) => item.channel === channel);
      list.append(el("h3", "channel-heading", `${channels[channel] || channel} / ${grouped.length}`));
      for (const item of grouped) {
        const row = el("div", "result-item");
        row.dataset.id = item.id;
        row.dataset.channel = item.channel;
        row.dataset.change = item.change;
        const button = el("button", "result-button");
        button.type = "button";
        button.dataset.id = item.id;
        button.setAttribute("aria-pressed", String(state.selected === item.id));
        button.append(el("span", "result-id", item.id), el("span", "result-title", item.key || item.region || "未命名条目"),
          el("span", "result-change", changes[item.change] || item.change),
          el("span", "result-subtitle", `${item.region || "未分类"}${item.review_required ? " · 需复核" : ""}${uncertain(item) ? " · 低确定性" : ""}`));
        button.addEventListener("click", () => selectItem(item.id));
        row.append(button);
        list.append(row);
      }
    }
    function renderResultStatus(visible) {
      const result = state.result;
      if (!result) return;
      const hidden = result.items.filter((item) => item.change === "interpretation_only").length;
      const unresolved = result.items.filter(unpaired).length;
      const formatting = result.items.filter((item) => item.change === "formatting_only").length;
      const graphicsUncertain = result.items.filter((item) => item.change === "visual_uncertain").length;
      const optional = visible.filter(transformation).length;
      const rowChanges = result.items.filter(tableRowChange).length;
      const tableChanges = result.items.filter(documentTableChange).length;
      const annotations = result.items.filter(annotationReview).length;
      status(`对比完成 · ${result.items.filter(pairedDifference).length} 条已配对差异候选${rowChanges ? ` · ${rowChanges} 条表格行增删候选` : ""}${tableChanges ? ` · ${tableChanges} 条表格专项候选（可能与字段/OCR重叠）` : ""}${annotations ? ` · ${annotations} 条图外标注次数待核（非确认增删）` : ""} · ${unresolved} 条未配对待复核${graphicsUncertain ? ` · ${graphicsUncertain} 条图形对应不确定，见人工复核` : ""} · ${formatting} 条仅格式差异${hidden ? ` · ${hidden} 个仅解释差异默认隐藏` : ""} · 非已确认变更数${state.graphicsEnabled ? optional ? ` · 另显示 ${optional} 条平移/缩放提示（不计内容变更）` : " · 仅设计内容" : " · 图形检测未接入/未启用"}`);
    }
  }
  function svgNode(tag, attributes) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    return node;
  }
  function renderBoxes(side) {
    const s = state[side], stage = $(`${side}-stage`), svg = stage.querySelector("svg");
    svg.replaceChildren();
    if (!s.loaded) return;
    // The SVG shares the image's exact CSS box; normalized coordinates never use the viewport.
    const items = visibleItems().slice().sort((a, b) => Number(a.id === state.selected) - Number(b.id === state.selected));
    for (const item of items) {
      let labelled = false;
      const displayed = [
        ...locations(item, side).map((loc) => ({ loc, counterpart: false })),
        ...counterpartLocations(item, side).map((loc) => ({ loc, counterpart: true }))
      ];
      for (const { loc, counterpart } of displayed.filter(({ loc }) => loc.page === s.page)) {
        const box = bounds(loc);
        const style = counterpart ? " counterpart-evidence" : evidenceStyle(item);
        const rect = svgNode("rect", {
          x: box.x * 1000, y: box.y * 1000, width: box.width * 1000, height: box.height * 1000,
          class: `evidence-box${style}${item.id === state.selected ? " selected" : ""}`,
          "data-id": item.id, "vector-effect": "non-scaling-stroke", tabindex: 0, role: "button",
          "data-field": loc.field || "",
          "data-evidence-role": counterpart ? "projected_counterpart" : transformation(item) ? "transformation_frame" : "observed",
          "data-channel": item.channel, "data-change": item.change,
          "aria-label": `${item.id} ${loc.label || item.key || item.region || ""}，${counterpart ? "对侧残差映射定位，非本侧修改证据" : changes[item.change] || item.change}，${sideName[side]}第 ${s.page} 页`
        });
        rect.addEventListener("click", () => selectItem(item.id));
        rect.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectItem(item.id); }
        });
        svg.append(rect);
        if (!labelled && (!state.selected || item.id === state.selected)) {
          const label = svgNode("text", {
            x: Math.min(box.x * 1000 + 2, 940), y: Math.max(18, box.y * 1000 - 5),
            class: `evidence-label${style}`, "font-size": Math.max(10, 11 * 1000 / (stage.clientHeight || 1000)),
            "aria-hidden": "true"
          });
          label.textContent = `${item.id}${counterpart ? " 对应" : transformation(item) ? " 视图范围" : contentDifference(item) ? "" : " 待核"}`;
          svg.append(label);
          labelled = true;
        }
      }
    }
  }
  function evidenceNote(side) {
    const item = state.result?.items.find((i) => i.id === state.selected);
    const node = $(`${side}-evidence-note`);
    if (!item) { node.textContent = state.result ? "点击证据框或索引查看证据；黄色虚框不是确认内容变更。" : "预览已就绪，等待开始对比。"; return; }
    const source = item[side], located = locations(item, side), counterparts = counterpartLocations(item, side);
    if (item.channel === "model") {
      const context = navigationLocations(item, side);
      if (modelVisualItem(item)) {
        node.textContent = `${item.id} · ${located.length
          ? `${sideName[side]}第 ${[...new Set(located.map((loc) => loc.page))].join("、")} 页 · ${modelVisualDifference(item) ? "本地PDF栅格非文字残差（候选，非CU词框）" : "定位未解决，黄色虚框仅供复核，非确认变更"}`
          : context.length ? "本侧无局部残差框；仅导航搜索区域上下文，不绘制变化框"
            : "本侧无局部残差框或可用上下文；不绘制推测框"}${source?.location_error ? `；${source.location_error}` : ""}`;
        return;
      }
      node.textContent = `${item.id} · ${located.length
        ? `${sideName[side]}第 ${[...new Set(located.map((loc) => loc.page))].join("、")} 页${modelTextDifference(item) ? "CU局部复读证据；对应关系由模型提出，仍需复核" : "复核来源（黄色虚框，非确认变更）"}`
        : source && context.length
          ? "本侧无变化词框，仅导航已提取上下文，非本侧变化证据"
          : `${source ? "缺少可定位原文" : "未配对到证据，不代表原图没有"}${context.length ? "；仅导航模型上下文，不绘制变化框" : "；不绘制推测框"}`}${source?.location_error ? `；${source.location_error}` : ""}`;
      return;
    }
    if (!source && annotationReview(item)) {
      node.textContent = `${item.id} · 本侧未提取到对应图外标注；仅定位到文字未变的BOM行作为上下文，不将BOM当作图外标注，也不伪造删除位置`;
    }
    else if (!source && tableRowChange(item)) {
      const context = navigationLocations(item, side);
      node.textContent = `${item.id} · 本侧CU表格未提取到该行，仍需核对原图${context.length ? "；定位到对应表格（仅上下文，不伪造缺失行红框）" : "；缺少表格定位来源"}`;
    }
    else if (!source && documentTableChange(item)) {
      node.textContent = `${item.id} · 对应完整网格内未发现该列；仅导航到本侧表格，不伪造缺失列红框，仍需原图复核`;
    }
    else if (!source) node.textContent = `${item.id} · 未配对到证据，不代表本侧图纸没有该内容`;
    else if (counterparts.length) {
      node.textContent = `${item.id} · 本侧无残差框；红色虚框为${sideName[counterparts[0].from_side]}残差映射的对应位置（非本侧修改证据）${counterparts.some((loc) => loc.page === state[side].page) ? "" : `；位于第 ${[...new Set(counterparts.map((loc) => loc.page))].join("、")} 页`}`;
    }
    else if (item.channel === "graphics" && !located.length) {
      const context = navigationLocations(item, side);
      node.textContent = context.length
        ? `${item.id} · 无局部残差框；第 ${[...new Set(context.map((loc) => loc.page))].join("、")} 页为配对上下文（仅导航，不是变化证据，不绘框）`
        : `${item.id} · 无法定位局部残差；不绘制推测框${source.location_error ? `：${source.location_error}` : ""}`;
    }
    else if (!located.length) node.textContent = `${item.id} · 无法定位${source.location_error ? `：${source.location_error}` : "；保留原文，不绘制推测框"}`;
    else if (!located.some((loc) => loc.page === state[side].page)) node.textContent = `${item.id} · 本页无对应证据；证据位于第 ${[...new Set(located.map((loc) => loc.page))].join("、")} 页`;
    else node.textContent = `${item.id} · 第 ${state[side].page} 页${item.change === "table_grid_changed" ? "表格网格证据（非文字记录增删）" : transformation(item) ? "对应视图范围（平移/缩放提示，非内容残差）" : item.channel === "graphics" ? "局部像素残差（候选）" : "证据"}${located.length > 1 ? ` · 共 ${located.length} 处，可切换页码查看` : ""}`;
  }
  function selectItem(id) {
    const item = state.result?.items.find((i) => i.id === id);
    if (!item) return;
    selectDetailTab("evidence");
    state.selected = id;
    document.querySelectorAll(".result-button").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.id === id)));
    renderDetails(item);
    for (const side of sides) {
      const first = navigationLocations(item, side)[0];
      if (first && first.page !== state[side].page) {
        state[side].page = first.page;
        showPage(side, true);
      } else {
        renderBoxes(side);
        evidenceNote(side);
        if (first) scrollToEvidence(side);
      }
    }
  }
  function scrollToEvidence(side) {
    const item = state.result?.items.find((i) => i.id === state.selected);
    const loc = navigationLocations(item, side).find((l) => l.page === state[side].page);
    if (!loc || !state[side].loaded) return;
    const box = bounds(loc), viewport = $(`${side}-viewport`), stage = $(`${side}-stage`);
    const stageRect = stage.getBoundingClientRect(), paneRect = viewport.getBoundingClientRect();
    viewport.scrollTo({
      left: viewport.scrollLeft + stageRect.left - paneRect.left + (box.x + box.width / 2) * stageRect.width - viewport.clientWidth / 2,
      top: viewport.scrollTop + stageRect.top - paneRect.top + (box.y + box.height / 2) * stageRect.height - viewport.clientHeight / 2,
      behavior: "auto"
    });
  }
  function renderGraphicsDetails(item) {
    const graphics = item.graphics || {}, alignment = graphics.alignment || {}, subview = graphics.subview;
    const optional = transformation(item);
    const section = el("section", "graphics-detail");
    section.append(el("h3", "", optional ? "可选视图提示 · 不计设计内容变更" : "本地图形证据 · 候选，非工程结论"));
    section.append(el("p", "graphics-disclaimer", optional
        ? "框表示对应视图的范围，不是变化像素。平移/绘图缩放不等于实物移动或尺寸改变，也不表示视图内容一定相同；内容残差独立保留。"
        : item.change === "visual_uncertain"
        ? "无法可靠建立图形对应关系，必须人工复核；不能认定内容变化或无变化。"
        : "仅核对独立对齐后的设计内容；忽略视图位置变化，外观残差仍需复核，不能推断真实材质或尺寸。"));
    const metric = (value, suffix = "") => Number.isFinite(value) ? `${value}${suffix}` : "未提供";
    const fraction = (value) => Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : "未提供";
    const facts = [
      ["候选分类", graphics.classification || changes[item.change]],
      ["检测方法", graphics.method || "未提供"],
      ["渲染分辨率", metric(graphics.dpi, " dpi")],
      ["配准可靠性", alignment.accepted === true ? "配准已接受（不是工程内容确认）" : alignment.accepted === false ? "配准未接受 / 不可靠" : "未提供"],
      ["配准方法 / 响应", `${text(alignment.method) || "未提供"} / ${metric(alignment.response)}（非准确率）`],
      ["对应确定性", item.match?.certainty === "high" ? "高（非概率保证）" : "不确定，需人工复核"],
      ["区域来源", ({ cu_figure: "CU 图形区域", local_subview: "本地轮廓子图（非语义零件识别）", page_fallback: "页面回退（对应关系需复核）" })[graphics.region_source] || text(graphics.region_source) || "未提供"]
    ];
    if (item.change === "visual_moved") {
      facts.push(["纸面中心平移（非实物移动）",
        `Δx ${metric(graphics.translation_pt?.dx, " pt")} / Δy ${metric(graphics.translation_pt?.dy, " pt")}`]);
    } else if (item.change === "visual_scaled") {
      const ratio = (value) => Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : "未提供";
      facts.push(["纸面宽 / 高比例（新版 ÷ 旧版）",
        `${ratio(graphics.scale_ratio?.x)} / ${ratio(graphics.scale_ratio?.y)}（非实物尺寸）`]);
    } else {
      facts.push(
        ["残差像素（旧 / 新）", `${metric(graphics.changed_pixels?.old)} / ${metric(graphics.changed_pixels?.new)}`],
        ["残差占比（旧 / 新）", `${fraction(graphics.residual_fraction?.old)} / ${fraction(graphics.residual_fraction?.new)}`]
      );
    }
    if (subview) {
      facts.push(
        ["跨位置对应", `旧子图 ${metric(subview.old_index)} → 新子图 ${metric(subview.new_index)}`],
        ["外观配对启发式得分（非准确率）", metric(subview.identity?.score)],
        ["优于次佳的得分差（旧 / 新）", `${metric(subview.identity?.old_margin)} / ${metric(subview.identity?.new_margin)}`],
        ["绘图尺寸变化", subview.drawing_size_changed === true ? "有候选，不等于实物尺寸变化" : subview.drawing_size_changed === false ? "未超过当前阈值" : "未提供"]
      );
    }
    if (alignment.method === "verified_uniform_scale" && Number.isFinite(alignment.scale_ratio)) {
      facts.push(["内容比较采用的等比绘图校正",
        `${(alignment.scale_ratio * 100).toFixed(2)}%（单一系数；剩余内容残差仍保留）`]);
    }
    if (alignment.inlier_count != null) facts.push(["配准内点数", metric(alignment.inlier_count)]);
    const list = el("dl", "graphics-metrics");
    for (const [label, value] of facts) {
      const row = el("div");
      row.append(el("dt", "", label), el("dd", "", value));
      list.append(row);
    }
    section.append(list, el("p", "graphics-disclaimer", optional
      ? "黄色虚框只标出视图范围，不标为内容变化红框。取消对应复选框即可隐藏，文字、尺寸标注和其他内容差异不受影响。"
      : "红色实框来自实际局部残差像素；红色虚框是对侧残差经已接受配准映射的对应位置，不是本侧实测变化。未提供可靠映射时仅导航上下文，不将整个区域边界冒充差异点。配准响应不是准确率。"));
    if (item.change === "visual_annotation") {
      section.append(el("p", "graphics-disclaimer",
        "本项是文字区域的像素残差，不代表完整文字比较；料号及表格内容请结合结构化字段中的旧值、新值和单元格证据。"));
    }
    if (Array.isArray(graphics.limitations) && graphics.limitations.length) {
      const limits = el("ul", "graphics-limitations");
      graphics.limitations.forEach((limit) => limits.append(el("li", "", limit)));
      section.append(el("h4", "", "方法限制"), limits);
    }
    return section;
  }
  function renderDetails(item, content = $("detail-content"), meta = $("detail-meta")) {
    content.replaceChildren();
    meta.textContent = item ? `${item.id} · ${changes[item.change] || item.change} · 匹配 ${text(item.match?.method) || "未提供"} / 确定性 ${text(item.match?.certainty) || "未提供"} / 启发式得分 ${text(item.match?.score) || "未提供"}（非准确率）` : "选择索引或证据框，联动定位两侧证据";
    if (!item) { content.append(el("p", "detail-placeholder", "保留原文 · 核对外观 · 不推测缺失证据")); return; }
    const visualModel = modelVisualItem(item);
    if (visualModel) {
      const comparison = item.model_comparison || {}, visual = item.visual_comparison || {};
      const unmeasured = visual.measurement_status === "unmeasured" ||
        visual.alignment?.accepted === false ||
        (visual.status === "unresolved" && visual.alignment?.accepted !== true && visual.measurement_status !== "measured");
      meta.textContent = `${item.id} · ${changes[item.change] || item.change} · 模型观察 + 本地PDF栅格核验（非OCR，不确认实体部件增删）`;
      const section = el("section", "model-detail model-visual-detail");
      section.append(el("h3", "", "图纸填充／轮廓变化候选"),
        el("p", "", "模型生成描述（非OCR）仅用于复核；定位依据是本地PDF栅格非文字残差，不是CU变化词框，不推断实体部件删除。"),
        el("p", "", modelVisualDifference(item)
          ? "已局部定位：红框仅表示实际测量的非文字残差；未变尺寸标注与搜索区域不高亮，仍需原图确认。"
          : "定位未解决：模型已发现疑点，但位置尚未核实；不表示没有变化，也不显示确认变更红框。"),
        el("p", "", `模型配对标签（非原文证据）：${text(comparison.pair_label) || "未提供"}`),
        el("p", "", `模型理由（非原文证据）：${text(comparison.rationale) || "未提供"}`),
        el("p", "visual-description", `模型观察（非OCR）：${text(visual.description) || "未提供"}`),
        el("p", "", `类型：${text(visual.kind) || "未提供"} · 状态：${text(visual.status) || "未提供"}`),
        el("p", "visual-measurement", unmeasured
          ? "残差像素：未测量（定位未通过或尚未执行），不是零变化。"
          : `实际残差像素（旧 / 新）：${visual.changed_pixels?.old ?? "未提供"} / ${visual.changed_pixels?.new ?? "未提供"}`),
        el("p", "visual-alignment-scope", visual.alignment?.scope === "subfeature_neighborhood"
          ? "定位方式：整体配准未通过，已独立核验子特征周边公共轮廓；不代表整幅视图一致。"
          : "定位方式：视图级核验；详细状态与未解决原因见下方。"),
        el("p", "review-reasons", `定位限制：${text(visual.limitations) || "未提供"}；来源 / 预算限制：${text(comparison.issues) || "未提供"}`));
      const provenance = el("details", "visual-provenance");
      provenance.append(el("summary", "", "本地定位依据、对齐与可追溯性（非CU词坐标）"),
        el("pre", "", text(visual)),
        el("p", "", "context_locations / model_context 仅供导航，不作为变化框；未观察到残差不代表已证明整个区域没有变化。"));
      section.append(provenance);
      content.append(section);
    }
    if (item.channel === "model" && !visualModel) {
      meta.textContent = `${item.id} · ${changes[item.change] || item.change} · 模型语义配对 + CU局部复读证据 / 对应关系仅为模型提议（不是独立确认的工程事实）`;
      const comparison = item.model_comparison || {}, section = el("section", "model-detail");
      section.append(el("h3", "", "模型引导复核 · 对应关系尚需人工确认"),
        el("p", "", modelDisclaimer),
        el("p", "", modelTextDifference(item)
          ? "两侧CU局部复读原文不同；原文保留完整短语作为上下文，红框仅指CU提取的变化词，不将整句或定位锚点标红，不确认语义对应或工程变更。"
          : "来源或对应关系不确定，或预算/定位限制下未完成局部复读；黄色虚框只供复核，不确认变更。"),
        el("p", "", `阶段：${comparison.stage === "fine" ? "局部复读" : "粗比对"} · 来源状态：${comparison.status === "source_grounded" ? "已提供来源（仍需核验）" : "仅供复核"}`),
        el("p", "", `模型配对标签（非原文证据）：${text(comparison.pair_label) || "未提供"}`),
        el("p", "", `模型理由（非原文证据）：${text(comparison.rationale) || "未提供"}`));
      if (item.change === "model_no_text_change") {
        section.append(el("p", "model-text-cleared",
          "局部复读未发现文字差异，默认不列入差异候选，也不画框；这不保证整个区域或条码编码一致。覆盖不足见下方限制。"));
      }
      if (Array.isArray(comparison.observations) && comparison.observations.length) {
        const labels = { text_change: "文字差异疑点", visual_change: "图形复核建议",
          unchanged_text: "仍存在的标注（不高亮）", unresolved: "尚未解决" };
        const observations = el("div", "model-observations");
        observations.append(el("h4", "", "逐项复核建议（模型观察，不是工程结论）"));
        const unchanged = el("details", "model-unchanged-context");
        unchanged.append(el("summary", "", "仍存在的标注 · 仅上下文，不高亮"));
        for (const observation of comparison.observations) {
          const entry = el("section", `model-observation ${observation.kind === "visual_change" ? "model-visual-observation" : ""}`);
          entry.append(el("h4", "", labels[observation.kind] || "复核建议"),
            el("p", "", observation.description),
            el("p", "", `建议核对：${text(observation.check) || "核对两侧原图"}`));
          if (observation.kind === "visual_change") entry.append(el("p", "source-meta",
            "图形观察不是OCR原文或已验证像素差异；仅用来源上下文导航，不把附近未变文字画成图形变化框。"));
          (observation.kind === "unchanged_text" ? unchanged : observations).append(entry);
        }
        if (unchanged.children.length > 1) observations.append(unchanged);
        section.append(observations);
      }
      if (Array.isArray(comparison.issues) && comparison.issues.length) {
        section.append(el("p", "review-reasons", `来源 / 预算限制：${comparison.issues.map(text).join("；")}`));
      }
      if (item.model_context) {
        const context = el("details", "model-context");
        context.append(el("summary", "", "模型上下文（仅导航，不是局部变化证据）"));
        for (const side of sides) {
          context.append(el("h4", "", sideName[side]), el("p", "source-text", item.model_context[side]?.raw_text ?? "未提供上下文"));
        }
        section.append(context);
      }
      content.append(section);
    }
    const graphical = item.channel === "graphics";
    if (graphical) content.append(renderGraphicsDetails(item));
    if (annotationReview(item)) {
      const annotation = item.annotation_comparison;
      content.append(el("p", "annotation-review",
        `BOM文字未变。该文字在BOM外的OCR出现次数：${annotation.counts_outside_bom.old} → ${annotation.counts_outside_bom.new}。黄色框定位实际提取到的图外标注；另一侧只导航BOM上下文。可能是标注增删，也可能是OCR遗漏，不确认删除标签或部件。`));
    }
    if (tableRowChange(item)) {
      content.append(el("p", "review-reasons",
        `${changes[item.change]}：来自已建立对应、单元格网格完整的CU表格，不是把普通未配对字段当作增删。只在有该行提取证据的一侧标红，另一侧仅定位到对应表格。OCR仍可能遗漏，需按两侧原图确认。`));
    }
    if (item.channel === "tables") {
      const section = el("section", "table-detail");
      section.append(el("h3", "", "表格专项证据 · 与字段/OCR可能重叠"));
      section.append(el("p", "", item.change === "table_grid_changed"
        ? "比较实际绘制的表格网格。行数包含表头和空白行；网格行数变化不等于业务记录新增或删除。框表示网格证据，不是OCR文字变化。"
        : "按有来源的表头、单元格和对应表格比较；仅标记实际有证据的一侧。未提取到文字不能直接证明单元格为空，OCR内容仍需原图复核。"));
      const comparison = item.table_comparison || {};
      if (comparison.scope === "observed_panel_text") {
        section.append(el("p", "panel-line-counts",
          `面板正文 OCR 文字行：${comparison.old_ocr_line_count ?? "未提供"} → ${comparison.new_ocr_line_count ?? "未提供"}；表框行数（含标题）：${comparison.old_grid?.rows_including_header ?? "未提供"} → ${comparison.new_grid?.rows_including_header ?? "未提供"}。文字换行与表格网格行不同，仅换行不计内容变更。`));
        if (comparison.panel_text_complete === false) {
          section.append(el("p", "review-reasons",
            "红框仅对应已核验的局部文字片段，不代表整段文字已完整比较。其余文字或符号仍需人工复核。"));
        }
        const context = el("details", "panel-ocr-context");
        context.append(el("summary", "", "完整面板 OCR 上下文（可能有误读，不是全部已确认差异）"));
        for (const side of sides) {
          context.append(el("h4", "", sideName[side]),
            el("p", "source-text", (comparison[`${side}_ocr_lines`] || []).map((line) => line.raw_text).join("\n")));
        }
        section.append(context);
      }
      const details = el("details");
      details.append(el("summary", "", "表格对应与来源依据"), el("pre", "", text(item.table_comparison)));
      section.append(details);
      content.append(section);
    }
    if (item.cell_comparison) {
      const cells = item.cell_comparison, section = el("section", "cell-diff");
      section.append(el("h3", "", "实际变化的列（CU 单元格证据）"));
      if (cells.status === "complete") {
        const changed = cells.fields.filter((field) => ["modified", "relocated"].includes(field.change));
        const table = el("table"), header = el("tr");
        for (const label of ["列", "旧图", "新图"]) header.append(el("th", "", label));
        const head = el("thead"); head.append(header); table.append(head);
        const body = el("tbody");
        for (const field of changed) {
          const row = el("tr");
          row.dataset.field = field.key;
          row.append(el("th", "", field.label), el("td", "", field.old.raw_text),
            el("td", "", field.new.raw_text));
          body.append(row);
        }
        table.append(body); section.append(table);
        section.append(el("p", "", `${changed.length} 列变化候选；其余 ${cells.fields.length - changed.length} 列值相同，不画差异框。数量/单位合列与拆列按相同语义比较；单元格没有独立置信度，不代表已签核。`));
      } else {
        section.append(el("p", "", `未能可靠细化到单元格，不再整行标红：${cells.issues.join("；")}`));
      }
      content.append(section);
    }
    if (item.change === "formatting_only") {
      content.append(el("p", "review-reasons", "仅有效日期分隔符旁的空格，或表格列值一致的排版/数量单位格式不同；原文保留，不计工程变更。"));
    }
    if (unpaired(item)) {
      content.append(el("p", "review-reasons",
        graphical
          ? "图形区域尚未可靠配对，仅供人工查证。另一侧没有配对证据不等于图纸没有该内容，不能认定新增或删除。"
          : "此项不是已确认差异：自动配对未找到对应项，可能是 OCR 分段、漏识别或字段命名不同。另一侧没有配对证据不等于图纸没有该内容，不能认定新增或删除。"));
    }
    for (const side of sides) {
      const source = item[side], section = el("section", "source-detail");
      section.dataset.side = side;
      if (visualModel) {
        section.append(el("h3", "", `${sideName[side]} / PDF栅格来源 · 模型生成描述（非OCR）`),
          el("p", "source-text", source?.visual_description || item.visual_comparison?.[`${side}_description`] || "未提供模型描述（不代表原图没有该特征）"),
          el("p", "source-meta", `本地PDF栅格来源（非CU词证据）：${text(source?.source) || "未提供"}\n实际局部残差框 ${locations(item, side).length} 处；搜索区域仅供导航，不绘变化框。${source?.location_error ? `\n定位说明：${source.location_error}` : ""}`));
        content.append(section);
        continue;
      }
      section.append(el("h3", "", `${sideName[side]} / ${item.channel === "model" ? (modelTextDifference(item) ? "CU局部复读原文" : "复核来源原文（非确认变更）") : item.change === "table_grid_changed" ? "本地网格测量（非 OCR 原文）" : graphical ? "本地渲染证据描述（非 OCR 原文）" : item.cell_comparison ? "整行原文（上下文，非整行变更）" : "原始文本"}`));
      section.append(el("p", "source-text", source ? (source.raw_text ?? (graphical ? "未提供本地渲染描述" : "未提供原文")) : "未配对到证据（不代表原图没有）"));
      if (source) {
        if (Array.isArray(source.schema_sources) && source.schema_sources.length) {
          const originals = el("details");
          originals.append(el("summary", "", "CU 原始提取分组（标签 / 值）"),
            el("p", "source-text", source.schema_sources.map((entry) => entry.raw_text).join("\n")));
          section.append(originals);
        }
        if (graphical && !transformation(item)) {
          section.append(el("p", "source-meta",
            `实际残差框 ${locations(item, side).length} 处 · 对应定位框 ${counterpartLocations(item, side).length} 处（不计入残差像素或变化数量）${source.counterpart_location_error ? `；映射限制：${source.counterpart_location_error}` : ""}`));
        }
        section.append(el("p", graphical ? "source-meta graphics-provenance" : "source-meta", graphical
          ? `本地渲染来源：${text(source.source) || "未提供"}\n置信度：未提供概率置信度${source.location_error ? `\n定位说明：${source.location_error}` : ""}`
          : `来源：${text(source.source) || "未提供"} · 置信度：${text(source.confidence) || "未提供"}${source.location_error ? ` · 无法定位：${source.location_error}` : ""}`));
        if (source.detail != null && source.detail !== "") {
          const detail = el("details");
          detail.append(el("summary", "", graphical || item.channel === "tables" ? "本地检测说明（非 OCR 原文，非工程结论）" : "生成解释（不是原文，不单独作为变更依据）"), el("p", "", source.detail));
          section.append(detail);
        }
      }
      content.append(section);
    }
    if (item.review_required || item.change === "interpretation_only") {
      const reasons = (item.review_reasons || []).map(text).join("；");
      content.append(el("p", "review-reasons", item.change === "interpretation_only"
        ? `仅生成解释存在差异，不代表图纸发生变更。${reasons}`
        : `需要人工复核：${reasons || (graphical || visualModel ? "请核对两侧图形、配准可靠性及定位依据。" : "请核对两侧原文及位置。")}`));
    }
  }
  function coverageFacts(value) {
    const labels = {
      pages: "页数", page_counts: "页数统计", page_count: "页数", old_pages: "旧版页数", new_pages: "新版页数",
      regions: "区域", region_counts: "区域统计", region_count: "区域数", old_regions: "旧版区域数", new_regions: "新版区域数",
      paired: "已配对", paired_pages: "已配对页数", paired_regions: "已配对区域数",
      unpaired: "未配对（非确认增删）", unpaired_old: "旧侧未配对", unpaired_new: "新侧未配对",
      old: "旧版", new: "新版", total: "总计", analyzed: "已分析", skipped: "已跳过",
      warnings: "警告", limits: "运行限制", limitations: "方法限制", dpi: "渲染 DPI",
      max_pages: "页数上限", max_regions: "区域数上限", max_pixels: "像素上限",
      method: "方法", status: "状态", enabled: "已启用", candidates: "候选数",
      subview_matching: "子图跨位置配对", old_candidates: "旧侧子图候选", new_candidates: "新侧子图候选",
      matched: "唯一外观配对", unresolved: "未唯一配对（保留父区复核）",
      omitted: "超限未处理", resolved_residual_pairs: "独立对齐并替换父区残差的配对",
      comparison_policy: "比较策略", ignored_changes: "不作为差异的项目",
      transformations_included: "已提供可选平移/缩放提示", transformation_candidates: "可选视图提示数量",
      design_content_items: "设计内容候选数量",
      visual_moved: "平移提示", visual_scaled: "绘图缩放提示",
      coarse: "粗比对覆盖", fine: "局部复读覆盖", unprocessed: "未处理 / 预算及来源限制",
      usage: "用量（不是金额）", budget: "预算", max_pairs: "配对预算上限", max_crops: "裁切预算上限",
      crops: "裁切数", processed: "已处理", issues: "来源与限制",
      visual: "非文字视觉覆盖", label: "区域标签", features: "模型观察特征（非确认变更）",
      localized: "已局部定位特征", no_visual_change_observed: "未观察到视觉变化（不保证无遗漏）",
      reason: "未处理 / 延后原因", model: "直接视觉模型调用（含返回用量）",
      input_tokens: "输入 token", output_tokens: "输出 token", total_tokens: "该来源返回的 token 合计"
    };
    if (Array.isArray(value)) {
      const list = el("ul");
      if (!value.length) list.append(el("li", "", "无"));
      value.forEach((entry) => { const row = el("li"); row.append(coverageFacts(entry)); list.append(row); });
      return list;
    }
    if (value && typeof value === "object") {
      const list = el("dl", "coverage-facts");
      for (const [key, entry] of Object.entries(value)) {
        const row = el("div"), detail = el("dd");
        detail.append(coverageFacts(entry));
        row.append(el("dt", "", labels[key] || key), detail);
        list.append(row);
      }
      return list;
    }
    const descriptions = {
      design_content_only: "仅设计内容", view_translation: "视图平移",
      view_order: "视图顺序", uniform_drawing_scale: "等比绘图缩放",
      completed: "已完成（不保证无遗漏）", completed_with_limits: "已完成，仍有预算或来源限制",
      source_grounded: "已提供来源（仍需核验）", review_only: "仅供复核",
      reviewed: "已执行视觉复核（非全部变化已确认）", unprocessed: "未处理 / 延后（不代表无变化）"
    };
    return el("span", "", value == null ? "未提供" : typeof value === "boolean" ? (value ? "是" : "否") : Object.hasOwn(descriptions, value) ? descriptions[value] : value);
  }
  function renderModelCoverage(coverage) {
    const model = el("section", "model-coverage"), { visual, ...other } = coverage;
    model.append(el("h4", "", "模型引导覆盖、预算与来源限制"), coverageFacts(other));
    if (visual != null) {
      const section = el("section", "model-visual-coverage");
      section.append(el("h4", "", "非文字视觉覆盖 · 独立预算与延后项"),
        el("p", "", `视觉复核启用：${visual.enabled === true ? "是" : visual.enabled === false ? "否" : "未提供"} · 视觉区域预算上限：${visual.max_regions ?? "未提供"}`),
        el("p", "", "视觉区域预算与CU文字局部复读裁切数独立；逐区域列示模型观察特征、局部定位及未处理 / 延后原因。未处理、零特征或未观察到视觉变化均不证明无变化。"),
        coverageFacts(visual),
        el("p", "", "用量按来源列示，包含各区域直接视觉模型调用返回的 token；不计算跨通道总用量或费用，缺失用量不按零计。"));
      model.append(section);
    }
    model.append(el("p", "", modelLimits),
      el("p", "", "模型条目可能与字段、表格或图形候选重叠，不相加为独立变更数；没有候选不等于没有变更。"));
    return model;
  }
  function renderCoverage(result) {
    const content = $("coverage-content");
    content.replaceChildren(el("p", "", state.graphicsEnabled
      ? "对比仅覆盖成功提取的字段、OCR 原文、可定位的表格及已处理的本地图形区域；未识别、未配对或无法定位不等于无变更。证据框不是工程结论。"
      : `${graphicsUnavailable}对比仅覆盖成功提取的字段、OCR 原文与可定位的表格，不包含图形残差检测。未识别、未配对或无法定位不等于无变更。`));
    content.append(el("p", "", `覆盖信息：\n${text(result.coverage) || "未提供覆盖统计"}`));
    if (result.model_coverage != null) {
      content.append(renderModelCoverage(result.model_coverage));
    } else if (state.modelEnabled) {
      content.append(el("p", "", "本轮未返回模型覆盖统计，不能认定模型已完成全部比较或没有遗漏。"));
    }
    if (result.table_coverage != null) {
      const tables = el("section", "table-coverage");
      tables.append(el("h4", "", "表格专项覆盖与限制"), coverageFacts(result.table_coverage),
        el("p", "", "该通道补充可定位的非BOM表格，不代表所有表格均已识别；与字段/OCR可能重复，不相加为独立变更数。"));
      content.append(tables);
    }
    if (state.graphicsEnabled && result.graphics_coverage != null) {
      const graphics = el("section", "graphics-coverage");
      graphics.append(el("h4", "", "本地图形覆盖与限制"), coverageFacts(result.graphics_coverage),
        el("p", "", "局部像素差异仅提示外观候选；配准失败、回退页面或跳过区域必须人工复核。"));
      const raw = el("details");
      raw.append(el("summary", "", "完整图形覆盖统计（JSON）"), el("pre", "", text(result.graphics_coverage)));
      graphics.append(raw);
      content.append(graphics);
    } else if (state.graphicsEnabled) {
      content.append(el("p", "", "本轮未返回图形覆盖统计，不能据此认定图形无变化。"));
    }
    if (result.warnings) content.append(el("p", "", `限制与警告：\n${Array.isArray(result.warnings) ? result.warnings.map(text).join("\n") || "无" : text(result.warnings)}`));
    for (const side of sides) {
      const meta = result.metadata?.[side];
      if (meta) content.append(el("p", "", `${sideName[side]}：${meta.cache_hit ? "缓存命中" : "本轮提取"}\n上传原件 SHA256：${result.documents?.[side]?.sha256 || "未提供"}\nCU 分析文件 SHA256：${meta.document_sha256 || "未提供"}\n坐标依据：${meta.coordinate_basis || "未提供"}\n${meta.cache_hit ? "历史分析用量（非本次新增计费）" : "分析用量"}：${text(meta.usage) || "未提供"}`));
    }
  }
  function exportDetailBlocks(root) {
    const blocks = [];
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        if (node.textContent.trim()) blocks.push({ kind: "paragraph", text: node.textContent });
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      if (node.tagName === "TABLE") {
        blocks.push({ kind: "table", rows: Array.from(node.rows, (row) => Array.from(row.cells, (cell) => cell.textContent)) });
      } else if (node.tagName === "DL") {
        blocks.push({ kind: "table", rows: Array.from(node.children, (row) => [
          row.querySelector("dt")?.textContent || "", row.querySelector("dd")?.textContent || ""
        ]) });
      } else if (["H1", "H2", "H3", "H4", "SUMMARY", "P", "PRE", "LI"].includes(node.tagName)) {
        blocks.push({ kind: ["H1", "H2", "H3", "H4", "SUMMARY"].includes(node.tagName) ? "heading"
          : node.tagName === "PRE" ? "code" : "paragraph", text: node.textContent });
      } else {
        node.childNodes.forEach(walk);
      }
    };
    root.childNodes.forEach(walk);
    return blocks;
  }
  function exportSnapshot() {
    const itemsById = new Map(visibleItems().map((item) => [item.id, item]));
    const items = Array.from(document.querySelectorAll("#results-list .result-item"), (row) => {
      const item = itemsById.get(row.dataset.id);
      if (!item) throw new Error("显示结果已变化，请重新导出。");
      const content = el("div"), meta = el("span");
      renderDetails(item, content, meta);
      const blocks = exportDetailBlocks(content);
      const rows = [["侧别", "页码", "定位类型（归一化0–1，非工程尺寸）", "x", "y", "宽", "高"]];
      for (const side of sides) {
        const observed = locations(item, side), projected = counterpartLocations(item, side);
        for (const [kind, positions] of [["显示证据框", observed], ["对侧映射位置，非本侧残差", projected]]) {
          for (const loc of positions) {
            const box = bounds(loc);
            rows.push([sideName[side], String(loc.page), kind,
              ...["x", "y", "width", "height"].map((key) => box[key].toFixed(6))]);
          }
        }
        if (!observed.length && !projected.length) {
          const pages = [...new Set(navigationLocations(item, side).map((loc) => loc.page))];
          blocks.push({ kind: "paragraph", text: `${sideName[side]}没有可显示的证据框；${pages.length
            ? `仅有第 ${pages.join("、")} 页导航上下文，不作为变化框。` : "没有可靠定位，不推测缺失位置。"}` });
        }
      }
      if (rows.length > 1) blocks.push({ kind: "heading", text: "来源页与定位（其它页位置不在当前整页总览中绘制）" }, { kind: "table", rows });
      return { id: item.id, title: item.key || item.region || "未命名条目", meta: meta.textContent, blocks };
    });
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("浏览器无法读取标记颜色，未生成不完整的导出。");
    const rgba = (value) => {
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = "rgba(0,0,0,0)";
      if (value !== "none") context.fillStyle = value;
      context.fillRect(0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data, (v) => v / 255);
    };
    const panes = Object.fromEntries(sides.map((side) => {
      const stage = $(`${side}-stage`), svg = stage.querySelector("svg"), image = stage.querySelector("img").getBoundingClientRect();
      const rects = Array.from(svg.querySelectorAll("rect.evidence-box"), (node) => {
        const style = getComputedStyle(node), stroke = rgba(style.stroke), fill = rgba(style.fill);
        return {
          id: node.dataset.id,
          ...Object.fromEntries(["x", "y", "width", "height"].map((key) => [key, Number(node.getAttribute(key))])),
          kind: node.classList.contains("counterpart-evidence") ? "counterpart" : node.classList.contains("review-evidence") ? "review" : "change",
          stroke: { color: stroke.slice(0, 3), opacity: stroke[3] * Number(style.opacity) * Number(style.strokeOpacity),
            width: parseFloat(style.strokeWidth), dash: style.strokeDasharray === "none" ? [] : style.strokeDasharray.split(/[,\s]+/).filter(Boolean).map(parseFloat) },
          fill: { color: fill.slice(0, 3), opacity: fill[3] * Number(style.opacity) * Number(style.fillOpacity) }
        };
      });
      const labels = Array.from(svg.querySelectorAll("text.evidence-label"), (node) => ({
        id: node.textContent, x: Number(node.getAttribute("x")), y: Number(node.getAttribute("y")),
        font_size: Number(node.getAttribute("font-size")), color: rgba(getComputedStyle(node).fill).slice(0, 3)
      }));
      return [side, { document_id: state[side].document.id, page: state[side].page,
        width: image.width, height: image.height, rects, labels }];
    }));
    return { revision: state.revision, job_id: state.jobId, panes, items, filters: {
      channel: $("channel-filter").selectedOptions[0].textContent, review: $("review-filter").selectedOptions[0].textContent,
      interpretation: $("show-interpretation").checked, translation: $("show-translation").checked, scaling: $("show-scaling").checked
    } };
  }
  async function exportPdf() {
    if ($("export-button").disabled) return;
    clearError();
    const generation = state.generation, controller = new AbortController();
    state.exporting = true;
    state.exportController = controller;
    updateControls();
    try {
      const body = JSON.stringify(exportSnapshot());
      if (new TextEncoder().encode(body).length > 8 * 1024 * 1024) {
        throw new Error("当前导出详情超过8MB，请缩小筛选范围后重试；没有截断内容。");
      }
      const blob = await request("/api/export/pdf", {
        method: "POST", headers: { "Content-Type": "application/json" }, body,
        responseType: "pdf", signal: controller.signal
      });
      if (generation !== state.generation || controller.signal.aborted) return;
      const url = URL.createObjectURL(blob), link = el("a");
      link.href = url;
      link.download = `drawing-comparison-${new Date().toISOString().replace(/[:.]/g, "-")}.pdf`;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (err) {
      if (generation === state.generation && err.name !== "AbortError") error(`PDF导出失败：${err.message}`);
    } finally {
      if (state.exportController === controller) {
        state.exporting = false;
        state.exportController = null;
        updateControls();
      }
    }
  }
  function wait(ms, signal) {
    return new Promise((resolve, reject) => {
      const abort = () => { clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); };
      const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, ms);
      if (signal.aborted) abort();
      else signal.addEventListener("abort", abort, { once: true });
    });
  }
  async function compare() {
    if ($("compare-button").disabled) return;
    clearError();
    invalidateResults();
    const generation = state.generation, revision = state.revision;
    const controller = new AbortController();
    state.jobController = controller;
    state.comparing = true;
    updateControls();
    status("正在提交对比任务…");
    try {
      const job = await request("/api/compare", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision }), signal: controller.signal
      });
      if (generation !== state.generation) return;
      while (generation === state.generation) {
        const data = await request(`/api/jobs/${encodeURIComponent(job.job_id)}`, { signal: controller.signal });
        if (generation !== state.generation) return;
        renderUsage(data.usage_cost ?? data.result?.usage_cost ?? null);
        if (data.status === "stale") { status("文件版本已变化，本次结果已作废。请重新上传或刷新后再对比。"); return; }
        if (data.status === "failed") throw new Error(data.error || "对比失败，请重试。");
        if (data.status === "succeeded") {
          if (!data.result || !Array.isArray(data.result.items)) throw new Error("返回的对比结果不完整，请重试。");
          if (!sides.every((side) => data.result.documents?.[side]?.id === state[side].document?.id &&
              data.result.documents[side].sha256 === state[side].document?.sha256)) {
            throw new Error("结果文件与当前上传文件不一致，已拒绝显示旧结果或证据框，请重新对比。");
          }
          const result = {
            ...data.result,
            items: data.result.items.filter((item) => state.graphicsEnabled || item.channel !== "graphics"),
            graphics_coverage: state.graphicsEnabled ? data.result.graphics_coverage : undefined
          };
          if (result.graphics_coverage?.subview_matching) {
            const { moved, order_reversal_pairs, ...contentMatching } = result.graphics_coverage.subview_matching;
            result.graphics_coverage = { ...result.graphics_coverage, subview_matching: contentMatching };
          }
          state.result = result;
          state.jobId = job.job_id;
          renderResults();
          renderCoverage(result);
          sides.forEach((side) => { renderBoxes(side); evidenceNote(side); });
          return;
        }
        if (!["queued", "running"].includes(data.status)) throw new Error("任务状态异常，请重试。");
        status(`${data.status === "queued" ? "任务排队中" : "正在分析"}${data.phase ? ` · ${data.phase}` : ""}`);
        await wait(1200, controller.signal);
      }
    } catch (err) {
      if (generation !== state.generation || err.name === "AbortError") return;
      error(err.message);
      status("对比未完成。可点击「开始对比」重试。");
    } finally {
      if (generation === state.generation) {
        state.comparing = false;
        state.jobController = null;
        updateControls();
      }
    }
  }
  for (const side of sides) {
    const input = $(`${side}-upload`), viewport = $(`${side}-viewport`);
    input.addEventListener("change", () => { const file = input.files[0]; input.value = ""; if (file) mutate(side, file); });
    for (const name of ["choose", "replace"]) $(`${side}-${name}`).addEventListener("click", () => input.click());
    $(`${side}-remove`).addEventListener("click", () => mutate(side, null));
    $(`${side}-page`).addEventListener("change", (event) => { state[side].page = Number(event.target.value); showPage(side); });
    $(`${side}-zoom`).addEventListener("change", (event) => { state[side].zoom = event.target.value; resizeStage(side); });
    $(`${side}-fit`).addEventListener("click", () => { state[side].zoom = "fit"; $(`${side}-zoom`).value = "fit"; resizeStage(side); });
    viewport.addEventListener("dragover", (event) => { event.preventDefault(); if (state.ready) viewport.classList.add("drag-active"); });
    viewport.addEventListener("dragleave", (event) => { if (!viewport.contains(event.relatedTarget)) viewport.classList.remove("drag-active"); });
    viewport.addEventListener("drop", (event) => {
      event.preventDefault();
      viewport.classList.remove("drag-active");
      if (!state.ready) return;
      if (event.dataTransfer.files.length !== 1) { error("每侧请选择一份 PDF 文件。"); return; }
      mutate(side, event.dataTransfer.files[0]);
    });
  }
  for (const name of ["channel-filter", "review-filter", "show-interpretation", "show-translation", "show-scaling"]) $(name).addEventListener("change", () => {
    if (!visibleItems().some((item) => item.id === state.selected)) { state.selected = null; renderDetails(null); }
    renderResults();
    sides.forEach((side) => { renderBoxes(side); evidenceNote(side); });
  });
  $("compare-button").addEventListener("click", compare);
  $("export-button").addEventListener("click", exportPdf);
  $("dismiss-error").addEventListener("click", clearError);
  $("retry-bootstrap").addEventListener("click", bootstrap);
  const observer = new ResizeObserver(() => sides.forEach(resizeStage));
  sides.forEach((side) => observer.observe($(`${side}-viewport`)));
  window.addEventListener("beforeunload", () => { state.jobController?.abort(); state.exportController?.abort(); });
  bootstrap();
})();
