(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const sides = ["old", "new"];
  const sideName = { old: "旧版", new: "新版" };
  const changes = {
    modified: "提取原文不同", relocated: "行号重排", interpretation_only: "仅解释差异",
    unpaired_old: "旧侧未配对", unpaired_new: "新侧未配对", unchanged: "提取原文相同"
  };
  const channels = { schema: "结构化字段", ocr: "OCR 原文", unchanged: "一致项复核" };
  const state = {
    ready: false, csrf: "", revision: 0, azure: false, generation: 0,
    limits: { max_bytes: 20971520, max_pages: 20 }, result: null, selected: null,
    comparing: false, jobController: null, mutationQueue: Promise.resolve(),
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
    const headers = new Headers(options.headers);
    if (options.method && options.method !== "GET") headers.set("X-CSRF-Token", state.csrf);
    let response;
    try {
      response = await fetch(url, { ...options, headers, credentials: "same-origin", cache: "no-store" });
    } catch (err) {
      if (err.name === "AbortError") throw err;
      throw new Error("连接中断，请检查网络后重试。");
    }
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("服务器响应异常，请稍后重试。"); }
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}），请重试。`);
    return data;
  }
  function updateControls() {
    const busy = sides.some((side) => state[side].pending);
    $("compare-button").disabled = !state.ready || busy || state.comparing ||
      !sides.every((side) => state[side].document);
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
    state.jobController = null;
    state.comparing = false;
    state.result = null;
    state.selected = null;
    sides.forEach((side) => $(`${side}-stage`).querySelector("svg").replaceChildren());
    renderResults();
    renderDetails(null);
    $("coverage-content").textContent = "文件已更改，旧结果已清除。重新对比后显示覆盖范围。";
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
      state.ready = true;
      $("connection-status").textContent = "会话已连接";
      $("model-tag").textContent = `${data.model || "模型"} / ${state.azure ? "缓存优先 · 可提交 CU" : "只读缓存模式"}`;
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
            status(sides.every((s) => state[s].document) ? "两份图纸已就绪，请开始对比。" : "等待上传有效的旧版和新版图纸。");
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
    };
    image.onerror = () => {
      if (token !== s.imageEpoch) return;
      s.loaded = false;
      stage.hidden = true;
      $(`${side}-evidence-note`).replaceChildren(el("span", "", "页面预览加载失败。 "), retryButton(() => showPage(side, scrollSelection)));
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
  function locations(item, side) {
    const pages = state[side].document?.pages || [];
    return (item?.[side]?.locations || []).filter((loc) =>
      pages.some((page) => page.number === loc.page) && bounds(loc));
  }
  function uncertain(item) {
    const certainty = item.match?.certainty;
    return (typeof certainty === "number" && certainty < 0.8) ||
      ["low", "uncertain", "ambiguous", "低", "低确定性"].includes(String(certainty).toLowerCase());
  }
  function visibleItems() {
    return (state.result?.items || []).filter((item) => {
      if (!$("show-interpretation").checked && item.change === "interpretation_only") return false;
      if ($("channel-filter").value !== "all" && item.channel !== $("channel-filter").value) return false;
      switch ($("review-filter").value) {
        case "review": return Boolean(item.review_required);
        case "uncertain": return uncertain(item);
        case "unpaired": return item.change === "unpaired_old" || item.change === "unpaired_new";
        default: return true;
      }
    });
  }
  function renderResults() {
    const list = $("results-list");
    list.replaceChildren();
    const items = visibleItems();
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
    for (const channel of groups) {
      const grouped = items.filter((item) => item.channel === channel);
      list.append(el("h3", "channel-heading", `${channels[channel] || channel} / ${grouped.length}`));
      for (const item of grouped) {
        const row = el("div", "result-item");
        row.dataset.id = item.id;
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
      for (const loc of locations(item, side).filter((l) => l.page === s.page)) {
        const box = bounds(loc);
        const rect = svgNode("rect", {
          x: box.x * 1000, y: box.y * 1000, width: box.width * 1000, height: box.height * 1000,
          class: `evidence-box${item.id === state.selected ? " selected" : ""}`,
          "data-id": item.id, "vector-effect": "non-scaling-stroke", tabindex: 0, role: "button",
          "aria-label": `${item.id} ${item.key || item.region || ""}，${sideName[side]}第 ${s.page} 页`
        });
        rect.addEventListener("click", () => selectItem(item.id));
        rect.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectItem(item.id); }
        });
        svg.append(rect);
        if (!labelled && (!state.selected || item.id === state.selected)) {
          const label = svgNode("text", {
            x: Math.min(box.x * 1000 + 2, 940), y: Math.max(18, box.y * 1000 - 5),
            class: "evidence-label", "font-size": Math.max(10, 11 * 1000 / (stage.clientHeight || 1000)),
            "aria-hidden": "true"
          });
          label.textContent = item.id;
          svg.append(label);
          labelled = true;
        }
      }
    }
  }
  function evidenceNote(side) {
    const item = state.result?.items.find((i) => i.id === state.selected);
    const node = $(`${side}-evidence-note`);
    if (!item) { node.textContent = state.result ? "点击红框或差异索引，查看对应原文。" : "预览已就绪，等待开始对比。"; return; }
    const source = item[side], located = locations(item, side);
    if (!source) node.textContent = `${item.id} · 无对应证据`;
    else if (!located.length) node.textContent = `${item.id} · 无法定位${source.location_error ? `：${source.location_error}` : "；保留原文，不绘制推测框"}`;
    else if (!located.some((loc) => loc.page === state[side].page)) node.textContent = `${item.id} · 本页无对应证据；证据位于第 ${[...new Set(located.map((loc) => loc.page))].join("、")} 页`;
    else node.textContent = `${item.id} · 第 ${state[side].page} 页证据${located.length > 1 ? ` · 共 ${located.length} 处，可切换页码查看` : ""}`;
  }
  function selectItem(id) {
    const item = state.result?.items.find((i) => i.id === id);
    if (!item) return;
    state.selected = id;
    document.querySelectorAll(".result-button").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.id === id)));
    renderDetails(item);
    for (const side of sides) {
      const first = locations(item, side)[0];
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
    const loc = locations(item, side).find((l) => l.page === state[side].page);
    if (!loc || !state[side].loaded) return;
    const box = bounds(loc), viewport = $(`${side}-viewport`), stage = $(`${side}-stage`);
    const stageRect = stage.getBoundingClientRect(), paneRect = viewport.getBoundingClientRect();
    viewport.scrollTo({
      left: viewport.scrollLeft + stageRect.left - paneRect.left + (box.x + box.width / 2) * stageRect.width - viewport.clientWidth / 2,
      top: viewport.scrollTop + stageRect.top - paneRect.top + (box.y + box.height / 2) * stageRect.height - viewport.clientHeight / 2,
      behavior: "auto"
    });
  }
  function renderDetails(item) {
    const content = $("detail-content");
    content.replaceChildren();
    $("detail-meta").textContent = item ? `${item.id} · ${changes[item.change] || item.change} · 匹配 ${text(item.match?.method) || "未提供"} / 确定性 ${text(item.match?.certainty) || "未提供"} / 启发式得分 ${text(item.match?.score) || "未提供"}（非准确率）` : "选择索引或红框，联动定位两侧原文";
    if (!item) { content.append(el("p", "detail-placeholder", "保留原文 · 分离解释 · 不推测缺失证据")); return; }
    for (const side of sides) {
      const source = item[side], section = el("section", "source-detail");
      section.append(el("h3", "", `${sideName[side]} / 原始文本`));
      section.append(el("p", "source-text", source ? (source.raw_text ?? "未提供原文") : "无对应证据"));
      if (source) {
        section.append(el("p", "source-meta", `来源：${text(source.source) || "未提供"} · 置信度：${text(source.confidence) || "未提供"}${source.location_error ? ` · 无法定位：${source.location_error}` : ""}`));
        if (source.detail != null && source.detail !== "") {
          const detail = el("details");
          detail.append(el("summary", "", "生成解释（不是原文，不单独作为变更依据）"), el("p", "", source.detail));
          section.append(detail);
        }
      }
      content.append(section);
    }
    if (item.review_required || item.change === "interpretation_only") {
      const reasons = (item.review_reasons || []).map(text).join("；");
      content.append(el("p", "review-reasons", item.change === "interpretation_only"
        ? `仅生成解释存在差异，不代表图纸发生变更。${reasons}`
        : `需要人工复核：${reasons || "请核对两侧原文及位置。"}`));
    }
  }
  function renderCoverage(result) {
    const content = $("coverage-content");
    content.replaceChildren(el("p", "", "对比仅覆盖成功提取的字段与 OCR 原文；未识别、未配对或无法定位不等于无变更。红框为证据边界，不是工程结论。"));
    content.append(el("p", "", `覆盖信息：\n${text(result.coverage) || "未提供覆盖统计"}`));
    if (result.warnings) content.append(el("p", "", `限制与警告：\n${Array.isArray(result.warnings) ? result.warnings.map(text).join("\n") || "无" : text(result.warnings)}`));
    for (const side of sides) {
      const meta = result.metadata?.[side];
      if (meta) content.append(el("p", "", `${sideName[side]}：${meta.cache_hit ? "缓存命中" : "本轮提取"}\n${meta.cache_hit ? "历史分析用量（非本次新增计费）" : "分析用量"}：${text(meta.usage) || "未提供"}`));
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
        if (data.status === "stale") { status("文件版本已变化，本次结果已作废。请重新上传或刷新后再对比。"); return; }
        if (data.status === "failed") throw new Error(data.error || "对比失败，请重试。");
        if (data.status === "succeeded") {
          if (!data.result || !Array.isArray(data.result.items)) throw new Error("返回的对比结果不完整，请重试。");
          state.result = data.result;
          renderResults();
          renderCoverage(data.result);
          sides.forEach((side) => { renderBoxes(side); evidenceNote(side); });
          const hidden = data.result.items.filter((item) => item.change === "interpretation_only").length;
          status(`对比完成 · ${data.result.items.length} 条证据候选${hidden ? ` · ${hidden} 个仅解释差异默认隐藏` : ""} · 不是已确认变更数量`);
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
  for (const name of ["channel-filter", "review-filter", "show-interpretation"]) $(name).addEventListener("change", () => {
    if (!visibleItems().some((item) => item.id === state.selected)) { state.selected = null; renderDetails(null); }
    renderResults();
    sides.forEach((side) => { renderBoxes(side); evidenceNote(side); });
  });
  $("compare-button").addEventListener("click", compare);
  $("dismiss-error").addEventListener("click", clearError);
  $("retry-bootstrap").addEventListener("click", bootstrap);
  const observer = new ResizeObserver(() => sides.forEach(resizeStage));
  sides.forEach((side) => observer.observe($(`${side}-viewport`)));
  window.addEventListener("beforeunload", () => state.jobController?.abort());
  bootstrap();
})();
