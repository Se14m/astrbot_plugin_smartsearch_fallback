const state = {
  currentView: "overview",
  bridge: null,
  settings: {},
  engines: [],
  lastResult: null,
};

const titles = {
  overview: "总览",
  search: "搜索测试",
  engines: "引擎编排",
  settings: "插件设置",
};

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function showToast(message, error = false) {
  const toast = document.getElementById("toast");
  toast.querySelector("span").textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2800);
}

async function apiGet(path) {
  if (!state.bridge) throw new Error("当前页面尚未连接 AstrBot");
  return state.bridge.apiGet(path);
}

async function apiPost(path, body) {
  if (!state.bridge) throw new Error("当前页面尚未连接 AstrBot");
  return state.bridge.apiPost(path, body);
}

function setConnection(connected, message) {
  document.querySelectorAll(".connection-text").forEach((node) => { node.textContent = message; });
  document.querySelectorAll(".live-text").forEach((node) => { node.textContent = connected ? "运行正常" : "等待 AstrBot"; });
  document.querySelectorAll(".status-dot").forEach((node) => node.classList.toggle("offline", !connected));
  document.querySelectorAll(".lab-status").forEach((node) => {
    node.innerHTML = `<span class="status-dot${connected ? "" : " offline"}"></span>${connected ? "AstrBot API 已连接" : "等待 AstrBot Plugin Pages"}`;
  });
}

