"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 5000;
  const STALE_MS = 20000;
  const MODE_LABEL = { failover: "主备切换", weighted: "按权重轮换", round_robin: "多 A 记录轮询" };
  const DECISION_LABEL = { smart: "TCP 优先", api_only: "仅国内 ICMP", tcp_only: "仅服务器 TCP" };
  const VIEWS = {
    overview: ["运行总览", "关注解析去向与节点健康，让每一次切换都有依据。"],
    nodes: ["节点管理", "管理探测目标、主备顺序与参与调度的节点。"],
    routing: ["调度策略", "配置健康探测与自动切换规则，修改后手动保存。"],
    "ping-api": ["国内 Ping API", "在国内服务器一键部署，使用原有接口检测国外节点。"],
    domain: ["域名与连接", "管理主域名和 Cloudflare 凭据，检查已保存的连接。"],
    logs: ["运行日志", "查看探测、配置和解析变更的服务端记录。"],
    security: ["账户安全", "更新面板密码，管理当前登录会话。"],
  };
  const state = {
    status: null, config: null, statusError: "", lastSuccessAt: 0,
    fetching: false, busy: false, redirecting: false, refreshQueued: false, requestSerial: 0,
    pollTimer: null, ticker: null, statusController: null, dirty: new Set(),
    roundValue: null, roundObservedAt: 0, pendingProbe: null, editingTarget: null,
    nodeDirty: false, cfTest: null, logSignature: "", recentLogSignature: "",
  };
  const overviewRows = new Map();
  const managementRows = new Map();

  function make(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text ?? "");
    return item;
  }
  function text(elementOrId, value) {
    const element = typeof elementOrId === "string" ? $(elementOrId) : elementOrId;
    const next = String(value ?? "");
    if (element && element.textContent !== next) element.textContent = next;
  }
  function tone(element, value) { element.dataset.tone = value; }
  function message(id, value, level = "neutral") { text(id, value); tone($(id), level); }
  function announce(value, level = "success") {
    text("operationMessage", value);
    tone($("operationNotice"), level);
    $("operationNotice").hidden = false;
  }
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }
  function button(label, action, className = "button small") {
    const item = make("button", className, label);
    item.type = "button";
    item.dataset.action = action;
    item.dataset.write = "";
    return item;
  }
  function badge(value, level = "neutral") { const b = make("span", "badge", value); tone(b, level); return b; }
  function ageLabel(ms) {
    const seconds = Math.max(0, Math.floor(ms / 1000));
    if (seconds < 5) return "刚刚";
    if (seconds < 60) return `${seconds} 秒前`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    return `${Math.floor(seconds / 3600)} 小时前`;
  }
  function currentConfigKey(config) {
    return JSON.stringify([config?.record_name, config?.record_type, config?.zone_id, config?.api_token_masked]);
  }
  function isStale() { return !!state.lastSuccessAt && Date.now() - state.lastSuccessAt > STALE_MS; }
  function canWrite() { return !!state.status && !state.statusError && navigator.onLine && !isStale() && !state.busy && !state.redirecting; }
  function setUnavailable(element, unavailable) { element.dataset.unavailable = unavailable ? "true" : "false"; }

  class ApiError extends Error {
    constructor(value, status = 0, uncertain = false) { super(value); this.status = status; this.uncertain = uncertain; }
  }
  async function api(url, { method = "GET", payload, signal } = {}) {
    const writing = method !== "GET";
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (signal?.aborted) controller.abort();
    else signal?.addEventListener("abort", cancel, { once: true });
    const timeout = window.setTimeout(() => controller.abort(), writing ? 45000 : 15000);
    const headers = { Accept: "application/json" };
    if (writing) {
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = document.querySelector('meta[name="csrf-token"]')?.content || "";
    }
    try {
      const response = await fetch(url, {
        method, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        ...(writing ? { body: JSON.stringify(payload ?? {}) } : {}),
      });
      if (response.status === 401 || (response.redirected && new URL(response.url, location.href).pathname === "/login")) {
        state.redirecting = true;
        location.assign("/login");
        throw new ApiError("登录已过期，请重新登录。", 401);
      }
      let data;
      try { data = await response.json(); }
      catch { throw new ApiError("服务器返回了无法识别的响应，请刷新状态后重试。", response.status, writing); }
      if (!response.ok || data?.ok === false) {
        const fallback = response.status === 403 ? "安全校验未通过，请刷新页面后重试。" : `请求失败（HTTP ${response.status}）。`;
        throw new ApiError(typeof data?.msg === "string" && data.msg ? data.msg : fallback, response.status);
      }
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new ApiError("服务器响应格式不正确。", response.status, writing);
      if (writing && data.ok !== true) throw new ApiError("服务器未确认操作结果，请刷新状态后核对。", response.status, true);
      return data;
    } catch (error) {
      if (signal?.aborted) throw error;
      if (error instanceof ApiError) throw error;
      throw new ApiError(writing ? "请求未获确认，请刷新状态核对结果，避免重复操作。" : "无法连接服务，请检查网络后重试。", 0, writing);
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", cancel);
    }
  }

  function setButtonBusy(element, active, label = "处理中…") {
    if (!element) return;
    if (active) {
      element.setAttribute("aria-busy", "true");
      if (element.getAttribute("role") === "switch") return;
      let textElement = element.querySelector(":scope > span");
      if (!textElement) {
        const textNodes = Array.from(element.childNodes).filter((node) => node.nodeType === Node.TEXT_NODE);
        textElement = make("span", "button-label", textNodes.map((node) => node.textContent).join("").trim());
        textNodes.forEach((node) => node.remove());
        element.append(textElement);
      }
      element._busyLabel = textElement;
      element._idleText = textElement.textContent;
      text(textElement, label);
    } else {
      if (element._busyLabel && element._idleText !== undefined) text(element._busyLabel, element._idleText);
      element.removeAttribute("aria-busy");
      delete element._busyLabel;
      delete element._idleText;
    }
  }
  function syncInteractivity() {
    const writable = canWrite();
    document.querySelectorAll("[data-write]").forEach((element) => {
      element.disabled = !writable || element.dataset.unavailable === "true";
    });
    document.querySelectorAll("[data-config-form] fieldset").forEach((field) => { field.disabled = !state.config || state.busy; });
    $("passwordForm").querySelector("fieldset").disabled = state.busy;
    $("nodeForm").querySelector("fieldset").disabled = state.busy;
    document.querySelectorAll("[data-reset-form]").forEach((element) => { element.disabled = !state.config || state.busy; });
    document.querySelectorAll("[data-logout]").forEach((element) => { element.disabled = state.busy || state.redirecting; });
    document.querySelectorAll("[data-close-dialog]").forEach((element) => { element.disabled = state.busy; });
    document.querySelectorAll('[data-action="edit"]').forEach((element) => { element.disabled = state.busy; });
    $("refreshButton").disabled = state.fetching || state.busy;
    $("retryButton").disabled = state.fetching || state.busy;
    const probing = state.status?.probing === true || !!state.pendingProbe;
    setUnavailable($("probeButton"), probing || !state.status?.nodes?.length);
    $("probeButton").disabled = !writable || probing || !state.status?.nodes?.length;
    if (!state.busy) {
      text($("probeButton").querySelector("span"), probing ? "探测进行中" : "立即探测");
      if (probing) $("probeButton").setAttribute("aria-busy", "true");
      else $("probeButton").removeAttribute("aria-busy");
    }
    $("modeRoundRobin").disabled = !state.config || state.busy || state.config.record_type !== "A";
  }
  function stopPolling(abort = false) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (abort) { state.requestSerial += 1; state.statusController?.abort(); }
  }
  function schedulePoll() {
    stopPolling();
    if (!document.hidden && !state.busy && !state.redirecting) state.pollTimer = window.setTimeout(loadStatus, POLL_MS);
  }
  async function loadStatus() {
    if (document.hidden || state.busy || state.redirecting) return;
    if (state.fetching) { state.refreshQueued = true; return; }
    state.refreshQueued = false;
    stopPolling();
    state.fetching = true;
    const serial = ++state.requestSerial;
    const controller = new AbortController();
    state.statusController = controller;
    $("refreshButton").setAttribute("aria-busy", "true");
    syncInteractivity();
    try {
      const status = await api("/api/status", { signal: controller.signal });
      if (serial !== state.requestSerial || state.busy) return;
      if (!Array.isArray(status.nodes) || !status.config || typeof status.config !== "object") throw new ApiError("状态响应不完整，请稍后重试。");
      state.status = status;
      state.statusError = "";
      state.lastSuccessAt = Date.now();
      if (state.roundValue !== status.last_round) { state.roundValue = status.last_round; state.roundObservedAt = Date.now(); }
      if (state.pendingProbe) {
        if (status.probing === true) state.pendingProbe.seen = true;
        if (status.last_round !== state.pendingProbe.baseline || (state.pendingProbe.seen && status.probing === false)) {
          state.pendingProbe = null;
        } else if (Date.now() - state.pendingProbe.at > 15000 && status.probing === false) {
          state.pendingProbe = null;
          announce("探测请求已受理；尚未看到新的完成时间，请以最近探测记录为准。", "warning");
        }
      }
      applyConfig(status.config);
      renderStatus();
    } catch (error) {
      if (!controller.signal.aborted && !state.redirecting && serial === state.requestSerial) {
        state.statusError = error.message;
        if (!state.status) renderInitialFailure();
      }
    } finally {
      state.fetching = false;
      if (state.statusController === controller) state.statusController = null;
      $("refreshButton").removeAttribute("aria-busy");
      renderFreshness();
      syncInteractivity();
      if (state.refreshQueued && !document.hidden && !state.busy && !state.redirecting) {
        state.refreshQueued = false;
        void loadStatus();
      } else schedulePoll();
    }
  }

  function renderFreshness() {
    const stale = isStale();
    const offline = !navigator.onLine;
    let notice = "";
    if (offline) notice = state.status ? "网络已离线，当前显示的是上次同步的数据；恢复连接后将自动刷新。" : "网络已离线，暂时无法取得服务状态。";
    else if (state.statusError) notice = `${state.statusError}${state.status ? " 当前保留上次同步的数据，请刷新后再操作。" : " 尚未取得状态，无法确认服务健康。"}`;
    else if (stale) notice = `状态已过期，最近同步于 ${ageLabel(Date.now() - state.lastSuccessAt)}。请重新连接后再操作。`;
    $("connectionNotice").hidden = !notice;
    text("connectionMessage", notice);
    tone($("connectionNotice"), offline || stale ? "warning" : "danger");
    const level = offline || state.statusError || stale ? "warning" : state.status ? "success" : "neutral";
    tone($("sidebarDot"), level);
    tone($("syncDot"), level);
    text("sidebarConnection", offline ? "网络离线" : state.statusError || stale ? "状态更新中断" : state.status ? "服务状态已同步" : "等待同步状态");
    const syncLabel = state.lastSuccessAt ? `${offline || state.statusError || stale ? "上次同步" : "已同步"} · ${ageLabel(Date.now() - state.lastSuccessAt)}` : "等待首次同步";
    text("syncText", syncLabel);
    text("footerStatus", syncLabel);
    renderProbeNotice();
    syncInteractivity();
  }
  function renderProbeNotice() {
    const status = state.status;
    if (!status) return;
    let value = "";
    let level = "neutral";
    if (status.probing === true || state.pendingProbe) value = "正在执行健康探测，完成后将显示本轮结果。探测可能需要一些时间，请勿重复提交。";
    else if (status.last_probe_error) { value = `最近探测未完成：${status.last_probe_error}`; level = "danger"; }
    else if (status.nodes.length && !status.last_round) value = "尚无已完成的探测。节点保持未探测状态，可手动执行一次探测。";
    else {
      const limit = Math.max(90000, (Number(status.interval || 60) * 2 + Number(state.config?.timeout_seconds || 10) * Math.max(status.nodes.length, 1) * 2 + 15) * 1000);
      if (status.last_round && Date.now() - state.roundObservedAt > limit) {
        value = "最近探测时间长时间未更新，当前健康判定可能已过期。请检查日志或手动探测。";
        level = "warning";
      }
    }
    $("probeNotice").hidden = !value;
    text("probeNotice", value);
    tone($("probeNotice"), level);
  }
  function renderInitialFailure() {
    text("curTarget", "暂不可用"); text("lastRound", "未取得状态");
    for (const [bodyId, columns] of [["overviewNodeBody", 4], ["nodeBody", 5]]) setEmptyTable($(bodyId), columns, "无法加载节点状态", "请检查连接后，使用上方的刷新状态重试。");
    for (const id of ["recentLogBody", "logBody"]) $(id).replaceChildren(make("li", "empty-text", "尚未取得日志"));
  }

  function markDirty(form, dirty = true) {
    if (dirty) state.dirty.add(form.id); else state.dirty.delete(form.id);
    const indicator = form.querySelector(".unsaved-badge");
    if (indicator) indicator.hidden = !dirty;
  }
  function applyConfig(config, forceForm = "") {
    if (!config || typeof config !== "object") return;
    if (state.config && currentConfigKey(state.config) !== currentConfigKey(config)) state.cfTest = null;
    state.config = config;
    document.querySelectorAll("[data-config-form]").forEach((form) => {
      const force = form.id === forceForm;
      if (!force && (state.dirty.has(form.id) || form.contains(document.activeElement))) return;
      fillForm(form, config);
      if (force) markDirty(form, false);
    });
    const allowed = config.record_type === "A";
    $("roundRobinLabel").dataset.available = allowed ? "true" : "false";
    text("recordModeHint", allowed ? "多 A 记录仅管理本服务创建的记录；外部已有 A 记录会保留，不会自动接管或删除。DNS 缓存可能延迟实际流量变化。" : "当前已保存的主域名使用 CNAME；如需多 A 记录轮询，请先到“域名与连接”保存 A 记录类型。DNS 缓存可能延迟实际流量变化。");
    text("tokenHelp", config.has_token ? "服务端已保存 Token。此处留空保留原值；不会回填密钥或掩码。" : "服务端尚未配置 Token，请填写具有对应站点 DNS 权限的新 Token。");
    $("cfgToken").placeholder = config.has_token ? "已配置 · 留空保留原 Token" : "请输入新的 Cloudflare API Token";
    $("autoStart").required = $("autoWindowEnabled").checked;
    $("autoEnd").required = $("autoWindowEnabled").checked;
    syncInteractivity();
  }
  function fillForm(form, config) {
    const value = (id, data) => { $(id).value = String(data ?? ""); };
    if (form.id === "modeForm") {
      form.querySelectorAll('input[name="mode"]').forEach((input) => { input.checked = input.value === config.mode; });
    } else if (form.id === "probeForm") {
      value("cfgInterval", config.interval_seconds); value("cfgTimeout", config.timeout_seconds);
      value("cfgThreshold", config.fail_threshold); value("cfgDecision", config.decision_mode); value("cfgApiUrl", config.api_url);
    } else if (form.id === "autoForm") {
      $("autoWindowEnabled").checked = config.auto_window?.enabled === true;
      value("autoStart", config.auto_window?.start || "00:00"); value("autoEnd", config.auto_window?.end || "23:59");
      value("autoCooldown", config.cooldown_minutes ?? 0); value("cfgSlice", config.slice_seconds); $("cfgDryRun").checked = config.dry_run === true;
    } else if (form.id === "domainForm") {
      value("cfgRecord", config.record_name); value("cfgType", config.record_type); value("cfgZone", config.zone_id); value("cfgTtl", config.record_ttl);
      $("cfgToken").value = "";
    }
    form.querySelectorAll("input,select").forEach((input) => input.setCustomValidity(""));
  }

  function hasProbe(node) { return !!node.last_check && typeof node.healthy === "boolean"; }
  function health(node) {
    if (!hasProbe(node)) return ["未探测", "neutral"];
    return node.healthy === true ? ["正常", "success"] : ["异常", "danger"];
  }
  function probeValue(probe) {
    if (!probe || !probe.status || !["ok", "fail", "unknown"].includes(probe.status)) return ["未探测", "neutral"];
    if (probe.status === "fail") return ["失败", "danger"];
    if (probe.status === "unknown") return ["未确认", "warning"];
    const latency = Number(probe.latency);
    return [probe.latency !== null && probe.latency !== undefined && Number.isFinite(latency) && latency >= 0 ? `${latency} ms` : "成功", "success"];
  }
  function makeProbeLine(label) {
    const line = make("div", "probe-line");
    const labelElement = make("span", "probe-label", label);
    const valueElement = make("span", "probe-value", "未探测");
    line.append(labelElement, valueElement);
    line._value = valueElement;
    return line;
  }
  function updateProbeLine(line, probe, type) {
    const [value, level] = probeValue(probe);
    text(line._value, value); tone(line, level);
    line.title = probe?.error ? `${type === "ICMP" ? "国内探测" : "TCP 探测"}：${probe.error}` : `${type}：${value}`;
  }
  function nodeSync(node, status) {
    if (status.mode === "round_robin") {
      const value = node.sync && node.sync !== "-" ? node.sync : "等待同步";
      const levels = { "已同步": "success", "待同步": "warning", "待摘除": "warning", "保留外部记录": "warning", "模拟（未写 DNS）": "accent", "已禁用（仍有记录）": "warning" };
      return [value, levels[value] || "neutral"];
    }
    if (node.current === true && status.cf_loaded !== false) return ["当前解析", "accent"];
    return [node.enabled === false ? "不参与自动" : "备用节点", "neutral"];
  }
  function renderStatus() {
    const s = state.status, c = state.config, nodes = s.nodes;
    const checked = nodes.filter(hasProbe);
    const healthyCount = checked.filter((node) => node.healthy === true).length;
    const abnormalCount = checked.filter((node) => node.healthy === false).length;
    text("headerDomain", s.record_name || "尚未配置主域名");
    text("curTarget", s.current || (s.cf_loaded === false ? "尚未读取解析" : "暂无解析目标"));
    text("currentContext", s.cf_loaded === true ? (s.cf_stale ? "上次读取的解析 · 信息待更新" : `Cloudflare 已读取 · ${s.record_type || "DNS"} 记录`) : "尚未确认 Cloudflare 的实际解析");
    const healthElement = $("healthSummary");
    healthElement.replaceChildren(document.createTextNode(checked.length ? String(healthyCount) : "—"), make("span", "metric-unit", ` / ${nodes.length}`));
    tone(healthElement, !checked.length ? "neutral" : abnormalCount ? "warning" : "success");
    text("healthContext", !nodes.length ? "暂无节点，请先添加探测目标" : !checked.length ? "全部节点尚未完成探测" : `${abnormalCount} 个异常${nodes.length > checked.length ? ` · ${nodes.length - checked.length} 个未探测` : " · 已探测全部节点"}`);
    text("autoState", s.auto_switch === true ? "已开启" : "已暂停");
    tone($("autoState"), s.auto_switch === true ? "success" : "warning");
    $("autoSwitch").setAttribute("aria-checked", String(s.auto_switch === true));
    text("autoContext", s.auto_switch !== true ? "仅监测健康，不自动更改解析" : s.dry_run === true ? "演练中 · 不写入 DNS 变更" : c.auto_window?.enabled ? `生效时间 ${c.auto_window.start} — ${c.auto_window.end}` : "按当前规则自动调度");
    text("lastRound", s.last_round ? String(s.last_round).slice(11) || s.last_round : "尚未探测");
    text("roundContext", s.probing === true || state.pendingProbe ? "正在执行新一轮探测…" : s.last_round ? `${String(s.last_round).slice(0, 10)} · 服务器时间` : "等待首轮探测完成");
    $("dryBadge").hidden = s.dry_run !== true;
    for (const id of ["navNodeCount", "overviewNodeCount", "nodeCount"]) text(id, nodes.length);
    text("modeSummary", MODE_LABEL[s.mode] || "未知模式"); text("intervalSummary", `${s.interval} 秒 / 轮`);
    text("thresholdSummary", `${s.threshold} 次`); text("decisionSummary", DECISION_LABEL[s.decision_mode] || s.decision_mode || "未配置");
    text("windowSummary", c.auto_window?.enabled ? `${c.auto_window.start} — ${c.auto_window.end}` : "全天");
    text("cooldownSummary", Number(c.cooldown_minutes) > 0 ? `${c.cooldown_minutes} 分钟` : "不限制");
    text("modeHint", s.mode === "weighted" ? "按权重分配 DNS 解析时间片；实际流量受 DNS 缓存影响。" : s.mode === "round_robin" ? "健康节点组成多 A 记录地址集合；仅管理本服务创建的记录。" : "按列表顺序选择健康备用节点；↑ ↓ 调整主备优先级。");
    text("appVersion", s.version ? `版本 ${s.version}` : "桃花酱出品");
    $("cfErrorNotice").hidden = !s.cf_error;
    text("cfErrorText", s.cf_error || "");
    renderCfSummary(); renderExtraNotices();
    renderNodes($("overviewNodeBody"), nodes, overviewRows, makeOverviewRow, updateOverviewRow, 4);
    renderNodes($("nodeBody"), nodes, managementRows, makeManagementRow, updateManagementRow, 5);
    renderLogs(); renderFreshness();
  }
  function renderCfSummary() {
    const s = state.status;
    if ((s.cf_error || s.cf_stale === true) && state.cfTest && !state.cfTest.error) state.cfTest = null;
    let title, description;
    if (s.cf_error) { title = "Cloudflare 连接异常"; description = "检查域名配置、凭据与服务日志"; }
    else if (!s.has_token || !state.config.zone_id) { title = "Cloudflare 尚未配置"; description = "请补全主域名与访问凭据"; }
    else if (s.cf_loaded === true && !s.cf_stale) { title = "Cloudflare 已读取"; description = s.cf_observed_at ? `读取时间 ${s.cf_observed_at}` : "已取得实际 DNS 记录"; }
    else if (s.cf_loaded === true && s.cf_stale) { title = "Cloudflare 信息待更新"; description = "当前显示上次读取的解析记录"; }
    else { title = "Cloudflare 已配置"; description = "尚未确认连接，请手动测试"; }
    text("cfBadge", title); text("cfSummary", description);
    if (!state.cfTest) {
      text("cfTestTitle", s.cf_error ? "连接存在异常" : s.cf_loaded === true ? s.cf_stale ? "上次读取的信息已过期" : "已读取实际解析" : "尚未测试连接");
      text("cfTestDescription", s.cf_error ? "请检查配置，并手动重试连接检查。" : s.cf_observed_at ? `最近读取：${s.cf_observed_at}（服务器时间）` : "保存配置后，可检查 Token 权限和主域名记录是否可读取。");
      tone($("cfTestMarker"), s.cf_error ? "danger" : s.cf_loaded === true && !s.cf_stale ? "success" : "neutral");
      $("cfTestResult").hidden = true;
    }
  }
  function extraNotice(id, value, level = "warning") {
    let item = $(id);
    if (!item) {
      item = make("div", "notice"); item.id = id; item.setAttribute("role", "status");
      $("operationNotice").before(item);
    }
    item.hidden = !value; text(item, value); tone(item, level);
  }
  function renderExtraNotices() {
    const s = state.status;
    const external = Array.isArray(s.unmanaged_records) ? s.unmanaged_records : [];
    extraNotice("unmanagedNotice", s.mode === "round_robin" && external.length ? `保留 ${external.length} 条外部 A 记录：${external.join("、")}。这些记录不由本服务自动摘除；异常地址可能仍被解析。` : "");
    const simulation = s.simulation;
    const hasSimulation = simulation && typeof simulation === "object" && Object.keys(simulation).length;
    const target = hasSimulation ? simulation.current || (Array.isArray(simulation.ips) ? simulation.ips.join("、") || "保留现有记录" : "") : "";
    extraNotice("simulationNotice", s.dry_run === true && hasSimulation ? `最近演练：${target || "已计算调度方案"}${simulation.at ? `（${simulation.at}）` : ""}。仅为模拟结果，上方仍展示实际解析。` : "", "neutral");
    extraNotice("cfStaleNotice", s.cf_stale === true && s.cf_loaded === true && !s.cf_error ? `Cloudflare 解析信息待更新${s.cf_observed_at ? `，最近读取于 ${s.cf_observed_at}` : ""}。可到“域名与连接”手动检查。` : "");
  }

  function setEmptyTable(body, columns, title, description = "") {
    const row = make("tr", "placeholder-row"), cell = make("td"), content = make("div", "empty-state");
    cell.colSpan = columns; content.append(make("p", "", title));
    if (description) content.append(make("p", "field-help", description));
    if (state.status?.nodes?.length === 0) {
      const add = button("添加第一个节点", "add", "button primary");
      add.addEventListener("click", () => openNodeDialog()); content.append(add);
    }
    cell.append(content); row.append(cell); body.replaceChildren(row);
  }
  function renderNodes(body, nodes, map, create, update, columns) {
    const targets = new Set(nodes.map((node) => node.target));
    for (const [target, row] of map) if (!targets.has(target)) { row.remove(); map.delete(target); }
    if (!nodes.length) { setEmptyTable(body, columns, "还没有节点", "添加域名或 IPv4 地址后，才能查看探测结果与配置调度。"); return; }
    body.querySelector(".placeholder-row")?.remove();
    nodes.forEach((node, index) => {
      let row = map.get(node.target);
      if (!row) { row = create(); row.dataset.target = node.target; map.set(node.target, row); }
      row._node = node;
      update(row, node, index);
      if (body.children[index] !== row) body.insertBefore(row, body.children[index] || null);
    });
  }
  function makeOverviewRow() {
    const row = make("tr");
    const identity = make("td"), statusCell = make("td"), probes = make("td"), syncCell = make("td");
    const name = make("div", "node-name"), target = make("div", "node-target"), verdict = badge("未探测");
    const apiLine = makeProbeLine("ICMP"), tcpLine = makeProbeLine("TCP"), sync = badge("待确认"), note = make("div", "node-status-note");
    identity.append(name, target); statusCell.append(verdict); probes.append(apiLine, tcpLine); syncCell.append(sync, note);
    row.append(identity, statusCell, probes, syncCell);
    row._refs = { name, target, verdict, apiLine, tcpLine, sync, note };
    return row;
  }
  function updateOverviewRow(row, node) {
    const r = row._refs, s = state.status;
    text(r.name, node.name || node.target); text(r.target, node.target);
    const [value, level] = health(node); text(r.verdict, value); tone(r.verdict, level);
    updateProbeLine(r.apiLine, node.api, "ICMP"); updateProbeLine(r.tcpLine, node.tcp, "TCP");
    const [sync, syncLevel] = nodeSync(node, s); text(r.sync, sync); tone(r.sync, syncLevel);
    text(r.note, node.last_check ? `${String(node.last_check).slice(11)} 探测` : "暂无完成时间");
  }
  function makeManagementRow() {
    const row = make("tr");
    const identity = make("td"), healthCell = make("td"), enabledCell = make("td"), weightCell = make("td"), actionsCell = make("td");
    const name = make("div", "node-name"), index = make("span", "node-index"), nameValue = make("span");
    const target = make("div", "node-target"), ip = make("div", "node-ip");
    name.append(index, nameValue); identity.append(name, target, ip);
    const healthBox = make("div", "node-health"), verdict = badge("未探测"), probeBox = make("div");
    const apiLine = makeProbeLine("ICMP"), tcpLine = makeProbeLine("TCP"), note = make("div", "node-status-note");
    probeBox.append(apiLine, tcpLine);
    const details = make("details", "probe-details"), detailsSummary = make("summary", "", "查看探测错误"), errors = make("p");
    details.append(detailsSummary, errors); healthBox.append(verdict, probeBox, note, details); healthCell.append(healthBox);
    const enabled = button("参与", "enabled", "node-enabled-button"); enabled.setAttribute("aria-pressed", "false"); enabledCell.append(enabled);
    const weight = make("div", "weight-value"), weightLabel = make("div", "weight-label"); weightCell.append(weight, weightLabel);
    const actions = make("div", "node-actions"), switchButton = button("切换", "switch"), edit = button("编辑", "edit");
    delete edit.dataset.write;
    const order = make("span", "node-order-actions"), up = button("↑", "up", "button order-button"), down = button("↓", "down", "button order-button");
    const remove = button("删除", "delete", "button small danger"); order.append(up, down); actions.append(switchButton, edit, order, remove); actionsCell.append(actions);
    row.append(identity, healthCell, enabledCell, weightCell, actionsCell);
    row._refs = { index, nameValue, target, ip, verdict, apiLine, tcpLine, note, details, errors, enabled, weight, weightLabel, switchButton, edit, up, down, remove };
    enabled.addEventListener("click", () => toggleNode(row._node, enabled));
    switchButton.addEventListener("click", () => switchNode(row._node, switchButton));
    edit.addEventListener("click", () => openNodeDialog(row._node));
    up.addEventListener("click", () => moveNode(row._node, -1, up)); down.addEventListener("click", () => moveNode(row._node, 1, down));
    remove.addEventListener("click", () => deleteNode(row._node, remove));
    return row;
  }
  function updateManagementRow(row, node, index) {
    const r = row._refs, s = state.status;
    text(r.index, String(index + 1).padStart(2, "0")); text(r.nameValue, node.name || node.target);
    text(r.target, `${node.target}:${node.port}`); text(r.ip, node.ip ? `解析 IP · ${node.ip}` : "解析 IP · 暂无结果");
    const [value, level] = health(node); text(r.verdict, value); tone(r.verdict, level);
    updateProbeLine(r.apiLine, node.api, "ICMP"); updateProbeLine(r.tcpLine, node.tcp, "TCP");
    const [sync] = nodeSync(node, s);
    text(r.note, `${s.mode === "round_robin" || node.current ? `${sync} · ` : ""}${node.last_check || "尚未完成探测"}${Number(node.fail) > 0 ? ` · 连续失败 ${node.fail} 次` : ""}`);
    const errors = [];
    if (node.api?.error) errors.push(`国内探测${node.api.status === "fail" ? "失败" : "未确认"}：${node.api.error}`);
    if (node.tcp?.error) errors.push(`TCP 探测${node.tcp.status === "fail" ? "失败" : "未确认"}：${node.tcp.error}`);
    text(r.errors, errors.join("\n")); r.details.hidden = !errors.length;
    text(r.enabled, node.enabled === false ? "已停用" : "参与中"); r.enabled.setAttribute("aria-pressed", String(node.enabled !== false));
    r.enabled.setAttribute("aria-label", `${node.name || node.target}：${node.enabled === false ? "启用参与自动调度" : "停用参与自动调度"}`);
    text(r.weight, node.weight ?? 1); text(r.weightLabel, s.mode === "weighted" ? "生效中" : "当前模式不使用");
    r.switchButton.hidden = s.mode === "round_robin";
    text(r.switchButton, node.current === true && s.cf_loaded !== false ? "当前" : "切换");
    setUnavailable(r.switchButton, node.current === true && s.cf_loaded !== false);
    row.classList.toggle("is-current", node.current === true && s.mode !== "round_robin" && s.cf_loaded !== false);
    r.switchButton.setAttribute("aria-label", `手动切换到 ${node.name || node.target}`);
    r.edit.setAttribute("aria-label", `编辑 ${node.name || node.target}`); r.edit.disabled = state.busy;
    r.up.setAttribute("aria-label", `上移 ${node.name || node.target}`); r.down.setAttribute("aria-label", `下移 ${node.name || node.target}`);
    r.remove.setAttribute("aria-label", `删除 ${node.name || node.target}`);
    setUnavailable(r.up, index === 0); setUnavailable(r.down, index === s.nodes.length - 1);
  }

  function makeLog(log) {
    const level = log.level === "error" ? "danger" : log.level === "warn" || log.level === "warning" ? "warning" : "neutral";
    const labels = { error: "错误", warn: "警告", warning: "警告", info: "信息", debug: "调试" };
    const item = make("li", "activity-item"), time = make("time", "activity-time", log.ts || "时间未知");
    const type = make("span", "activity-level", labels[log.level] || "记录"), value = make("span", "activity-message", log.msg ?? "");
    tone(item, level); tone(type, level); item.append(time, type, value); return item;
  }
  function renderLogs() {
    const logs = Array.isArray(state.status?.logs) ? state.status.logs : [];
    const filter = $("logFilter").value;
    const filtered = filter === "all" ? logs : logs.filter((log) => log.level === filter || (filter === "warn" && log.level === "warning"));
    text("logCount", filter === "all" ? `${logs.length} 条` : `${filtered.length} / ${logs.length} 条`);
    const signature = JSON.stringify([filter, logs]);
    if (signature !== state.logSignature) {
      const scrollTop = $("logBody").scrollTop;
      $("logBody").replaceChildren(...(filtered.length ? filtered.map(makeLog) : [make("li", "empty-text", logs.length ? "没有符合筛选条件的日志" : "暂无运行日志")]));
      $("logBody").scrollTop = scrollTop; state.logSignature = signature;
    }
    const recent = logs.slice(0, 5), recentSignature = JSON.stringify(recent);
    if (recentSignature !== state.recentLogSignature) {
      $("recentLogBody").replaceChildren(...(recent.length ? recent.map(makeLog) : [make("li", "empty-text", "暂无运行日志")])); state.recentLogSignature = recentSignature;
    }
  }

  let confirmationPending = false;
  async function confirmAction(title, description, accept = "确认操作") {
    if (confirmationPending || state.busy) return false;
    confirmationPending = true;
    const dialog = $("confirmDialog");
    text("confirmTitle", title); text("confirmDescription", description); text("confirmAccept", accept);
    dialog.returnValue = "cancel";
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => { confirmationPending = false; resolve(dialog.returnValue === "confirm"); }, { once: true });
      dialog.showModal();
    });
  }
  async function mutate({ endpoint, payload = {}, method = "POST", trigger, msgId, success, onSuccess, onError, sessionAction = false }) {
    if (state.busy || state.redirecting) return false;
    if (!sessionAction && !canWrite()) {
      if (msgId) message(msgId, "状态暂不可确认，请刷新连接后重试。", "danger");
      else announce("状态暂不可确认，请刷新连接后重试。", "warning");
      return false;
    }
    state.busy = true; stopPolling(true); setButtonBusy(trigger, true);
    if (msgId) message(msgId, "正在提交，请稍候…");
    syncInteractivity();
    try {
      const result = await api(endpoint, { method, payload });
      if (onSuccess) await onSuccess(result);
      if (success) {
        const value = typeof success === "function" ? success(result) : success;
        if (msgId) message(msgId, value, "success"); else announce(value);
      }
      return true;
    } catch (error) {
      if (!state.redirecting) {
        if (msgId) message(msgId, error.message, "danger"); else announce(error.message, "danger");
        if (onError) onError(error);
        if (error.uncertain) state.statusError = "上次操作结果未获确认，请刷新状态核对。";
      }
      return false;
    } finally {
      setButtonBusy(trigger, false); state.busy = false;
      renderFreshness(); syncInteractivity();
      if (!state.redirecting) {
        if (state.fetching) schedulePoll(); else void loadStatus();
      }
    }
  }

  function normalizeHost(value) {
    const source = value.trim().replace(/\.$/, "");
    if (!source || source.length > 253 || /[\s/:\\?#@<>"'\[\]]/.test(source)) return null;
    let host;
    try { host = new URL(`http://${source}`).hostname.toLowerCase(); } catch { return null; }
    if (/^[0-9.]+$/.test(source)) {
      const parts = source.split(".");
      return parts.length === 4 && parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255) ? parts.map(Number).join(".") : null;
    }
    if (host.length > 253 || !host.split(".").every((part) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(part))) return null;
    return host;
  }
  function validateHost(id, allowIp = true) {
    const input = $(id), host = normalizeHost(input.value);
    const valid = !!host && (allowIp || !/^\d+\.\d+\.\d+\.\d+$/.test(host));
    input.setCustomValidity(valid ? "" : allowIp ? "请填写有效的域名或 IPv4 地址，不含协议、端口与路径。" : "请填写有效的主域名，不含协议、端口与路径。");
    return valid ? host : null;
  }
  function number(id) { return Number($(id).value); }
  function formTrigger(event) { return event.submitter || event.target?.querySelector('button[type="submit"]'); }
  async function saveSettings(formId, payload, msgId, trigger, confirmation) {
    if (confirmation && !await confirmAction(...confirmation)) return;
    await mutate({ endpoint: "/api/config", payload, trigger, msgId, success: "已保存，后续操作使用新配置。", onSuccess(result) {
      if (result.config) applyConfig(result.config, formId);
      else markDirty($(formId), false);
      if (formId === "domainForm") { $("cfgToken").value = ""; state.cfTest = null; }
    } });
  }
  function openNodeDialog(node = null) {
    if (state.busy) return;
    state.editingTarget = node ? node.target : null; state.nodeDirty = false;
    $("nodeForm").reset();
    $("nodeTarget").readOnly = !!node;
    $("nodeName").value = node?.name || ""; $("nodeTarget").value = node?.target || "";
    $("nodePort").value = node?.port ?? 443; $("nodeWeight").value = node?.weight ?? 1; $("nodeEnabled").checked = node?.enabled !== false;
    $("nodeTarget").setCustomValidity("");
    text("nodeDialogTitle", node ? "编辑节点" : "添加节点"); text("saveNodeButton", node ? "保存节点" : "添加节点");
    text("nodeDialogDescription", node ? "修改名称、端口、权重与参与状态。目标地址作为节点标识，编辑时不可更改。" : "添加用于健康探测和解析调度的域名或 IPv4 地址。");
    text("nodeTargetHelp", node ? "目标地址不可修改。如需更换目标，请添加新节点。" : "填写域名或 IPv4 地址，不含协议、端口和路径。");
    message("nodeMsg", ""); $("nodeDialog").showModal(); $("nodeName").focus(); syncInteractivity();
  }
  async function switchNode(node, trigger) {
    const disabledNote = node.enabled === false ? "该节点目前不参与自动调度；本次仍会执行手动切换。\n" : "";
    const healthNote = node.healthy !== true || !hasProbe(node) ? "该节点尚未确认健康，请留意切换风险。\n" : "";
    const dry = state.status.dry_run === true;
    if (!await confirmAction(dry ? "模拟切换解析？" : "手动切换解析？", `${disabledNote}${healthNote}${dry ? "演练将模拟切换，不会写入 Cloudflare。" : "将向 Cloudflare 提交主域名解析变更，实际生效时间受 DNS 缓存影响。"}\n目标：${node.name || node.target}（${node.target}）`, dry ? "确认演练" : "确认切换")) return;
    await mutate({ endpoint: "/api/switch", payload: { target: node.target }, trigger,
      success: (result) => result.simulated === true ? "已记录模拟切换，实际 DNS 未更改。" : "服务端已确认解析切换，请留意 DNS 缓存的生效时间。" });
  }
  async function deleteNode(node, trigger) {
    if (!await confirmAction("删除这个节点？", `将移除 ${node.name || node.target}（${node.target}）的节点配置。${node.current ? "该节点当前仍在解析中；删除配置不代表 DNS 已切走。" : "已有 DNS 记录将按服务端调度规则处理。"}\n此操作无法直接撤销。`, "确认删除")) return;
    await mutate({ endpoint: `/api/nodes?target=${encodeURIComponent(node.target)}`, method: "DELETE", trigger, success: "节点已删除。实际解析变化请以状态与日志为准。" });
  }
  async function toggleNode(node, trigger) {
    const enabled = node.enabled === false;
    if (!enabled && !await confirmAction("停用参与自动调度？", `${node.name || node.target} 将不再参与自动调度，仍可查看探测结果与手动切换。${node.current ? "\n当前解析不会仅因本次停用立即切走，请核对调度规则。" : ""}`, "确认停用")) return;
    await mutate({ endpoint: "/api/nodes/update", payload: { target: node.target, enabled }, trigger,
      success: enabled ? "节点已参与自动调度。" : "节点已停用参与自动调度。" });
  }
  async function moveNode(node, delta, trigger) {
    const order = state.status.nodes.map((item) => item.target), index = order.indexOf(node.target), next = index + delta;
    if (index < 0 || next < 0 || next >= order.length) return;
    [order[index], order[next]] = [order[next], order[index]];
    await mutate({ endpoint: "/api/nodes/reorder", payload: { order }, trigger, success: "节点顺序已保存。" });
  }

  function changeView(focus = false) {
    const requested = location.hash.slice(1), view = Object.hasOwn(VIEWS, requested) ? requested : "overview";
    if (requested === "mainContent") { $("mainContent").focus(); return; }
    const [title, description] = VIEWS[view];
    document.querySelectorAll("[data-view-panel]").forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== view; });
    document.querySelectorAll("[data-nav]").forEach((link) => {
      const active = link.dataset.nav === view; link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    text("pageTitle", title); text("breadcrumbCurrent", title); text("pageDescription", description);
    document.title = `${title} · 桃花源 GTM`;
    if (focus) { $("pageTitle").tabIndex = -1; $("pageTitle").focus({ preventScroll: true }); window.scrollTo({ top: 0, behavior: "auto" }); }
  }

  document.querySelectorAll("[data-config-form]").forEach((form) => {
    for (const eventName of ["input", "change"]) form.addEventListener(eventName, () => markDirty(form));
    form.addEventListener("focusout", () => {
      window.setTimeout(() => { if (state.config && !state.dirty.has(form.id) && !form.contains(document.activeElement)) fillForm(form, state.config); }, 0);
    });
  });
  document.querySelectorAll("input,select").forEach((input) => input.addEventListener("input", () => input.setCustomValidity("")));
  $("pwdNew").addEventListener("input", () => $("pwdConfirm").setCustomValidity(""));
  document.querySelectorAll("[data-reset-form]").forEach((trigger) => trigger.addEventListener("click", () => {
    const form = $(trigger.dataset.resetForm); if (!state.config || state.busy) return;
    fillForm(form, state.config); markDirty(form, false);
    form.querySelector(".form-message")?.replaceChildren(); applyConfig(state.config); syncInteractivity();
  }));
  $("autoWindowEnabled").addEventListener("change", () => { $("autoStart").required = $("autoWindowEnabled").checked; $("autoEnd").required = $("autoWindowEnabled").checked; });
  $("modeForm").addEventListener("submit", async (event) => {
    event.preventDefault(); const selected = event.currentTarget.querySelector('input[name="mode"]:checked')?.value;
    if (!selected || !Object.hasOwn(MODE_LABEL, selected)) { message("modeMsg", "请选择一种调度模式。", "danger"); return; }
    if (selected === "round_robin" && state.config.record_type !== "A") { message("modeMsg", "多 A 记录轮询需要先保存 A 记录类型。", "danger"); return; }
    await saveSettings("modeForm", { mode: selected }, "modeMsg", formTrigger(event), selected !== state.config.mode ? ["更改调度模式？", `将使用“${MODE_LABEL[selected]}”。后续探测将按该规则执行调度，可能改变实际解析；已有 DNS 缓存不会立即消失。`, "确认保存"] : null);
  });
  $("probeForm").addEventListener("submit", async (event) => {
    event.preventDefault(); const input = $("cfgApiUrl"), url = input.value.trim();
    let valid = false;
    try { const parsed = new URL(url); valid = ["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password && url.includes("{target}"); } catch { valid = false; }
    input.setCustomValidity(valid ? "" : "请填写有效的 HTTP / HTTPS 地址，并包含 {target} 占位符，地址中不能包含登录凭据。");
    if (!event.currentTarget.reportValidity()) return;
    await saveSettings("probeForm", { interval_seconds: number("cfgInterval"), timeout_seconds: number("cfgTimeout"), fail_threshold: number("cfgThreshold"), decision_mode: $("cfgDecision").value, api_url: url }, "cfgMsg", formTrigger(event));
  });
  $("autoForm").addEventListener("submit", async (event) => {
    event.preventDefault(); if (!event.currentTarget.reportValidity()) return;
    const dry = $("cfgDryRun").checked;
    await saveSettings("autoForm", { auto_window: { enabled: $("autoWindowEnabled").checked, start: $("autoStart").value || "00:00", end: $("autoEnd").value || "23:59" }, cooldown_minutes: number("autoCooldown"), slice_seconds: number("cfgSlice"), dry_run: dry }, "autoMsg", formTrigger(event), state.config.dry_run === true && !dry ? ["结束演练模式？", "保存后，手动切换与允许执行的自动调度将向 Cloudflare 写入真实 DNS 变更。请确认节点与调度规则。", "确认结束演练"] : null);
  });
  $("domainForm").addEventListener("submit", async (event) => {
    event.preventDefault(); const host = validateHost("cfgRecord", false), token = $("cfgToken").value.trim(), ttl = number("cfgTtl");
    $("cfgTtl").setCustomValidity(ttl === 1 || (Number.isInteger(ttl) && ttl >= 60 && ttl <= 86400) ? "" : "TTL 请填写 1（自动），或 60 至 86400 秒。");
    const masked = token === state.config.api_token_masked || token === "已配置" || token.includes("*") || token.includes("...") || token.includes("…");
    $("cfgToken").setCustomValidity(token && masked ? "请填写新的完整 Token；保留原 Token 时请留空，不可提交掩码。" : "");
    if (!event.currentTarget.reportValidity() || !host) return;
    if (state.config.mode === "round_robin" && $("cfgType").value !== "A") { message("domainMsg", "当前使用多 A 记录模式，请先切换到主备或权重模式，再保存 CNAME。", "danger"); return; }
    const payload = { record_name: host, record_type: $("cfgType").value, zone_id: $("cfgZone").value.trim(), record_ttl: ttl };
    if (token) payload.api_token = token;
    await saveSettings("domainForm", payload, "domainMsg", formTrigger(event), ["保存主域名配置？", `将保存 ${host}（${payload.record_type}）及本表单的 Cloudflare 配置。后续探测和切换使用这些设置，可能影响该域名的解析。Token 留空时保留原值。`, "确认保存"]);
  });
  $("nodeForm").addEventListener("input", () => { state.nodeDirty = true; });
  $("nodeForm").addEventListener("change", () => { state.nodeDirty = true; });
  $("nodeForm").addEventListener("submit", async (event) => {
    event.preventDefault(); const target = state.editingTarget || validateHost("nodeTarget");
    if (!event.currentTarget.reportValidity() || !target) return;
    if (!state.editingTarget && state.status.nodes.some((node) => node.target.toLowerCase() === target.toLowerCase())) { message("nodeMsg", "这个目标已存在，请编辑现有节点。", "danger"); return; }
    const payload = { name: $("nodeName").value.trim() || target, target, port: number("nodePort"), weight: number("nodeWeight"), enabled: $("nodeEnabled").checked };
    const previous = state.status.nodes.find((node) => node.target === target);
    if (previous && previous.enabled !== false && payload.enabled === false && !await confirmAction("停用参与自动调度？", `${previous.name || target} 将退出自动调度，仍保留探测与手动切换。当前实际解析不会仅因本次修改立即切走。`, "确认停用并保存")) return;
    await mutate({ endpoint: state.editingTarget ? "/api/nodes/update" : "/api/nodes", payload, trigger: formTrigger(event), msgId: "nodeMsg", onSuccess() {
      state.nodeDirty = false; $("nodeDialog").close(); announce(state.editingTarget ? "节点配置已保存。" : "节点已添加，健康状态将在完成探测后显示。");
    } });
  });
  $("passwordForm").addEventListener("submit", async (event) => {
    event.preventDefault(); const old = $("pwdOld").value, next = $("pwdNew").value;
    $("pwdConfirm").setCustomValidity(next === $("pwdConfirm").value ? "" : "两次新密码输入不一致。");
    if (!event.currentTarget.reportValidity()) return;
    await mutate({ endpoint: "/api/password", payload: { old, new: next }, trigger: formTrigger(event), msgId: "pwdMsg", success: "密码已更新，其他旧会话已失效。请妥善保存新密码。", onSuccess() { $("passwordForm").reset(); } });
  });
  $("addNodeButton").addEventListener("click", () => openNodeDialog());
  $("autoSwitch").addEventListener("click", async () => {
    if (!state.status) return; const enabled = state.status.auto_switch !== true;
    if (!await confirmAction(enabled ? "开启自动切换？" : "暂停自动切换？", enabled ? "服务将按已保存的模式、时间窗和冷却规则自动调度。关闭演练模式时，允许执行的调度会写入真实 DNS 变更。" : "服务继续探测节点，但暂停自动更新 DNS 解析。仍可手动探测和切换。", enabled ? "确认开启" : "确认暂停")) return;
    await mutate({ endpoint: "/api/auto", payload: { enabled }, trigger: $("autoSwitch"), success: enabled ? "自动切换已开启。" : "自动切换已暂停。", onSuccess(result) {
      state.status.auto_switch = result.auto_switch === true; if (state.config) state.config.auto_switch = result.auto_switch === true; renderStatus();
    } });
  });
  $("probeButton").addEventListener("click", async () => {
    if (!state.status || state.status.probing === true || state.pendingProbe) return;
    if (state.status.auto_switch === true && state.status.dry_run !== true && !await confirmAction("立即探测节点？", "探测将按当前策略判定健康。自动切换已开启，探测结果可能触发真实 DNS 变更。", "开始探测")) return;
    await mutate({ endpoint: "/api/probe", trigger: $("probeButton"), success: "探测请求已受理，正在等待本轮结果。", onSuccess() { state.pendingProbe = { at: Date.now(), baseline: state.status.last_round, seen: false }; renderProbeNotice(); } });
  });
  $("testCFButton").addEventListener("click", async () => {
    await mutate({ endpoint: "/api/test_cf", trigger: $("testCFButton"), onSuccess(result) {
      state.cfTest = { key: currentConfigKey(state.config), result };
      text("cfTestTitle", "已确认连接可读取"); text("cfTestDescription", `服务端返回 ${Number(result.count) || 0} 条主域名记录。${state.dirty.has("domainForm") ? "本次使用已保存的配置，未提交表单修改。" : "此次检查没有更改 DNS。"}`); tone($("cfTestMarker"), "success");
      const content = [make("strong", "", `${Number(result.count) || 0} 条 DNS 记录`)];
      for (const record of Array.isArray(result.records) ? result.records : []) content.push(make("p", "", `${record.type || "未知类型"} · ${record.content || "空内容"} · TTL ${record.ttl === 1 ? "自动" : record.ttl ?? "未知"}`));
      $("cfTestResult").replaceChildren(...content); $("cfTestResult").hidden = false;
      announce("Cloudflare 连接检查完成，结果见连接检查卡片。");
    }, onError(error) { state.cfTest = { error: true }; text("cfTestTitle", "连接检查失败"); text("cfTestDescription", error.message); tone($("cfTestMarker"), "danger"); $("cfTestResult").hidden = true; } });
  });
  document.querySelectorAll("[data-logout]").forEach((trigger) => trigger.addEventListener("click", async () => {
    if ((state.dirty.size || state.nodeDirty) && !await confirmAction("退出当前会话？", "仍有未保存的修改。退出后需要重新登录，未提交的输入将丢失。", "确认退出")) return;
    await mutate({ endpoint: "/logout", trigger, sessionAction: true, onSuccess() { state.dirty.clear(); state.nodeDirty = false; state.redirecting = true; location.assign("/login"); } });
  }));
  document.querySelectorAll("[data-close-dialog]").forEach((trigger) => trigger.addEventListener("click", () => { if (!state.busy) $(trigger.dataset.closeDialog).close(); }));
  $("nodeDialog").addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
  $("nodeDialog").addEventListener("close", () => { state.nodeDirty = false; });
  $("logFilter").addEventListener("change", renderLogs);
  $("dismissOperation").addEventListener("click", () => { $("operationNotice").hidden = true; });
  $("refreshButton").addEventListener("click", () => { void loadStatus(); });
  $("retryButton").addEventListener("click", () => { void loadStatus(); });
  window.addEventListener("hashchange", () => changeView(true));
  window.addEventListener("offline", () => { renderFreshness(); });
  window.addEventListener("online", () => { renderFreshness(); void loadStatus(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { stopPolling(true); clearInterval(state.ticker); state.ticker = null; }
    else { renderFreshness(); if (!state.ticker) state.ticker = window.setInterval(renderFreshness, 1000); void loadStatus(); }
  });
  window.addEventListener("beforeunload", (event) => {
    if (!state.redirecting && (state.dirty.size || state.nodeDirty)) { event.preventDefault(); event.returnValue = ""; }
  });
  const pingApiInstaller = new URL("/tools/install-ping-api.sh", location.origin).href;
  $("pingApiInstallCommand").value = `curl -fsSL --retry 2 '${pingApiInstaller}' -o /tmp/cf-gtm-ping-api-install.sh && bash /tmp/cf-gtm-ping-api-install.sh`;
  $("copyPingApiCommand").addEventListener("click", async () => {
    const command = $("pingApiInstallCommand");
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard-unavailable");
      await navigator.clipboard.writeText(command.value);
      message("copyPingApiMsg", "安装命令已复制，请在国内服务器 root 终端执行。", "success");
    } catch {
      command.focus(); command.select();
      message("copyPingApiMsg", "已选中安装命令，请手动复制后在国内服务器执行。", "neutral");
    }
  });
  changeView(); syncInteractivity();
  if (!document.hidden) { state.ticker = window.setInterval(renderFreshness, 1000); void loadStatus(); }
})();
