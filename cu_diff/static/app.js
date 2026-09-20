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
    visual_uncertain: "图形对应不确定（待复核）"
  };
  const channels = { schema: "结构化字段", graphics: "本地图形候选", ocr: "OCR 原文", unchanged: "一致项复核" };
  const state = {
    ready: false, csrf: "", revision: 0, azure: false, graphicsEnabled: false, generation: 0,
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
  function identicalFiles() {
    return Boolean(state.old.document?.sha256) &&
      state.old.document.sha256 === state.new.document?.sha256;
  }
  const graphicsUnavailable = "图形检测未接入/未启用：当前仅支持字段与 OCR 证据。";
  function updateGraphicsAvailability() {
    const menu = $("channel-filter");
    menu.querySelector('[value="primary"]').textContent = state.graphicsEnabled ? "字段 + 图形候选" : "结构化字段（默认）";
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
      : "默认显示结构化字段，不重复展示 OCR。红色实框为原文差异候选；黄色虚框仅供复核，不是确认变更。";
    $("coverage-content").textContent = `尚未运行对比。${state.graphicsEnabled ? "图形覆盖以本轮服务返回的统计为准。" : graphicsUnavailable}系统不会将缺失位置的证据推测成红框。`;
  }
  function updateControls() {
    const busy = sides.some((side) => state[side].pending);
    $("compare-button").disabled = !state.ready || busy || state.comparing ||
      !sides.every((side) => state[side].document) || identicalFiles();
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
    state.jobController = null;
    state.comparing = false;
    state.result = null;
    state.selected = null;
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
      updateGraphicsAvailability();
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
    const counterparts = counterpartLocations(item, side);
    if (counterparts.length) return counterparts;
    // Context is only a navigation fallback for a paired graphical region, never a change box.
    if (evidence.length || item?.channel !== "graphics" || !item.old || !item.new) return evidence;
    return validLocations(item[side].context_locations, side);
  }
  function uncertain(item) {
    const certainty = item.match?.certainty;
    return (typeof certainty === "number" && certainty < 0.8) ||
      ["low", "uncertain", "ambiguous", "低", "低确定性"].includes(String(certainty).toLowerCase());
  }
  function pairedDifference(item) {
    return Boolean(item.old && item.new) &&
      ["modified", "relocated", "visual_modified", "visual_annotation"].includes(item.change);
  }
  function transformation(item) {
    return ["visual_moved", "visual_scaled"].includes(item.change);
  }
  function evidenceStyle(item) {
    return pairedDifference(item) ? "" : " review-evidence";
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
        if (!["schema", "graphics"].includes(item.channel)) return false;
      } else if (channel !== "all" && item.channel !== channel) return false;
      switch ($("review-filter").value) {
        case "paired": return pairedDifference(item) || Boolean(item.old && item.new && transformation(item));
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
      status(`对比完成 · ${result.items.filter(pairedDifference).length} 条已配对差异候选 · ${unresolved} 条未配对待复核${graphicsUncertain ? ` · ${graphicsUncertain} 条图形对应不确定，见人工复核` : ""} · ${formatting} 条仅格式差异${hidden ? ` · ${hidden} 个仅解释差异默认隐藏` : ""} · 非已确认变更数${state.graphicsEnabled ? optional ? ` · 另显示 ${optional} 条平移/缩放提示（不计内容变更）` : " · 仅设计内容" : " · 图形检测未接入/未启用"}`);
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
          label.textContent = `${item.id}${counterpart ? " 对应" : transformation(item) ? " 视图范围" : pairedDifference(item) ? "" : " 待核"}`;
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
    if (!source) node.textContent = `${item.id} · 未配对到证据，不代表本侧图纸没有该内容`;
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
    else node.textContent = `${item.id} · 第 ${state[side].page} 页${transformation(item) ? "对应视图范围（平移/缩放提示，非内容残差）" : item.channel === "graphics" ? "局部像素残差（候选）" : "证据"}${located.length > 1 ? ` · 共 ${located.length} 处，可切换页码查看` : ""}`;
  }
  function selectItem(id) {
    const item = state.result?.items.find((i) => i.id === id);
    if (!item) return;
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
    if (Array.isArray(graphics.limitations) && graphics.limitations.length) {
      const limits = el("ul", "graphics-limitations");
      graphics.limitations.forEach((limit) => limits.append(el("li", "", limit)));
      section.append(el("h4", "", "方法限制"), limits);
    }
    return section;
  }
  function renderDetails(item) {
    const content = $("detail-content");
    content.replaceChildren();
    $("detail-meta").textContent = item ? `${item.id} · ${changes[item.change] || item.change} · 匹配 ${text(item.match?.method) || "未提供"} / 确定性 ${text(item.match?.certainty) || "未提供"} / 启发式得分 ${text(item.match?.score) || "未提供"}（非准确率）` : "选择索引或证据框，联动定位两侧证据";
    if (!item) { content.append(el("p", "detail-placeholder", "保留原文 · 核对外观 · 不推测缺失证据")); return; }
    const graphical = item.channel === "graphics";
    if (graphical) content.append(renderGraphicsDetails(item));
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
      section.append(el("h3", "", `${sideName[side]} / ${graphical ? "本地渲染证据描述（非 OCR 原文）" : item.cell_comparison ? "整行原文（上下文，非整行变更）" : "原始文本"}`));
      section.append(el("p", "source-text", source ? (source.raw_text ?? (graphical ? "未提供本地渲染描述" : "未提供原文")) : "未配对到证据（不代表原图没有）"));
      if (source) {
        if (graphical && !transformation(item)) {
          section.append(el("p", "source-meta",
            `实际残差框 ${locations(item, side).length} 处 · 对应定位框 ${counterpartLocations(item, side).length} 处（不计入残差像素或变化数量）${source.counterpart_location_error ? `；映射限制：${source.counterpart_location_error}` : ""}`));
        }
        section.append(el("p", graphical ? "source-meta graphics-provenance" : "source-meta", graphical
          ? `本地渲染来源：${text(source.source) || "未提供"}\n置信度：未提供概率置信度${source.location_error ? `\n定位说明：${source.location_error}` : ""}`
          : `来源：${text(source.source) || "未提供"} · 置信度：${text(source.confidence) || "未提供"}${source.location_error ? ` · 无法定位：${source.location_error}` : ""}`));
        if (source.detail != null && source.detail !== "") {
          const detail = el("details");
          detail.append(el("summary", "", graphical ? "本地检测说明（非 OCR 原文，非工程结论）" : "生成解释（不是原文，不单独作为变更依据）"), el("p", "", source.detail));
          section.append(detail);
        }
      }
      content.append(section);
    }
    if (item.review_required || item.change === "interpretation_only") {
      const reasons = (item.review_reasons || []).map(text).join("；");
      content.append(el("p", "review-reasons", item.change === "interpretation_only"
        ? `仅生成解释存在差异，不代表图纸发生变更。${reasons}`
        : `需要人工复核：${reasons || (graphical ? "请核对两侧图形、配准可靠性及定位依据。" : "请核对两侧原文及位置。")}`));
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
      visual_moved: "平移提示", visual_scaled: "绘图缩放提示"
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
      view_order: "视图顺序", uniform_drawing_scale: "等比绘图缩放"
    };
    return el("span", "", value == null ? "未提供" : typeof value === "boolean" ? (value ? "是" : "否") : Object.hasOwn(descriptions, value) ? descriptions[value] : value);
  }
  function renderCoverage(result) {
    const content = $("coverage-content");
    content.replaceChildren(el("p", "", state.graphicsEnabled
      ? "对比仅覆盖成功提取的字段、OCR 原文及已处理的本地图形区域；未识别、未配对或无法定位不等于无变更。证据框不是工程结论。"
      : `${graphicsUnavailable}对比仅覆盖成功提取的字段与 OCR 原文，不包含图形检测。未识别、未配对或无法定位不等于无变更。`));
    content.append(el("p", "", `覆盖信息：\n${text(result.coverage) || "未提供覆盖统计"}`));
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
  $("dismiss-error").addEventListener("click", clearError);
  $("retry-bootstrap").addEventListener("click", bootstrap);
  const observer = new ResizeObserver(() => sides.forEach(resizeStage));
  sides.forEach((side) => observer.observe($(`${side}-viewport`)));
  window.addEventListener("beforeunload", () => state.jobController?.abort());
  bootstrap();
})();