function setView(view, updateHash = true) {
  state.currentView = view;
  document.querySelectorAll("[data-view-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.viewPanel === view));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  document.getElementById("pageTitle").textContent = titles[view] || titles.overview;
  if (updateHash) window.location.hash = view;
  refreshIcons();
}

function wireNavigation() {
  document.querySelectorAll("[data-view], [data-navigate]").forEach((item) => {
    item.addEventListener("click", (event) => {
      event.preventDefault();
      setView(item.dataset.view || item.dataset.navigate);
    });
  });
  const initial = window.location.hash.replace("#", "");
  if (titles[initial]) setView(initial, false);
  window.addEventListener("hashchange", () => {
    const view = window.location.hash.replace("#", "");
    if (titles[view]) setView(view, false);
  });
}

function setToggle(toggle, value) {
  if (toggle) toggle.setAttribute("aria-checked", String(Boolean(value)));
}

function wireToggles() {
  document.querySelectorAll("[data-toggle]").forEach((toggle) => {
    toggle.addEventListener("click", () => setToggle(toggle, toggle.getAttribute("aria-checked") !== "true"));
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[character]));
}

function engineLabel(name) {
  return String(name || "").replace(/^./, (char) => char.toUpperCase());
}

function engineIcon(name) {
  const cls = String(name || "engine").replace(/[^a-z0-9_-]/gi, "").toLowerCase();
  return `<span class="engine-icon ${cls}">${escapeHtml(engineLabel(name).slice(0, 1) || "?")}</span>`;
}

function renderEngineOptions() {
  const selects = [document.getElementById("primaryEngine"), document.getElementById("fallbackEngine")];
  selects.forEach((select) => {
    if (!select) return;
    const settingKey = select.id === "primaryEngine" ? "primary_engine" : "fallback_engine";
    const current = String(state.settings[settingKey] || select.value || "").toLowerCase();
    select.innerHTML = state.engines.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(engineLabel(name))}</option>`).join("");
    if (state.engines.includes(current)) select.value = current;
  });
}

function renderOverview(stats) {
  const set = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = value; };
  set("versionValue", `v${stats.version || "1.6.5"}`);
  set("engineCount", stats.engine_count ?? state.engines.length);
  set("engineTotal", `/ ${stats.engine_count ?? state.engines.length}`);
  set("primaryValue", engineLabel(stats.primary_engine));
  set("fallbackValue", engineLabel(stats.fallback_engine));
  set("searchCount", stats.search_count ?? 0);
  set("llmValue", stats.llm_verifier_enabled ? "已启用" : "已关闭");
  set("overviewStatus", stats.engine_count ? "引擎注册正常" : "未发现引擎");
  set("strategyText", `${engineLabel(stats.primary_engine)} → ${engineLabel(stats.fallback_engine)}`);
  set("strategyTextAside", `${engineLabel(stats.primary_engine)} → ${engineLabel(stats.fallback_engine)}`);
  set("primaryEngineName", engineLabel(stats.primary_engine));
  set("fallbackEngineName", engineLabel(stats.fallback_engine));
  set("primaryFlowName", engineLabel(stats.primary_engine));
  set("fallbackFlowName", engineLabel(stats.fallback_engine));
  set("primaryFlowText", `优先返回 ${stats.primary_engine || "bocha"} 结果`);
  set("fallbackFlowText", `不足时调用 ${stats.fallback_engine || "anysearch"}`);
  set("qualityFlowText", `少于 ${stats.min_quality_results ?? "配置阈值"} 条时补足`);
  set("timeoutValue", stats.timeout ?? "—");
  set("totalTimeoutValue", stats.total_timeout ?? "—");
  set("versionValueLarge", stats.version || "1.6.5");
}

function renderHealth(checks) {
  const list = document.getElementById("healthList");
  if (!list) return;
  list.innerHTML = (checks || []).map((check) => `
    <div class="health-row">
      <div class="engine-name">${engineIcon(check.name)}<div><strong>${escapeHtml(engineLabel(check.name))}</strong><small>${escapeHtml(check.message || "")}</small></div></div>
      <span class="health-status ${check.ok ? "success" : "warning"}"><span class="status-dot"></span>${check.ok ? "可用" : "需配置"}</span>
      <span class="response-time muted">${check.ok ? "OK" : "--"}</span>
    </div>`).join("") || `<div class="empty-inline">尚未完成检查</div>`;
  refreshIcons();
}

function renderEngineTable() {
  const body = document.getElementById("engineTableBody");
  if (!body) return;
  const primary = state.settings.primary_engine;
  const fallback = state.settings.fallback_engine;
  body.innerHTML = state.engines.map((name) => {
    const role = name === primary ? "优先引擎" : name === fallback ? "兜底引擎" : "可选引擎";
    const configured = name === "bocha" ? state.settings.bocha_api_key_configured :
      ["anysearch", "serpapi"].includes(name) ? state.settings.fallback_api_key_configured : true;
    return `<tr><td><div class="table-engine">${engineIcon(name)}<strong>${escapeHtml(engineLabel(name))}</strong></div></td><td>${role}</td><td><span class="credential ${configured ? "masked" : "open"}">${configured ? "已配置 / 已隐藏" : "按引擎规则读取"}</span></td><td><span class="health-status ${configured ? "success" : "warning"}"><span class="status-dot"></span>${configured ? "可用" : "待检查"}</span></td><td><span class="response-time muted">--</span></td></tr>`;
  }).join("");
  const count = document.getElementById("engineTableCount");
  if (count) count.textContent = `共 ${state.engines.length} 个引擎，可在设置中调整优先级`;
  refreshIcons();
}

function renderSettings(settings) {
  state.settings = { ...settings };
  const values = {
    primaryEngine: settings.primary_engine,
    fallbackEngine: settings.fallback_engine,
    qualityRange: settings.min_quality_results,
    resultsRange: settings.max_results,
    timeoutInput: settings.timeout,
    totalTimeoutInput: settings.total_timeout,
    providerSelect: settings.llm_verifier_provider_id || "",
  };
  Object.entries(values).forEach(([id, value]) => { const node = document.getElementById(id); if (node && value !== undefined) node.value = value; });
  setToggle(document.querySelector('[data-toggle="llm-settings"]'), settings.llm_verifier_enabled);
  setToggle(document.querySelector('[data-toggle="llm"]'), settings.llm_verifier_enabled);
  updateOutputs();
  renderEngineOptions();
}

function updateOutputs() {
  const quality = document.getElementById("qualityRange");
  const results = document.getElementById("resultsRange");
  if (quality) document.getElementById("qualityOutput").textContent = `${quality.value} 条`;
  if (results) document.getElementById("resultsOutput").textContent = `${results.value} 条`;
}

async function loadData() {
  try {
    const [stats, settings, engineData] = await Promise.all([apiGet("stats"), apiGet("settings"), apiGet("engines")]);
    state.engines = engineData.engines || stats.engines || [];
    renderOverview({ ...stats, ...settings });
    renderSettings(settings);
    renderEngineTable();
    setConnection(true, "AstrBot API 已连接");
    try { renderHealth((await apiPost("health-check", {})).checks); } catch (error) { renderHealth([]); }
    document.getElementById("saveState").innerHTML = '<i data-lucide="check"></i>已读取';
    refreshIcons();
  } catch (error) {
    setConnection(false, "等待 AstrBot Plugin Pages");
    showToast(error.message || "无法读取插件数据", true);
  }
}

function wireSearch() {
  const input = document.getElementById("queryInput");
  const count = document.getElementById("charCount");
  const updateCount = () => { count.textContent = `${input.value.length} / 500`; };
  input.addEventListener("input", updateCount);
  updateCount();
  document.getElementById("runSearch").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const query = input.value.trim();
    if (!query) return showToast("请输入搜索内容", true);
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle" class="spin"></i>搜索中';
    refreshIcons();
    try {
      const result = await apiPost("search", { query, max_results: Number(document.getElementById("maxResults").value), freshness: document.getElementById("freshness").value });
      state.lastResult = result;
      renderResults(result);
      showToast(result.status === "ok" ? `搜索完成，返回 ${result.count || 0} 条结果` : "搜索完成，但未返回结果");
    } catch (error) {
      showToast(error.message || "搜索失败", true);
    } finally {
      button.disabled = false;
      button.innerHTML = '<i data-lucide="play"></i>运行搜索';
      refreshIcons();
    }
  });
  document.getElementById("copyResults").addEventListener("click", async () => {
    const result = state.lastResult;
    const groups = result?.results || {};
    const items = [...(groups.verified || []), ...(groups.primary_only || []), ...(groups.secondary_only || [])];
    if (!items.length) return showToast("暂无可复制的结果", true);
    const text = items.map((item) => `${item.title}\n${item.url}\n${item.snippet || ""}`).join("\n\n");
    try { await navigator.clipboard.writeText(text); showToast("结果已复制"); } catch { showToast("浏览器未授予剪贴板权限", true); }
  });
}

function renderResults(result) {
  const list = document.getElementById("resultList");
  const meta = document.getElementById("resultMeta");
  const groups = result?.results || {};
  const items = [...(groups.verified || []), ...(groups.primary_only || []), ...(groups.secondary_only || [])];
  meta.textContent = `${result?.engine || "未执行"} · ${items.length} 条结果${result?.llm_dropped ? ` · LLM 过滤 ${result.llm_dropped} 条` : ""}`;
  if (!items.length) {
    list.innerHTML = `<div class="empty-state"><span class="empty-icon"><i data-lucide="search-x"></i></span><strong>${escapeHtml(result?.text || "没有找到结果")}</strong><p>请检查引擎配置、API Key 和网络连接。</p></div>`;
    return refreshIcons();
  }
  list.innerHTML = items.map((item) => `<article class="result-card">${item.verified ? '<span class="result-badge">✓ 双引擎验证</span>' : ""}<h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.snippet || "")}</p><a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.url)}</a><div class="result-source">来源：${escapeHtml(item.source || "未知")}${item.llm_reason ? ` · ${escapeHtml(item.llm_reason)}` : ""}</div></article>`).join("");
  refreshIcons();
}

function collectSettings() {
  return {
    primary_engine: document.getElementById("primaryEngine").value,
    fallback_engine: document.getElementById("fallbackEngine").value,
    min_quality_results: Number(document.getElementById("qualityRange").value),
    max_results: Number(document.getElementById("resultsRange").value),
    timeout: Number(document.getElementById("timeoutInput").value),
    total_timeout: Number(document.getElementById("totalTimeoutInput").value),
    llm_verifier_enabled: document.querySelector('[data-toggle="llm-settings"]').getAttribute("aria-checked") === "true",
    llm_verifier_provider_id: document.getElementById("providerSelect").value.trim(),
  };
}

function wireSettings() {
  document.querySelectorAll("#qualityRange, #resultsRange").forEach((node) => node.addEventListener("input", updateOutputs));
  document.querySelector('[data-action="save"]').addEventListener("click", async () => {
    try {
      const settings = await apiPost("settings/save", collectSettings());
      renderSettings(settings);
      document.getElementById("saveState").innerHTML = '<i data-lucide="check"></i>刚刚保存';
      showToast("设置已保存");
      refreshIcons();
    } catch (error) { showToast(error.message || "保存失败", true); }
  });
  document.querySelector('[data-action="reset"]').addEventListener("click", () => renderSettings({ ...state.settings }));
}

function wireActions() {
  document.querySelectorAll('[data-action="refresh"]').forEach((button) => button.addEventListener("click", async () => {
    button.classList.add("refreshing");
    try { if (state.bridge) await loadData(); else showToast("当前页面尚未连接 AstrBot", true); }
    finally { window.setTimeout(() => button.classList.remove("refreshing"), 500); }
  }));
  document.querySelector('[data-action="help"]').addEventListener("click", () => showToast("请从 AstrBot 插件管理页打开本页面；旧版会继续使用原生配置页"));
  document.querySelector('[data-action="custom-engine"]').addEventListener("click", () => showToast("自定义引擎请放入插件 engines 目录后重载插件"));
}

async function initBridge() {
  if (!window.AstrBotPluginPage) {
    setConnection(false, "独立预览：等待 AstrBot");
    return;
  }
  try {
    state.bridge = window.AstrBotPluginPage;
    await state.bridge.ready();
    await loadData();
  } catch (error) {
    setConnection(false, "AstrBot 连接失败");
    showToast(error.message || "AstrBot 页面桥接失败", true);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  wireNavigation();
  wireToggles();
  wireSearch();
  wireSettings();
  wireActions();
  refreshIcons();
  initBridge();
});
