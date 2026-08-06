(() => {
  "use strict";

  const TOKEN_KEY = "tts_app_token";
  const SAMPLE_PAGE_SIZE = 16;
  const RECORDING_MIN_MS = 200;
  const RECORDING_MAX_MS = 30000;
  const ACTIVE_TASK_STATES = new Set(["queued", "running"]);
  const TERMINAL_TASK_STATES = new Set(["completed", "partial", "failed"]);
  const RETRYABLE_TASK_TYPES = new Set(["generate", "generate_batch", "augment", "export"]);
  const DEFAULT_PROJECT_SETTINGS = Object.freeze({
    display_text: "你好，小助手",
    label: "你好小助手",
    keyword_id: "ni_hao_xiao_zhu_shou",
    updated_at: "",
  });

  const state = {
    status: null,
    projectSettings: { ...DEFAULT_PROJECT_SETTINGS },
    providers: [],
    providerSettings: {},
    providerSettingsLoading: false,
    providerCapabilities: {},
    voices: [],
    tasks: [],
    taskGroups: [],
    samples: [],
    sampleKeywords: [],
    sampleKeywordInitialized: false,
    samplesTotal: 0,
    samplePage: 1,
    samplePages: 1,
    noises: [],
    exports: [],
    selectedVoices: new Set(),
    selectedSamples: new Set(),
    selectedNoises: new Set(),
    deletingExports: new Set(),
    taskStatus: "",
    audioUrls: new Map(),
    inlineAudio: null,
    inlineAudioId: null,
    noiseAudio: null,
    noiseAudioId: null,
    currentAnalysis: null,
    recording: {
      recorder: null,
      stream: null,
      audioContext: null,
      analyser: null,
      chunks: [],
      file: null,
      url: null,
      startedAt: 0,
      timer: null,
      levelFrame: null,
      discardOnStop: false,
    },
    pollTimer: null,
  };

  const $ = (id) => document.getElementById(id);
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
  const icon = (name, className = "") => `<svg class="${className}"><use href="#i-${name}"></use></svg>`;

  const providerNames = {
    volcengine: "火山引擎",
    aliyun: "阿里云",
    alibaba: "阿里云",
    mock: "本地 Mock",
    real: "真人录音",
    noise: "背景噪声",
  };
  const genderNames = { female: "女声", male: "男声", neutral: "中性", unknown: "未知" };
  const ageNames = { child: "儿童", teen: "少年", young: "青年", middle: "中年", senior: "老年", unknown: "未知" };
  const taskNames = { generate: "TTS 生成", generate_batch: "TTS 批量生成", augment: "噪声增强", materialize_noise: "背景噪声入库", export: "数据导出" };
  const statusNames = { queued: "排队中", running: "运行中", completed: "已完成", partial: "部分完成", failed: "失败" };
  const targetKindNames = { positive: "正样本", negative: "负样本", background: "背景噪声" };
  const qualityNames = {
    unchecked: "未检查", review: "待复核", pass: "已通过", reject: "已拒绝",
    pending: "待审核", accepted: "已通过", rejected: "已拒绝",
  };

  function token() {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  }

  function setToken(value) {
    if (value) sessionStorage.setItem(TOKEN_KEY, value);
    else sessionStorage.removeItem(TOKEN_KEY);
    updateTokenButton();
  }

  function updateTokenButton() {
    const button = $("accessTokenButton");
    if (!button) return;
    button.classList.toggle("primary", Boolean(token()));
    button.classList.toggle("secondary", !token());
    button.title = token()
      ? "访问令牌已为当前浏览器会话设置，点击可修改或清除"
      : "设置仅保存在当前浏览器会话中的访问令牌";
  }

  function askForToken(message = "服务器要求访问令牌。请输入 TTS_APP_API_TOKEN：") {
    const value = window.prompt(message, "");
    if (value === null) return false;
    setToken(value.trim());
    return Boolean(value.trim());
  }

  async function request(path, options = {}, allowTokenRetry = true) {
    const headers = new Headers(options.headers || {});
    if (path.startsWith("/api/") && token()) headers.set("x-app-token", token());
    if (options.body && !(options.body instanceof FormData) && !headers.has("content-type")) {
      headers.set("content-type", "application/json");
    }
    let response;
    try {
      response = await fetch(path, { ...options, headers });
    } catch (error) {
      throw new Error(`无法连接服务器：${error.message}`);
    }
    if (response.status === 401 && allowTokenRetry && askForToken()) {
      return request(path, options, false);
    }
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      const text = await response.text();
      try {
        const payload = JSON.parse(text);
        detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || payload);
      } catch (_) {
        if (text) detail = text.slice(0, 500);
      }
      if (response.status === 401) detail = "访问令牌错误或已失效";
      throw new Error(detail);
    }
    return response;
  }

  async function api(path, options = {}) {
    const response = await request(path, options);
    if (response.status === 204) return null;
    return response.json();
  }

  async function apiJson(path, method, body) {
    return api(path, { method, body: JSON.stringify(body) });
  }

  function toast(title, message = "", type = "info", timeout = 4200, action = null) {
    const element = document.createElement("div");
    element.className = `toast ${type}`;
    element.innerHTML = `<div class="toast-copy"><strong>${escapeHtml(title)}</strong>${message ? `<span>${escapeHtml(message)}</span>` : ""}</div>`;
    let actionButton = null;
    let timerId = null;
    let remaining = timeout;
    let startedAt = 0;
    let removed = false;
    const remove = () => {
      if (timerId !== null) window.clearTimeout(timerId);
      timerId = null;
      removed = true;
      element.remove();
    };
    const schedule = () => {
      if (removed || timerId !== null || remaining <= 0) return;
      startedAt = performance.now();
      timerId = window.setTimeout(remove, remaining);
    };
    const pause = () => {
      if (timerId === null) return;
      window.clearTimeout(timerId);
      timerId = null;
      remaining = Math.max(0, remaining - (performance.now() - startedAt));
    };
    const resume = () => schedule();
    if (action?.label && typeof action.handler === "function") {
      const button = document.createElement("button");
      actionButton = button;
      button.className = "toast-action";
      button.type = "button";
      button.textContent = action.label;
      button.addEventListener("click", async () => {
        pause();
        button.disabled = true;
        try { await action.handler(); } finally { remove(); }
      }, { once: true });
      element.append(button);
    }
    element.addEventListener("mouseenter", pause);
    element.addEventListener("mouseleave", resume);
    element.addEventListener("focusin", pause);
    element.addEventListener("focusout", (event) => {
      if (!element.contains(event.relatedTarget)) resume();
    });
    $("toastRegion").append(element);
    schedule();
    return { element, actionButton };
  }

  let confirmActive = false;

  function confirmAction(title, message, acceptText = "确认") {
    if (confirmActive) return Promise.resolve(false);
    confirmActive = true;
    return new Promise((resolve) => {
      const overlay = $("confirmOverlay");
      const cancelButton = $("confirmCancel");
      const acceptButton = $("confirmAccept");
      const previousFocus = document.activeElement;
      $("confirmTitle").textContent = title;
      $("confirmMessage").textContent = message;
      acceptButton.textContent = acceptText;
      overlay.classList.remove("hidden");
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        confirmActive = false;
        overlay.classList.add("hidden");
        cancelButton.removeEventListener("click", cancel);
        acceptButton.removeEventListener("click", accept);
        document.removeEventListener("keydown", onKeydown);
        if (previousFocus instanceof HTMLElement && previousFocus.isConnected && !previousFocus.matches(":disabled")) {
          previousFocus.focus({ preventScroll: true });
        }
        resolve(value);
      };
      const cancel = () => finish(false);
      const accept = () => finish(true);
      const onKeydown = (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          cancel();
          return;
        }
        if (event.key !== "Tab") return;
        if (event.shiftKey && document.activeElement === cancelButton) {
          event.preventDefault();
          acceptButton.focus();
        } else if (!event.shiftKey && document.activeElement === acceptButton) {
          event.preventDefault();
          cancelButton.focus();
        }
      };
      cancelButton.addEventListener("click", cancel);
      acceptButton.addEventListener("click", accept);
      document.addEventListener("keydown", onKeydown);
      window.requestAnimationFrame(() => cancelButton.focus({ preventScroll: true }));
    });
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(date);
  }

  function formatDuration(seconds) {
    const value = Number(seconds || 0);
    if (value < 60) return `${value.toFixed(value < 10 ? 2 : 1)} 秒`;
    const minutes = Math.floor(value / 60);
    const remainder = Math.round(value % 60);
    return `${minutes} 分 ${remainder} 秒`;
  }

  function navigate(view) {
    const target = $(`view-${view}`) || $("view-dashboard");
    document.querySelectorAll(".view").forEach((item) => item.classList.toggle("active", item === target));
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
    $("viewTitle").textContent = target.dataset.title || "概览";
    $("viewEyebrow").textContent = target.dataset.eyebrow || "数据集工作台";
    $("sidebar").classList.remove("open");
    if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function updateServerPill(online, textValue) {
    const pill = $("serverPill");
    pill.classList.toggle("online", online);
    pill.classList.toggle("offline", !online);
    pill.querySelector("span").textContent = textValue;
  }

  async function loadStatus() {
    try {
      state.status = await api("/api/status");
      updateServerPill(true, `服务正常 · 剩余 ${state.status.disk_free_gb ?? "—"} GB`);
      $("statVoices").textContent = state.status.voices ?? "—";
      $("statSamples").textContent = state.status.samples ?? "—";
      $("statAugmented").textContent = state.status.augmented_samples ?? "—";
      $("statRunning").textContent = state.status.running_tasks ?? "—";
      $("statSampleDuration").textContent = `总时长 ${formatDuration(state.status.sample_duration_seconds)}`;
      updateRunningBadge(Number(state.status.running_tasks || 0));
      return true;
    } catch (error) {
      updateServerPill(false, "服务器连接失败");
      toast("服务器不可用", error.message, "error", 7000);
      return false;
    }
  }

  function updateRunningBadge(count) {
    const badge = $("runningTaskBadge");
    badge.textContent = String(count);
    badge.classList.toggle("hidden", count <= 0);
  }

  function normalizeProjectSettings(value = {}) {
    return {
      display_text: String(value.display_text || DEFAULT_PROJECT_SETTINGS.display_text).trim(),
      label: String(value.label || DEFAULT_PROJECT_SETTINGS.label).trim(),
      keyword_id: String(value.keyword_id || DEFAULT_PROJECT_SETTINGS.keyword_id).trim(),
      updated_at: String(value.updated_at || ""),
    };
  }

  function renderProjectSummary() {
    $("targetWakeWord").textContent = state.projectSettings.display_text;
    $("targetKeywordId").textContent = state.projectSettings.keyword_id;
  }

  function fillProjectSettingsForm() {
    $("projectDisplayText").value = state.projectSettings.display_text;
    $("projectDerivedLabel").value = state.projectSettings.label;
    $("projectDerivedKeywordId").value = state.projectSettings.keyword_id;
    $("projectDerivedHint").textContent = state.projectSettings.updated_at
      ? `服务器配置已更新：${formatDate(state.projectSettings.updated_at)}`
      : "派生字段以服务器当前保存的配置为准。";
  }

  function projectGenerationTemplate(project = state.projectSettings) {
    const { display_text: displayText, label } = project;
    return [...new Set([
      displayText,
      label,
      `嗯，${displayText}`,
      `${label}，打开设备`,
    ].filter(Boolean))].join("\n");
  }

  function applyProjectDefaults({ previousProject = null, force = false } = {}) {
    const oldProject = previousProject || DEFAULT_PROJECT_SETTINGS;
    const replaceIfDefault = (element, oldDefault, nextDefault) => {
      if (force || element.value === oldDefault) element.value = nextDefault;
    };
    replaceIfDefault($("generationTexts"), projectGenerationTemplate(oldProject), projectGenerationTemplate());
    replaceIfDefault($("sampleUploadText"), oldProject.display_text, state.projectSettings.display_text);
    replaceIfDefault($("sampleUploadLabel"), oldProject.label, state.projectSettings.label);
    replaceIfDefault($("exportDatasetName"), `${oldProject.keyword_id}_v1`, `${state.projectSettings.keyword_id}_v1`);
    updateGenerationEstimate();
  }

  async function loadProjectSettings({ applyDefaults = false } = {}) {
    state.projectSettings = normalizeProjectSettings(await api("/api/settings/project"));
    renderProjectSummary();
    if (applyDefaults) applyProjectDefaults({ force: true });
    return state.projectSettings;
  }

  function openProjectSettings() {
    fillProjectSettingsForm();
    const dialog = $("projectSettingsDialog");
    if (!dialog.open) dialog.showModal();
    $("projectDisplayText").focus();
    $("projectDisplayText").select();
  }

  async function saveProjectSettings(event) {
    event.preventDefault();
    const displayText = $("projectDisplayText").value.trim();
    if (!displayText) return toast("请输入唤醒词", "显示唤醒词不能为空。", "error");
    const button = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    const previousProject = { ...state.projectSettings };
    try {
      const result = await apiJson("/api/settings/project", "PUT", { display_text: displayText });
      state.projectSettings = normalizeProjectSettings(result);
      renderProjectSummary();
      fillProjectSettingsForm();
      applyProjectDefaults({ previousProject });
      state.selectedSamples.clear();
      await loadSampleKeywords({ selectCurrent: true });
      await loadSamples(true);
      toast("唤醒词设置已保存", `后续新样本将使用“${state.projectSettings.display_text}”。`, "success");
    } catch (error) {
      toast("唤醒词设置保存失败", error.message, "error", 7000);
    } finally {
      button.disabled = false;
    }
  }

  async function loadProviders() {
    const [providers, capabilities] = await Promise.all([
      api("/api/providers"),
      api("/api/providers/capabilities").catch(() => ({})),
    ]);
    state.providers = Array.isArray(providers) ? providers : (providers?.items || providers?.providers || []);
    state.providerCapabilities = capabilities && typeof capabilities === "object" ? capabilities : {};
    state.providers.forEach((provider) => {
      const id = normalizeProviderId(provider.id || provider.provider);
      if (provider.capabilities && !state.providerCapabilities[id]) {
        state.providerCapabilities[id] = provider.capabilities;
      }
    });
    renderProviders();
    renderProviderAudioCapabilities();
  }

  function normalizeProviderId(value) {
    return String(value || "") === "alibaba" ? "aliyun" : String(value || "");
  }

  function capabilityForProvider(providerId) {
    return state.providerCapabilities[normalizeProviderId(providerId)] || {};
  }

  function normalizedCapabilityList(value, key = "value") {
    if (!Array.isArray(value)) return [];
    return value.map((item) => {
      if (item && typeof item === "object") return item[key] ?? item.value ?? item.id ?? item.name;
      return item;
    }).filter((item) => item !== undefined && item !== null && String(item).trim());
  }

  function replaceSelectOptions(selectId, values, formatter, preferred) {
    const select = $(selectId);
    if (!select || !values.length) return;
    const previous = select.value;
    select.innerHTML = values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(formatter(value))}</option>`).join("");
    const available = new Set(values.map(String));
    select.value = available.has(previous) ? previous : available.has(String(preferred)) ? String(preferred) : String(values[0]);
  }

  function renderProviderAudioCapabilities() {
    const formatName = (value) => String(value).replaceAll("_", " ").toUpperCase();
    const rateName = (value) => `${Number(value) / 1000} kHz`;
    [["aliyun", "aliyun"], ["volcengine", "volcengine"]].forEach(([providerId, prefix]) => {
      const capabilities = capabilityForProvider(providerId);
      const formats = normalizedCapabilityList(capabilities.audio_formats);
      const rates = normalizedCapabilityList(capabilities.sample_rates).map(Number).filter(Number.isFinite);
      replaceSelectOptions(`${prefix}AudioFormat`, formats, formatName, "wav");
      replaceSelectOptions(`${prefix}SampleRate`, rates, rateName, 16000);
      const formatSelect = $(`${prefix}AudioFormat`);
      const rateSelect = $(`${prefix}SampleRate`);
      if (formatSelect) formatSelect.title = formats.length ? `当前能力：${formats.map(formatName).join(" / ")}` : "未获取到格式能力，使用页面默认值";
      if (rateSelect) rateSelect.title = rates.length ? `当前能力：${rates.map(rateName).join(" / ")}` : "未获取到采样率能力，使用页面默认值";
    });
    updateRandomParameterState();
  }

  function secretDescription(secret) {
    const configured = Boolean(secret && (typeof secret === "object" ? secret.configured : secret));
    const masked = secret && typeof secret === "object" ? String(secret.masked || "").trim() : "";
    return {
      configured,
      text: configured ? `已配置${masked ? `（${masked}）` : ""}；留空保留现有值` : "未配置；保存后不会回填",
      placeholder: configured ? "已配置，留空保留现有值" : "尚未配置",
    };
  }

  function setInputValue(id, value, fallback = "") {
    const input = $(id);
    if (input) input.value = value === undefined || value === null || value === "" ? fallback : value;
  }

  function updateSecretField(inputId, stateId, secret) {
    const status = secretDescription(secret);
    const input = $(inputId);
    input.value = "";
    input.placeholder = status.placeholder;
    const stateElement = $(stateId);
    stateElement.textContent = status.text;
    stateElement.classList.toggle("configured", status.configured);
  }

  async function loadProviderSettings() {
    const payload = await api("/api/settings/providers");
    const providers = Array.isArray(payload) ? payload : (payload.providers || []);
    state.providerSettings = Object.fromEntries(providers.map((provider) => [normalizeProviderId(provider.id || provider.provider), provider]));
    renderProviderSettings();
  }

  function renderProviderSettings() {
    const aliyun = state.providerSettings.aliyun || {};
    const volcengine = state.providerSettings.volcengine || {};

    setInputValue("aliyunWorkspaceId", aliyun.workspace_id);
    setInputValue("aliyunRegion", "cn-beijing");
    setInputValue("aliyunEndpoint", aliyun.endpoint);
    setInputValue("aliyunCustomizationEndpoint", aliyun.customization_endpoint);
    setInputValue("aliyunPricingUnit", aliyun.pricing_unit);
    setInputValue("aliyunPricingRate", aliyun.pricing_rate);
    setInputValue("aliyunPricingCurrency", aliyun.pricing_currency);
    updateSecretField("aliyunApiKey", "aliyunApiKeyState", aliyun.api_key);

    setInputValue("volcengineAppId", volcengine.app_id);
    setInputValue("volcengineCluster", volcengine.cluster, "volcano_tts");
    setInputValue("volcengineEndpoint", volcengine.endpoint);
    setInputValue("volcengineAccessKeyId", volcengine.access_key_id);
    setInputValue("volcenginePricingUnit", volcengine.pricing_unit);
    setInputValue("volcenginePricingRate", volcengine.pricing_rate);
    setInputValue("volcenginePricingCurrency", volcengine.pricing_currency);
    updateSecretField("volcengineAccessToken", "volcengineAccessTokenState", volcengine.access_token);
    updateSecretField("volcengineAccessKeySecret", "volcengineAccessKeySecretState", volcengine.access_key_secret);

    const aliyunBadge = $("aliyunSettingsState");
    const aliyunConfigured = Boolean(aliyun.configured || aliyun.synthesis_configured);
    aliyunBadge.textContent = aliyunConfigured ? "已配置" : "未配置";
    aliyunBadge.classList.toggle("ready", aliyunConfigured);

    const volcengineBadge = $("volcengineSettingsState");
    const synthesisConfigured = Boolean(volcengine.synthesis_configured || volcengine.configured);
    const catalogConfigured = Boolean(volcengine.catalog_configured);
    volcengineBadge.textContent = synthesisConfigured && catalogConfigured
      ? "合成 + 目录"
      : synthesisConfigured ? "合成已配置" : catalogConfigured ? "目录已配置" : "未配置";
    volcengineBadge.classList.toggle("ready", synthesisConfigured || catalogConfigured);
  }

  function selectProviderSettingsTab(providerId) {
    const selected = normalizeProviderId(providerId) === "volcengine" ? "volcengine" : "aliyun";
    document.querySelectorAll("[data-provider-settings-tab]").forEach((button) => {
      const active = button.dataset.providerSettingsTab === selected;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll("[data-provider-settings-panel]").forEach((panel) => {
      const active = panel.dataset.providerSettingsPanel === selected;
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    });
  }

  function mountProviderSettingsPage() {
    const mount = $("providerSettingsPageMount");
    const dialog = $("providerSettingsDialog");
    const content = dialog?.querySelector(".provider-settings-content");
    if (!mount || !content || mount.contains(content)) return;
    mount.replaceChildren(content);
    dialog.remove();
  }

  async function openProviderSettings(providerId = "aliyun") {
    mountProviderSettingsPage();
    navigate("providers");
    if (state.providerSettingsLoading) return;
    state.providerSettingsLoading = true;
    const triggers = document.querySelectorAll("#providerSettingsButton, #reloadProviderSettings, [data-config-provider]");
    triggers.forEach((button) => { button.disabled = true; });
    try {
      await loadProviderSettings();
      selectProviderSettingsTab(providerId);
    } catch (error) {
      toast("供应商设置读取失败", error.message, "error", 7000);
    } finally {
      state.providerSettingsLoading = false;
      triggers.forEach((button) => { button.disabled = false; });
    }
  }

  function providerPricingPayload(prefix) {
    const unit = $(`${prefix}PricingUnit`).value;
    const rateText = $(`${prefix}PricingRate`).value.trim();
    const currency = $(`${prefix}PricingCurrency`).value.trim().toUpperCase();
    if (!unit && !rateText && !currency) {
      return { pricing_unit: null, pricing_rate: null, pricing_currency: null };
    }
    if (!unit || !rateText || !currency) {
      throw new Error("预估费用的计费单位、单价和币种必须同时填写；如果不使用，请全部留空。");
    }
    const rate = Number(rateText);
    if (!Number.isFinite(rate) || rate < 0) throw new Error("预估费用单价必须是非负有限数值。");
    return { pricing_unit: unit, pricing_rate: rate, pricing_currency: currency };
  }

  function providerSettingsPayload(providerId) {
    if (providerId === "aliyun") {
      const payload = {
        workspace_id: $("aliyunWorkspaceId").value.trim(),
        region: $("aliyunRegion").value,
        endpoint: $("aliyunEndpoint").value.trim(),
        customization_endpoint: $("aliyunCustomizationEndpoint").value.trim(),
        ...providerPricingPayload("aliyun"),
      };
      const apiKey = $("aliyunApiKey").value.trim();
      if (apiKey) payload.api_key = apiKey;
      return payload;
    }

    const payload = {
      app_id: $("volcengineAppId").value.trim(),
      cluster: $("volcengineCluster").value.trim() || "volcano_tts",
      endpoint: $("volcengineEndpoint").value.trim(),
      access_key_id: $("volcengineAccessKeyId").value.trim(),
      ...providerPricingPayload("volcengine"),
    };
    const accessToken = $("volcengineAccessToken").value.trim();
    const accessKeySecret = $("volcengineAccessKeySecret").value.trim();
    if (accessToken) payload.access_token = accessToken;
    if (accessKeySecret) payload.access_key_secret = accessKeySecret;
    return payload;
  }

  async function saveProviderSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const providerId = form.dataset.providerSettingsForm;
    const submitButton = form.querySelector('button[type="submit"]');
    submitButton.disabled = true;
    try {
      await apiJson(`/api/settings/providers/${providerId}`, "PUT", providerSettingsPayload(providerId));
    } catch (error) {
      toast("供应商设置保存失败", error.message, "error", 7000);
      submitButton.disabled = false;
      return;
    }
    toast(`${providerNames[providerId]}设置已保存`, "服务器不会向浏览器回传密钥明文，正在更新音色目录。", "success");
    try {
      await Promise.all([loadProviderSettings(), loadProviders()]);
    } catch (error) {
      toast("配置已保存，但页面状态刷新失败", error.message, "error", 7000);
    }
    await refreshVoices(providerId, true);
    submitButton.disabled = false;
  }

  async function testProviderSettings(providerId, button) {
    button.disabled = true;
    try {
      const result = await api(`/api/settings/providers/${providerId}/test`, { method: "POST" });
      const ok = result?.ok !== false;
      toast(ok ? `${providerNames[providerId]}配置检查通过` : `${providerNames[providerId]}配置不完整`, result?.message || result?.detail || "本地字段检查已完成；此操作不会发起计费合成。", ok ? "success" : "error", 6500);
    } catch (error) {
      toast("配置检查失败", error.message, "error", 6500);
    } finally {
      button.disabled = false;
    }
  }

  async function clearProviderSettings(providerId, button) {
    const confirmed = await confirmAction(
      `清空${providerNames[providerId]}配置`,
      "将从当前服务器删除该供应商的所有账号信息、密钥和预估费用配置。内置回退音色仍会保留。",
      "清空配置",
    );
    if (!confirmed) return;
    button.disabled = true;
    try {
      await api(`/api/settings/providers/${providerId}`, { method: "DELETE" });
      toast(`${providerNames[providerId]}配置已清空`, "后续目录查询不会再访问该账号。", "success");
      await Promise.all([loadProviderSettings(), loadProviders()]);
      await refreshVoices(providerId);
    } catch (error) {
      toast("供应商配置清空失败", error.message, "error", 7000);
    } finally {
      button.disabled = false;
    }
  }

  function renderProviders() {
    if (!state.providers.length) {
      $("providerCards").innerHTML = '<div class="empty-inline">尚未注册 TTS 服务</div>';
      return;
    }
    $("providerCards").innerHTML = state.providers.map((provider) => {
      const id = String(provider.id || provider.provider || "unknown");
      const synthesisConfigured = Boolean(
        provider.synthesis_configured ?? provider.configured ?? provider.ready ?? provider.available,
      );
      const catalogConfigured = Boolean(provider.catalog_configured);
      const configured = synthesisConfigured || catalogConfigured;
      const configurationLabel = id === "volcengine"
        ? (synthesisConfigured && catalogConfigured
          ? "合成 + 目录"
          : synthesisConfigured ? "合成已配置" : catalogConfigured ? "目录已配置" : "未配置")
        : configured ? "已配置" : "未配置";
      const count = state.voices.filter((voice) => voice.provider === id || (id === "alibaba" && voice.provider === "aliyun")).length;
      const reason = provider.detail || provider.message || provider.reason || provider.description || `${count} 个已同步音色`;
      const settingsId = normalizeProviderId(id);
      return `<div class="provider-card">
        <div class="provider-logo ${escapeHtml(id)}">${escapeHtml((providerNames[id] || id).slice(0, 1))}</div>
        <div class="provider-copy"><strong>${escapeHtml(provider.name || providerNames[id] || id)}</strong><small title="${escapeHtml(reason)}">${escapeHtml(reason)}</small></div>
        <span class="provider-state ${configured ? "ready" : ""}">${configurationLabel}</span>
        ${["aliyun", "volcengine"].includes(settingsId) ? `<button type="button" class="provider-config-button" data-config-provider="${escapeHtml(settingsId)}">配置</button>` : ""}
      </div>`;
    }).join("");
  }

  async function loadVoices() {
    state.voices = await api("/api/voices");
    const validIds = new Set(state.voices.map((voice) => Number(voice.id)));
    state.selectedVoices.forEach((id) => { if (!validIds.has(id)) state.selectedVoices.delete(id); });
    renderVoices();
    renderProviders();
  }

  function filteredVoices() {
    const search = $("voiceSearch").value.trim().toLowerCase();
    const provider = $("voiceProviderFilter").value;
    const gender = $("voiceGenderFilter").value;
    const age = $("voiceAgeFilter").value;
    return state.voices.filter((voice) => {
      const haystack = `${voice.name} ${voice.voice_id} ${voice.model}`.toLowerCase();
      return (!search || haystack.includes(search))
        && (!provider || voice.provider === provider || (provider === "aliyun" && voice.provider === "alibaba"))
        && (!gender || voice.gender === gender)
        && (!age || voice.age_group === age);
    });
  }

  function renderVoices() {
    const voices = filteredVoices();
    if (!voices.length) {
      $("voiceGrid").innerHTML = '<div class="empty-state"><p>没有符合条件的音色</p><span>可清空筛选或查询最新音色。</span></div>';
    } else {
      $("voiceGrid").innerHTML = voices.map((voice) => {
        const id = Number(voice.id);
        const selected = state.selectedVoices.has(id);
        const provider = providerNames[voice.provider] || voice.provider;
        return `<label class="voice-card ${selected ? "selected" : ""}" data-voice-id="${id}">
          <input type="checkbox" ${selected ? "checked" : ""} aria-label="选择${escapeHtml(voice.name)}" />
          <span class="voice-icon">${voice.gender === "male" ? "男" : voice.gender === "female" ? "女" : "声"}</span>
          <strong title="${escapeHtml(voice.name)}">${escapeHtml(voice.name)}</strong>
          <code title="${escapeHtml(voice.voice_id)}">${escapeHtml(voice.voice_id)}</code>
          <span class="tag-row"><i class="tag provider-tag">${escapeHtml(provider)}</i><i class="tag">${escapeHtml(genderNames[voice.gender] || "未知")}</i><i class="tag">${escapeHtml(ageNames[voice.age_group] || "未知")}</i></span>
        </label>`;
      }).join("");
    }
    $("selectedVoiceCount").textContent = String(state.selectedVoices.size);
    updateRandomParameterState();
    updateGenerationEstimate();
  }

  function generationTexts() {
    return $("generationTexts").value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  }

  function updateGenerationEstimate() {
    const total = generationTexts().length * state.selectedVoices.size * Math.max(0, Number($("generationCount").value || 0));
    $("generationEstimate").querySelector("strong").textContent = total.toLocaleString("zh-CN");
  }

  function selectedHasVolcengine() {
    return state.voices.some(
      (voice) => voice.provider === "volcengine" && state.selectedVoices.has(Number(voice.id)),
    );
  }

  function selectedHasUnsupportedCapability(name) {
    return state.voices.some((voice) => (
      state.selectedVoices.has(Number(voice.id))
      && capabilityForProvider(voice.provider)[name] === false
    ));
  }

  function setRangeEnabled(baseId, toggleId, minId, maxId, enabled = true) {
    const randomized = enabled && $(toggleId).checked;
    $(toggleId).disabled = !enabled;
    $(baseId).disabled = !enabled || randomized;
    $(minId).disabled = !randomized;
    $(maxId).disabled = !randomized;
    $(toggleId).closest(".random-parameter")?.classList.toggle("disabled", !enabled);
  }

  function updateRandomParameterState() {
    const speedSupported = !selectedHasUnsupportedCapability("speed");
    const volumeSupported = !selectedHasUnsupportedCapability("volume");
    if (!speedSupported) {
      $("randomSpeed").checked = false;
      $("generationSpeed").value = "1";
      $("speedOutput").textContent = "1.00×";
      $("generationSpeed").title = "当前选中的音色包含不支持语速调节的供应商";
    } else {
      $("generationSpeed").removeAttribute("title");
    }
    if (!volumeSupported) {
      $("randomVolume").checked = false;
      $("generationVolume").value = "50";
      $("volumeOutput").textContent = "50 / 100";
      $("generationVolume").title = "当前选中的音色包含不支持音量调节的供应商";
    } else {
      $("generationVolume").removeAttribute("title");
    }
    setRangeEnabled("generationSpeed", "randomSpeed", "speedMin", "speedMax", speedSupported);
    setRangeEnabled("generationVolume", "randomVolume", "volumeMin", "volumeMax", volumeSupported);
    const pitchUnsupported = selectedHasUnsupportedCapability("pitch") || selectedHasVolcengine();
    if (pitchUnsupported) {
      $("randomPitch").checked = false;
      $("generationPitch").value = "0";
      $("pitchOutput").textContent = "0 st";
      $("generationPitch").title = "当前选中的音色包含不支持音高调节的供应商";
    } else {
      $("generationPitch").removeAttribute("title");
    }
    setRangeEnabled("generationPitch", "randomPitch", "pitchMin", "pitchMax", !pitchUnsupported);
    $("volcPitchHint").classList.toggle("hidden", !pitchUnsupported);
  }

  function validatedRange(minId, maxId, label) {
    const min = Number($(minId).value);
    const max = Number($(maxId).value);
    if (!Number.isFinite(min) || !Number.isFinite(max)) throw new Error(`${label}范围必须是数字`);
    if (min > max) throw new Error(`${label}最小值不能大于最大值`);
    return [min, max];
  }

  const semitoneToRatio = (value) => clamp(2 ** (Number(value) / 12), 0.5, 2);
  const providerVolumeLevel = (value) => Math.round(clamp(Number(value), 0, 100));

  async function refreshVoices(provider = null, afterSettingsSave = false) {
    const buttons = [$("refreshVoiceButton"), $("refreshAllVoices")];
    buttons.forEach((button) => { button.disabled = true; });
    try {
      const result = await apiJson("/api/voices/refresh", "POST", provider ? { provider } : {});
      await Promise.all([loadVoices(), loadStatus()]);
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      toast(
        warnings.length ? "音色目录部分更新" : "音色目录已更新",
        `同步 ${result.count ?? 0} 个音色${warnings.length ? `；${warnings[0]}` : ""}`,
        warnings.length ? "info" : "success",
        warnings.length ? 8000 : 4200,
      );
    } catch (error) {
      toast(afterSettingsSave ? "配置已保存，但音色刷新失败" : "音色查询失败", error.message, "error", 6500);
    } finally {
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  async function runWithLimit(items, limit, worker) {
    const queue = [...items];
    const results = [];
    const runners = Array.from({ length: Math.min(limit, queue.length) }, async () => {
      while (queue.length) {
        const item = queue.shift();
        results.push(await worker(item));
      }
    });
    await Promise.all(runners);
    return results;
  }

  async function submitGeneration(event) {
    event.preventDefault();
    const project = state.projectSettings;
    const texts = generationTexts();
    const voiceIds = [...state.selectedVoices];
    const count = Number($("generationCount").value);
    if (!texts.length) return toast("请输入合成文本", "至少保留一条非空文本。", "error");
    if (!voiceIds.length) return toast("请选择音色", "建议先选少量音色试听，再扩大批次。", "error");
    if (!Number.isInteger(count) || count < 1) return toast("数量无效", "每条文本 / 每音色数量必须大于 0。", "error");
    const total = texts.length * voiceIds.length * count;
    const confirmed = await confirmAction(
      "确认创建批量生成任务",
      `将创建 1 个总任务和 ${texts.length} 个文本子任务，预计调用合成接口 ${total} 次。\n页面金额仅是预估费用，以厂商账单为准。建议先用 1 个音色 × 1 条样本验证。`,
      `生成 ${total} 条`,
    );
    if (!confirmed) return;
    const button = $("startGeneration");
    button.disabled = true;
    button.textContent = "正在创建任务…";
    try {
      const baseSeed = Number($("generationSeed").value || 20260804);
      const concurrency = Math.round(clamp(Number($("generationConcurrency").value || 3), 1, 16));
      const [speedMin, speedMax] = validatedRange("speedMin", "speedMax", "语速");
      const [pitchMinSt, pitchMaxSt] = validatedRange("pitchMin", "pitchMax", "音高");
      const [volumeMinLevel, volumeMaxLevel] = validatedRange("volumeMin", "volumeMax", "供应商音量级");
      const pitchUnsupported = selectedHasUnsupportedCapability("pitch") || selectedHasVolcengine();
      const result = await apiJson("/api/tasks/generate-batch", "POST", {
        texts,
        label: project.label,
        keyword_id: project.keyword_id,
        target_kind: $("generationTargetKind").value,
        voice_ids: voiceIds,
        count,
        concurrency,
        speed: Number($("generationSpeed").value),
        pitch: pitchUnsupported ? 1 : semitoneToRatio($("generationPitch").value),
        volume: providerVolumeLevel($("generationVolume").value),
        seed: baseSeed,
        instruction: $("generationInstruction").value.trim(),
        random_speed: $("randomSpeed").checked,
        speed_min: speedMin,
        speed_max: speedMax,
        random_pitch: !pitchUnsupported && $("randomPitch").checked,
        pitch_min: pitchUnsupported ? 1 : semitoneToRatio(pitchMinSt),
        pitch_max: pitchUnsupported ? 1 : semitoneToRatio(pitchMaxSt),
        random_volume: $("randomVolume").checked,
        volume_min: providerVolumeLevel(volumeMinLevel),
        volume_max: providerVolumeLevel(volumeMaxLevel),
        provider_audio: {
          aliyun: { format: $("aliyunAudioFormat").value, sample_rate: Number($("aliyunSampleRate").value) },
          volcengine: { format: $("volcengineAudioFormat").value, sample_rate: Number($("volcengineSampleRate").value) },
        },
        cost_acknowledged: true,
      });
      const parentId = result?.parent_id || result?.id || "";
      const children = result?.child_ids || result?.children || [];
      toast("生成批次已创建", `总任务 ${String(parentId).slice(0, 12) || "已入队"}，${Array.isArray(children) ? children.length : texts.length} 个子任务已由后端调度。`, "success");
      await Promise.all([loadTasks(), loadStatus()]);
      navigate("tasks");
    } catch (error) {
      toast("任务创建失败", error.message, "error", 7000);
    } finally {
      button.disabled = false;
      button.innerHTML = `${icon("spark")}创建生成任务`;
    }
  }

  async function loadTasks(silent = false) {
    try {
      const payload = await api("/api/tasks?limit=500");
      state.tasks = Array.isArray(payload) ? payload : (payload.items || payload.tasks || []);
      state.taskGroups = groupTasks(state.tasks);
      renderTasks();
      renderSampleJobOptions();
      const running = state.tasks.filter((task) => ACTIVE_TASK_STATES.has(task.status)).length;
      updateRunningBadge(running);
      $("statRunning").textContent = String(running);
      const terminal = state.taskGroups.filter((task) => TERMINAL_TASK_STATES.has(task.status));
      const successful = terminal.filter((task) => task.status === "completed").length;
      $("statSuccessRate").textContent = terminal.length ? `近期成功率 ${Math.round(successful / terminal.length * 100)}%` : "暂无已完成任务";
      if (state.tasks.some((task) => task.type === "export" && task.status === "completed")) loadExports(true);
    } catch (error) {
      if (!silent) toast("任务读取失败", error.message, "error");
    }
  }

  function aggregateStatus(children) {
    if (children.some((child) => child.status === "running")) return "running";
    if (children.some((child) => child.status === "queued")) return "queued";
    if (children.every((child) => child.status === "completed")) return "completed";
    if (children.some((child) => child.status === "completed" || child.status === "partial")) return "partial";
    return children.length ? "failed" : "queued";
  }

  function hasEstimatedCost(task) {
    const cost = taskCostInfo(task);
    return cost.available === true && Number.isFinite(cost.amount);
  }

  function taskCostInfo(task) {
    const result = task?.result && typeof task.result === "object" ? task.result : {};
    const available = result.cost_available ?? task?.cost_available ?? null;
    const rawAmount = result.estimated_cost ?? task?.estimated_cost;
    const amount = rawAmount === null || rawAmount === undefined || rawAmount === "" ? NaN : Number(rawAmount);
    const rawByCurrency = result.cost_by_currency ?? task?.cost_by_currency;
    const byCurrency = rawByCurrency && typeof rawByCurrency === "object"
      ? Object.entries(rawByCurrency).map(([currency, value]) => [String(currency).toUpperCase(), Number(value)])
        .filter(([currency, value]) => currency && Number.isFinite(value))
      : [];
    return {
      available: available === true,
      amount,
      currency: String(result.currency ?? task?.currency ?? "").toUpperCase(),
      byCurrency,
    };
  }

  function groupTasks(tasks) {
    const rows = tasks.map((task) => ({ ...task }));
    const byId = new Map(rows.map((task) => [String(task.id), task]));
    const parentMap = new Map();
    const childIds = new Set();

    rows.forEach((task) => {
      if (Array.isArray(task.children) || task.type === "generate_batch" || task.is_parent) {
        parentMap.set(String(task.id), task);
      }
    });
    rows.forEach((task) => {
      if (!task.parent_id) return;
      const parentId = String(task.parent_id);
      if (!parentMap.has(parentId)) {
        parentMap.set(parentId, byId.get(parentId) || {
          id: parentId,
          type: "generate_batch",
          status: "queued",
          created_at: task.created_at,
          message: "批量生成任务",
          synthetic_parent: true,
        });
      }
    });

    parentMap.forEach((parent, parentId) => {
      const embedded = Array.isArray(parent.children) ? parent.children : [];
      const children = embedded.map((child) => {
        if (child && typeof child === "object") return { ...child, parent_id: child.parent_id || parentId };
        return byId.get(String(child)) || { id: String(child), parent_id: parentId, type: "generate", status: "queued" };
      });
      rows.filter((task) => String(task.parent_id || "") === parentId).forEach((child) => {
        if (!children.some((item) => String(item.id) === String(child.id))) children.push(child);
      });
      children.forEach((child) => childIds.add(String(child.id)));
      parent._children = children;
      if (children.length) {
        const sum = (key) => children.reduce((total, child) => total + Math.max(0, Number(child[key] || 0)), 0);
        if (parent.synthetic_parent || !parent.status) parent.status = aggregateStatus(children);
        parent.total = Number(parent.total || 0) || sum("total");
        parent.completed = Number(parent.completed || 0) || sum("completed");
        parent.failed = Number(parent.failed || 0) || sum("failed");
        if (!hasEstimatedCost(parent)) {
          const costs = children.filter(hasEstimatedCost).map((child) => Number(child.estimated_cost));
          if (costs.length) parent.estimated_cost = costs.reduce((total, value) => total + value, 0);
        }
        const childCosts = children.map(taskCostInfo);
        parent.cost_available = parent.cost_available ?? (childCosts.length > 0 && childCosts.every((cost) => cost.available));
        parent.currency = parent.currency || children.find((child) => child.currency)?.currency;
      }
    });

    const grouped = [...parentMap.values()];
    rows.forEach((task) => {
      const id = String(task.id);
      if (task.parent_id || childIds.has(id) || parentMap.has(id)) return;
      grouped.push(task);
    });
    return grouped.sort((left, right) => String(right.created_at || "").localeCompare(String(left.created_at || "")));
  }

  function taskTiming(task) {
    const result = task.result && typeof task.result === "object" ? task.result : {};
    const explicit = (...values) => {
      const value = values.find((item) => item !== null && item !== undefined && item !== "");
      if (value === undefined) return null;
      const number = Number(value);
      return Number.isFinite(number) && number >= 0 ? number : null;
    };
    return {
      queue: explicit(task.queue_duration_seconds, result.queue_duration_seconds),
      execution: explicit(task.execution_duration_seconds, task.duration_seconds, result.execution_duration_seconds, result.duration_seconds),
      total: explicit(task.total_duration_seconds, result.total_duration_seconds),
    };
  }

  function formatEstimatedCost(task) {
    const result = task?.result && typeof task.result === "object" ? task.result : {};
    const disclaimer = String(result.cost_disclaimer ?? task?.cost_disclaimer ?? "");
    if (disclaimer.includes("本地操作") && disclaimer.includes("不产生云费用")) {
      return "本地操作 · 不产生云费用";
    }
    const cost = taskCostInfo(task);
    if (cost.byCurrency.length > 1) {
      const parts = cost.byCurrency.map(([currency, amount]) => `${currency} ${amount.toFixed(amount < 0.01 ? 4 : 2)}`);
      return `${parts.join(" + ")} （分币种预估，不能直接汇总） · 预估费用，以厂商账单为准`;
    }
    if (!cost.available || !Number.isFinite(cost.amount)) return "未配置 · 预估费用，以厂商账单为准";
    const { amount, currency } = cost;
    const prefix = currency === "CNY" ? "¥" : currency === "USD" ? "$" : currency ? `${currency} ` : "";
    return `${prefix}${amount.toFixed(amount < 0.01 ? 4 : 2)} · 预估费用，以厂商账单为准`;
  }

  function taskTitle(task, isParent = false) {
    if (isParent) return `批量生成 · ${task._children?.length || 0} 个文本子任务`;
    const text = task.text || task.request?.text || task.payload?.text;
    return text ? `${taskNames[task.type] || task.type} · ${text}` : (taskNames[task.type] || task.type || "任务");
  }

  function taskHtml(task, { child = false, parent = false } = {}) {
    const total = Math.max(0, Number(task.total || 0));
    const done = Math.max(0, Number(task.completed || 0));
    const failed = Math.max(0, Number(task.failed || 0));
    const progress = total ? clamp((done + failed) / total * 100, 0, 100) : (task.status === "completed" ? 100 : 0);
    const errors = Array.isArray(task.result?.errors) ? task.result.errors.filter(Boolean) : [];
    const errorDetails = errors.length
      ? `<details class="task-errors"><summary>查看错误详情（${errors.length}）</summary><ul>${errors.map((error) => `<li>${escapeHtml(error)}</li>`).join("")}</ul></details>`
      : "";
    const timing = taskTiming(task);
    const result = task.result && typeof task.result === "object" ? task.result : {};
    const payload = task.payload && typeof task.payload === "object" ? task.payload : {};
    const requestedConcurrency = Number(result.requested_concurrency ?? task.requested_concurrency ?? payload.concurrency ?? task.concurrency ?? 0);
    const appliedConcurrency = Number(result.applied_concurrency ?? task.applied_concurrency ?? payload.applied_concurrency ?? 0);
    const activeChildren = task._children?.filter((item) => item.status === "running").length || 0;
    const timingLabel = `排队 ${timing.queue === null ? "—" : formatDuration(timing.queue)} · 执行 ${timing.execution === null ? "—" : formatDuration(timing.execution)} · 总耗时 ${timing.total === null ? "—" : formatDuration(timing.total)}`;
    const concurrencyParts = parent ? [
      `当前运行子任务 ${activeChildren}`,
      requestedConcurrency ? `请求并发 ${requestedConcurrency}` : "",
      appliedConcurrency ? `后端实际并发上限 ${appliedConcurrency}` : "",
    ].filter(Boolean) : [];
    const concurrencyLabel = concurrencyParts.join(" · ");
    const retryable = ["failed", "partial"].includes(task.status) && RETRYABLE_TASK_TYPES.has(task.type);
    return `<div class="task-item ${child ? "task-child" : ""}">
      <div class="task-main"><strong>${escapeHtml(taskTitle(task, parent))}</strong><small>${escapeHtml(String(task.id || "").slice(0, 12))} · ${escapeHtml(formatDate(task.created_at))}</small><span class="task-message" title="${escapeHtml(task.message)}">${escapeHtml(task.message || "等待处理")}</span><div class="task-labels"><span class="task-label task-timing">${escapeHtml(timingLabel)}</span><span class="task-label task-cost">${escapeHtml(formatEstimatedCost(task))}</span>${concurrencyLabel ? `<span class="task-label">${escapeHtml(concurrencyLabel)}</span>` : ""}</div>${errorDetails}</div>
      <div class="task-progress"><div class="progress-track"><i style="width:${progress.toFixed(1)}%"></i></div><div class="progress-meta"><span>${done} 完成 · ${failed} 失败</span><b>${progress.toFixed(0)}%</b></div></div>
      <div class="task-actions"><span class="status-badge ${escapeHtml(task.status)}">${escapeHtml(statusNames[task.status] || task.status)}</span>${retryable ? `<button class="button quiet compact" data-retry-task="${escapeHtml(task.id)}">重试</button>` : ""}</div>
    </div>`;
  }

  function taskGroupHtml(task, compact = false) {
    const children = task._children || [];
    if (!children.length || compact) return taskHtml(task, { parent: Boolean(children) });
    return `<article class="task-group">${taskHtml(task, { parent: true })}<details ${ACTIVE_TASK_STATES.has(task.status) ? "open" : ""}><summary>查看 ${children.length} 个文本子任务</summary><div class="task-children">${children.map((child) => taskHtml(child, { child: true })).join("")}</div></details></article>`;
  }

  function renderTasks() {
    const filtered = state.taskStatus ? state.taskGroups.filter((task) => {
      if (state.taskStatus === "running") return ACTIVE_TASK_STATES.has(task.status);
      return task.status === state.taskStatus;
    }) : state.taskGroups;
    $("taskList").innerHTML = filtered.length ? filtered.map((task) => taskGroupHtml(task)).join("") : '<div class="empty-state"><p>当前筛选下没有任务</p></div>';
    $("dashboardTasks").innerHTML = state.taskGroups.length ? state.taskGroups.slice(0, 5).map((task) => taskGroupHtml(task, true)).join("") : '<div class="empty-inline">还没有任务，先创建一批小样吧。</div>';
  }

  async function retryTask(button) {
    const taskId = button.dataset.retryTask;
    const task = state.tasks.find((item) => String(item.id) === taskId)
      || state.taskGroups.find((item) => String(item.id) === taskId)
      || state.taskGroups.flatMap((item) => item._children || []).find((item) => String(item.id) === taskId);
    const billable = ["generate", "generate_batch"].includes(task?.type);
    if (billable && !await confirmAction(
      "确认重试云端合成任务",
      "重试会创建新任务并可能再次调用云端 TTS。预估费用，以厂商账单为准。",
      "确认重试",
    )) return;
    button.disabled = true;
    try {
      if (billable) {
        await apiJson(`/api/tasks/${encodeURIComponent(taskId)}/retry`, "POST", { cost_acknowledged: true });
      } else {
        await apiJson(`/api/tasks/${encodeURIComponent(taskId)}/retry`, "POST", {});
      }
      await loadTasks();
      toast("重试任务已创建", "旧任务保留，新任务已进入队列。", "success");
    } catch (error) {
      toast("重试失败", error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  function renderSampleJobOptions() {
    const select = $("sampleJobFilter");
    if (!select) return;
    const previous = select.value;
    const parents = state.taskGroups.filter((task) => (
      task.type === "generate_batch" || task.is_parent || (task._children && task._children.length)
    ));
    select.innerHTML = '<option value="">全部任务</option>' + parents.map((task) => {
      const childCount = task._children?.length || task.child_count || 0;
      const title = task.name || task.batch_name || String(task.id || "").slice(0, 12);
      return `<option value="${escapeHtml(task.id)}">${escapeHtml(title)} · ${childCount} 个子任务 · ${escapeHtml(formatDate(task.created_at))}</option>`;
    }).join("");
    if (previous && !parents.some((task) => String(task.id) === previous)) {
      select.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(previous)}">${escapeHtml(previous.slice(0, 12))} · 历史总任务</option>`);
    }
    select.value = previous;
  }

  function normalizeSampleKeyword(item) {
    if (typeof item === "string") return { keyword_id: item, label: item, display_text: item, sample_count: 0 };
    return {
      ...item,
      keyword_id: String(item?.keyword_id || item?.id || "").trim(),
      sample_count: Number(item?.sample_count ?? item?.count ?? 0),
    };
  }

  async function loadSampleKeywords({ selectCurrent = false } = {}) {
    const payload = await api("/api/samples/keywords");
    const items = Array.isArray(payload) ? payload : (payload?.items || payload?.keywords || []);
    state.sampleKeywords = items.map(normalizeSampleKeyword).filter((item) => item.keyword_id);
    const currentId = state.projectSettings.keyword_id;
    if (currentId && !state.sampleKeywords.some((item) => item.keyword_id === currentId)) {
      state.sampleKeywords.unshift({
        keyword_id: currentId,
        label: state.projectSettings.label,
        display_text: state.projectSettings.display_text,
        sample_count: 0,
      });
    }
    const select = $("sampleKeywordFilter");
    const previous = select.value;
    select.innerHTML = '<option value="">全部唤醒词</option>' + state.sampleKeywords.map((item) => {
      const title = item.display_text || item.label || item.keyword_id;
      return `<option value="${escapeHtml(item.keyword_id)}">${escapeHtml(title)} · ${item.sample_count} 条</option>`;
    }).join("");
    const desired = selectCurrent || !state.sampleKeywordInitialized ? currentId : previous;
    select.value = state.sampleKeywords.some((item) => item.keyword_id === desired) ? desired : "";
    state.sampleKeywordInitialized = true;
  }

  function sampleQuery({ paginate = true } = {}) {
    const params = new URLSearchParams();
    if (paginate) {
      params.set("page", String(state.samplePage));
      params.set("page_size", String(SAMPLE_PAGE_SIZE));
    }
    const mapping = [
      ["provider", $("sampleProviderFilter").value],
      ["gender", $("sampleGenderFilter").value],
      ["age_group", $("sampleAgeFilter").value],
      ["sample_type", $("sampleTypeFilter").value],
      ["quality", $("sampleReviewFilter").value],
      ["search", $("sampleSearch").value.trim()],
      ["keyword_id", $("sampleKeywordFilter").value],
      ["target_kind", $("sampleTargetKindFilter").value],
      ["origin_job_id", $("sampleJobFilter").value],
    ];
    mapping.forEach(([key, value]) => { if (value) params.set(key, value); });
    return params;
  }

  function currentSampleKeywordId() {
    return $("sampleKeywordFilter").value || state.projectSettings.keyword_id;
  }

  function resetSampleSelectionAndLoad() {
    state.selectedSamples.clear();
    updateSelectedSampleCount();
    loadSamples(true);
  }

  async function selectFilteredSamples() {
    const button = $("selectFilteredSamples");
    button.disabled = true;
    try {
      const payload = await api(`/api/samples/ids?${sampleQuery({ paginate: false })}`);
      const rows = Array.isArray(payload) ? payload : (payload?.ids || payload?.items || []);
      const ids = rows.map((item) => typeof item === "object" ? item.id : item).filter(Boolean).map(String);
      ids.forEach((id) => state.selectedSamples.add(id));
      renderSamples();
      toast("已选中当前筛选的全部样本", `共 ${ids.length} 条，包含未显示的分页。`, "success");
    } catch (error) {
      toast("全选失败", error.message, "error", 6500);
    } finally {
      button.disabled = false;
    }
  }

  function clearSelectedSamples() {
    state.selectedSamples.clear();
    renderSamples();
  }

  function goToSamplePage() {
    const requested = Math.trunc(Number($("samplePageInput").value));
    if (!Number.isFinite(requested) || requested < 1 || requested > state.samplePages) {
      $("samplePageInput").value = String(state.samplePage);
      return toast("页码无效", `请输入 1–${state.samplePages} 之间的页码。`, "error");
    }
    if (requested === state.samplePage) return;
    state.samplePage = requested;
    loadSamples();
  }

  async function loadSamples(resetPage = false) {
    if (resetPage) state.samplePage = 1;
    try {
      const result = await api(`/api/samples?${sampleQuery()}`);
      state.samples = Array.isArray(result) ? result : (result.items || result.samples || []);
      state.samplesTotal = Number(Array.isArray(result) ? result.length : (result.total || 0));
      state.samplePages = Math.max(1, Math.ceil(state.samplesTotal / SAMPLE_PAGE_SIZE));
      if (state.samplePage > state.samplePages) {
        state.samplePage = state.samplePages;
        return loadSamples(false);
      }
      renderSamples();
      updateAugmentationEstimate();
    } catch (error) {
      $("sampleTableBody").innerHTML = `<tr><td colspan="8"><div class="empty-state"><p>样本读取失败</p><span>${escapeHtml(error.message)}</span></div></td></tr>`;
    }
  }

  function voiceForSample(sample) {
    const byDbId = sample.metadata?.voice_db_id;
    return state.voices.find((voice) => Number(voice.id) === Number(byDbId))
      || state.voices.find((voice) => voice.provider === sample.provider && voice.voice_id === sample.voice_id && voice.model === sample.model)
      || null;
  }

  function sampleType(sample) {
    if (sample.provider === "real") return "真人";
    return sample.parent_id ? "增强" : "干净";
  }

  function renderSamples() {
    if (!state.samples.length) {
      $("sampleTableBody").innerHTML = '<tr><td colspan="8"><div class="empty-state"><p>没有符合条件的样本</p><span>生成 TTS、导入真人录音或调整筛选条件。</span></div></td></tr>';
    } else {
      $("sampleTableBody").innerHTML = state.samples.map((sample) => {
        const selected = state.selectedSamples.has(sample.id);
        const voice = voiceForSample(sample);
        const gender = sample.metadata?.gender || voice?.gender || "unknown";
        const age = sample.metadata?.age_group || voice?.age_group || "unknown";
        const targetKind = sample.target_kind || sample.metadata?.target_kind || "positive";
        const originJobId = sample.origin_job_id || sample.metadata?.origin_job_id || "";
        const playing = state.inlineAudioId === sample.id && state.inlineAudio && !state.inlineAudio.paused;
        return `<tr data-sample-id="${escapeHtml(sample.id)}">
          <td class="check-cell"><input class="sample-check" type="checkbox" ${selected ? "checked" : ""} aria-label="选择样本" /></td>
          <td><div class="sample-title"><button class="play-button" data-action="play" aria-label="${playing ? "暂停" : "试听"}">${icon(playing ? "pause" : "play")}</button><div><strong title="${escapeHtml(sample.text)}">${escapeHtml(sample.text)}</strong><small>${escapeHtml(sample.id.slice(0, 16))}</small></div></div></td>
          <td><div class="cell-stack"><strong>${escapeHtml(sample.voice_name || sample.voice_id)}</strong><small>${escapeHtml(providerNames[sample.provider] || sample.provider)} · ${escapeHtml(sample.model || "—")}</small></div></td>
          <td><span class="tag target-kind ${escapeHtml(targetKind)}">${escapeHtml(targetKindNames[targetKind] || targetKind)}</span> <span class="tag">${escapeHtml(sampleType(sample))}</span><small class="cell-note">${escapeHtml(genderNames[gender] || "未知")} · ${escapeHtml(ageNames[age] || "未知")}${originJobId ? ` · 任务 ${escapeHtml(String(originJobId).slice(0, 8))}` : ""}</small></td>
          <td><div class="cell-stack"><strong>${formatDuration(sample.duration)}</strong><small>${Number(sample.sample_rate || 0) / 1000} kHz · ${sample.channels || 1} ch</small></div></td>
          <td><span class="status-badge ${sample.quality_status === "accepted" || sample.quality_status === "pass" ? "completed" : sample.quality_status === "rejected" || sample.quality_status === "reject" ? "failed" : "queued"}">${escapeHtml(qualityNames[sample.quality_status] || sample.quality_status)}</span></td>
          <td>${escapeHtml(formatDate(sample.created_at))}</td>
          <td><div class="row-actions"><button class="row-action" data-action="inspect" title="波形、频谱与元数据">${icon("wave")}</button><button class="row-action danger" data-action="delete" title="软删除">${icon("trash")}</button></div></td>
        </tr>`;
      }).join("");
    }
    const start = state.samplesTotal ? (state.samplePage - 1) * SAMPLE_PAGE_SIZE + 1 : 0;
    const end = Math.min(state.samplesTotal, state.samplePage * SAMPLE_PAGE_SIZE);
    $("sampleRange").textContent = `${start}–${end} / ${state.samplesTotal}`;
    $("samplePageLabel").textContent = `${state.samplePage} / ${state.samplePages}`;
    $("samplePageInput").value = String(state.samplePage);
    $("samplePageInput").max = String(state.samplePages);
    $("samplePrev").disabled = state.samplePage <= 1;
    $("sampleNext").disabled = state.samplePage >= state.samplePages;
    $("selectAllSamples").checked = Boolean(state.samples.length) && state.samples.every((sample) => state.selectedSamples.has(sample.id));
    updateSelectedSampleCount();
  }

  function updateSelectedSampleCount() {
    $("selectedSampleCount").textContent = String(state.selectedSamples.size);
    const label = state.selectedSamples.size
      ? `已选择 ${state.selectedSamples.size} 条样本`
      : "未选择时将增强服务端全部干净样本";
    $("noiseSampleSelection").textContent = label;
    updateAugmentationEstimate();
  }

  async function getAudioUrl(sampleId) {
    if (state.audioUrls.has(sampleId)) return state.audioUrls.get(sampleId);
    const response = await request(`/api/samples/${encodeURIComponent(sampleId)}/audio`);
    const url = URL.createObjectURL(await response.blob());
    state.audioUrls.set(sampleId, url);
    return url;
  }

  async function toggleSampleAudio(sampleId) {
    try {
      if (state.inlineAudioId === sampleId && state.inlineAudio) {
        if (state.inlineAudio.paused) await state.inlineAudio.play();
        else state.inlineAudio.pause();
        renderSamples();
        return;
      }
      if (state.inlineAudio) state.inlineAudio.pause();
      state.inlineAudioId = sampleId;
      state.inlineAudio = new Audio(await getAudioUrl(sampleId));
      state.inlineAudio.addEventListener("ended", () => renderSamples(), { once: true });
      await state.inlineAudio.play();
      renderSamples();
    } catch (error) {
      toast("试听失败", error.message, "error");
    }
  }

  function mapQualityToEditor(value) {
    if (value === "pass") return "accepted";
    if (value === "reject") return "rejected";
    if (value === "review" || value === "unchecked") return "pending";
    return value || "pending";
  }

  async function inspectSample(sampleId) {
    const sample = state.samples.find((item) => item.id === sampleId);
    if (!sample) return;
    $("sampleDialogTitle").textContent = sample.text;
    $("sampleDialogId").textContent = sample.id;
    $("sampleAudioSummary").textContent = `${providerNames[sample.provider] || sample.provider} · ${sample.voice_name || sample.voice_id}`;
    $("sampleEditId").value = sample.id;
    $("sampleEditText").value = sample.text;
    $("sampleEditLabel").value = sample.label;
    $("sampleEditSpeaker").value = sample.metadata?.speaker_id || sample.voice_id || "";
    $("sampleEditReview").value = mapQualityToEditor(sample.quality_status);
    $("sampleEditGender").value = sample.metadata?.gender || voiceForSample(sample)?.gender || "unknown";
    $("sampleEditAge").value = sample.metadata?.age_group || voiceForSample(sample)?.age_group || "unknown";
    $("sampleEditTags").value = Array.isArray(sample.metadata?.tags) ? sample.metadata.tags.join(", ") : "";
    $("sampleEditSpeaker").disabled = sample.provider !== "real";
    $("sampleProvenance").innerHTML = [
      ["来源", providerNames[sample.provider] || sample.provider], ["模型", sample.model || "—"],
      ["目标类型", targetKindNames[sample.target_kind] || sample.target_kind || "正样本"],
      ["唤醒词 ID", sample.keyword_id || "—"], ["生成总任务", sample.origin_job_id || "—"],
      ["音色 ID", sample.voice_id], ["Seed", sample.seed], ["语速 / 音高 / 音量", `${sample.speed} / ${sample.pitch} / ${sample.volume}`],
      ["父样本", sample.parent_id || "—"], ["噪声 / SNR", sample.noise_id ? `${sample.noise_id} / ${Number(sample.snr_db).toFixed(1)} dB` : "—"],
      ["SHA-256", sample.sha256],
    ].map(([key, value]) => `<div><strong>${escapeHtml(key)}：</strong>${escapeHtml(value)}</div>`).join("");
    $("sampleDialog").showModal();
    $("sampleAudioPlayer").removeAttribute("src");
    $("waveformCanvas").getContext("2d").clearRect(0, 0, $("waveformCanvas").width, $("waveformCanvas").height);
    $("spectrumCanvas").getContext("2d").clearRect(0, 0, $("spectrumCanvas").width, $("spectrumCanvas").height);
    try {
      const [url, analysis] = await Promise.all([
        getAudioUrl(sampleId), api(`/api/samples/${encodeURIComponent(sampleId)}/analysis`),
      ]);
      $("sampleAudioPlayer").src = url;
      state.currentAnalysis = analysis;
      drawAnalysis(analysis);
    } catch (error) {
      toast("音频分析失败", error.message, "error");
    }
  }

  function drawAnalysis(analysis) {
    drawWaveform($("waveformCanvas"), analysis.waveform || []);
    drawSpectrum($("spectrumCanvas"), analysis.spectrum || {});
    const info = analysis.info || {};
    const metrics = analysis.metrics || {};
    const envelope = analysis.waveform || [];
    const peak = envelope.length ? Math.max(...envelope) : 0;
    const approximateRms = envelope.length ? Math.sqrt(envelope.reduce((sum, value) => sum + value * value, 0) / envelope.length) : 0;
    const peakValue = Number.isFinite(Number(metrics.peak)) ? Number(metrics.peak).toFixed(3) : peak.toFixed(3);
    const rmsValue = Number.isFinite(Number(metrics.rms)) ? Number(metrics.rms).toFixed(3) : `≈ ${approximateRms.toFixed(3)}`;
    const values = [formatDuration(info.duration), peakValue, rmsValue, `${info.sample_rate || "—"} Hz`];
    $("audioMetrics").querySelectorAll("strong").forEach((item, index) => { item.textContent = values[index]; });
  }

  function prepareCanvas(canvas) {
    const ratio = Math.max(1, window.devicePixelRatio || 1);
    const width = Math.max(300, Math.round(canvas.clientWidth || 800));
    const height = Math.max(100, Math.round(canvas.clientHeight || 160));
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { context, width, height };
  }

  function drawWaveform(canvas, values) {
    const { context, width, height } = prepareCanvas(canvas);
    context.clearRect(0, 0, width, height);
    context.strokeStyle = "rgba(255,255,255,.11)";
    context.beginPath(); context.moveTo(0, height / 2); context.lineTo(width, height / 2); context.stroke();
    if (!values.length) return;
    const gradient = context.createLinearGradient(0, 0, width, 0);
    gradient.addColorStop(0, "#9e8cff"); gradient.addColorStop(1, "#f2a64a");
    context.fillStyle = gradient;
    const barWidth = width / values.length;
    values.forEach((value, index) => {
      const amplitude = clamp(Math.abs(Number(value)), 0, 1) * (height * .45);
      context.fillRect(index * barWidth, height / 2 - amplitude, Math.max(1, barWidth * .72), amplitude * 2);
    });
  }

  function drawSpectrum(canvas, spectrum) {
    const { context, width, height } = prepareCanvas(canvas);
    context.clearRect(0, 0, width, height);
    const frequencies = spectrum.frequency || [];
    const magnitudes = spectrum.magnitude_db || [];
    context.strokeStyle = "rgba(255,255,255,.1)";
    context.fillStyle = "rgba(255,255,255,.48)";
    context.font = "10px sans-serif";
    [0, .5, 1].forEach((position) => {
      const x = 12 + position * (width - 24);
      context.beginPath(); context.moveTo(x, 10); context.lineTo(x, height - 20); context.stroke();
      context.fillText(`${Math.round(position * 8)} kHz`, x - (position === 1 ? 25 : 0), height - 5);
    });
    if (!magnitudes.length) return;
    const minDb = Math.min(...magnitudes, -100);
    const maxDb = Math.max(...magnitudes, -10);
    const maxFrequency = Math.max(...frequencies, 8000);
    const gradient = context.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, "rgba(160,139,255,.55)"); gradient.addColorStop(1, "rgba(160,139,255,0)");
    context.beginPath();
    magnitudes.forEach((magnitude, index) => {
      const x = 12 + clamp((frequencies[index] || 0) / maxFrequency, 0, 1) * (width - 24);
      const y = 10 + (1 - clamp((magnitude - minDb) / Math.max(1, maxDb - minDb), 0, 1)) * (height - 35);
      if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
    });
    context.strokeStyle = "#a592ff"; context.lineWidth = 1.5; context.stroke();
    context.lineTo(width - 12, height - 25); context.lineTo(12, height - 25); context.closePath(); context.fillStyle = gradient; context.fill();
  }

  async function saveSampleMetadata(event) {
    event.preventDefault();
    const sampleId = $("sampleEditId").value;
    const sample = state.samples.find((item) => item.id === sampleId);
    const payload = {
      text: $("sampleEditText").value.trim(),
      label: $("sampleEditLabel").value.trim(),
      quality_status: $("sampleEditReview").value,
      gender: $("sampleEditGender").value,
      age_group: $("sampleEditAge").value,
      tags: $("sampleEditTags").value.split(",").map((item) => item.trim()).filter(Boolean),
    };
    if (sample?.provider === "real") payload.speaker_id = $("sampleEditSpeaker").value.trim();
    try {
      await apiJson(`/api/samples/${encodeURIComponent(sampleId)}`, "PATCH", payload);
      $("sampleDialog").close();
      await loadSamples();
      toast("样本信息已保存", "音频来源与 SHA-256 未被修改。", "success");
    } catch (error) {
      toast("保存失败", error.message, "error");
    }
  }

  async function deleteSamples(ids) {
    if (!ids.length) return toast("没有选择样本", "请选择至少一条样本。", "error");
    const confirmed = await confirmAction(
      "软删除样本",
      `将移动 ${ids.length} 条样本到服务器回收目录。删除后可在短时间内点击“撤销”；本页面暂不提供完整回收站，之后需通过恢复 API 或数据目录处理。`,
      "确认删除",
    );
    if (!confirmed) return;
    try {
      const result = await apiJson("/api/samples/delete", "POST", { ids });
      ids.forEach((id) => state.selectedSamples.delete(id));
      await Promise.all([loadSampleKeywords(), loadSamples(), loadStatus()]);
      toast("样本已软删除", `${result.deleted || 0} 条已移入回收目录；可立即撤销`, "success", 12000, {
        label: "撤销",
        handler: () => restoreSamples(ids),
      });
    } catch (error) {
      toast("删除失败", error.message, "error");
    }
  }

  async function restoreSamples(ids) {
    try {
      await runWithLimit(ids, 5, (id) => api(`/api/samples/${encodeURIComponent(id)}/restore`, { method: "POST" }));
      await Promise.all([loadSampleKeywords(), loadSamples(), loadStatus()]);
      toast("样本已恢复", `${ids.length} 条样本已回到样本库`, "success");
    } catch (error) {
      toast("恢复失败", `${error.message}；可通过恢复 API 或服务器数据目录继续处理。`, "error", 7000);
    }
  }

  async function acceptSamples(ids) {
    if (!ids.length) return toast("没有选择样本", "请选择要审核通过的样本。", "error");
    try {
      await runWithLimit(ids, 5, (id) => apiJson(`/api/samples/${encodeURIComponent(id)}`, "PATCH", { quality_status: "accepted" }));
      await loadSamples();
      toast("审核状态已更新", `${ids.length} 条样本标记为已通过`, "success");
    } catch (error) {
      toast("批量审核失败", error.message, "error");
    }
  }

  function recordingMimeType() {
    const candidates = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm"];
    return candidates.find((mime) => window.MediaRecorder?.isTypeSupported?.(mime)) || "";
  }

  function formatRecordingTime(milliseconds) {
    const value = Math.max(0, Number(milliseconds) || 0);
    const minutes = Math.floor(value / 60000);
    const seconds = Math.floor((value % 60000) / 1000);
    const tenths = Math.floor((value % 1000) / 100);
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${tenths}`;
  }

  function setRecorderButtons(recording = false, hasRecording = Boolean(state.recording.file)) {
    $("startRecording").disabled = recording;
    $("stopRecording").disabled = !recording;
    $("resetRecording").disabled = !recording && !hasRecording;
  }

  async function releaseRecordingInput() {
    if (state.recording.timer) window.clearInterval(state.recording.timer);
    if (state.recording.levelFrame) window.cancelAnimationFrame(state.recording.levelFrame);
    state.recording.timer = null;
    state.recording.levelFrame = null;
    state.recording.stream?.getTracks().forEach((track) => track.stop());
    state.recording.stream = null;
    if (state.recording.audioContext && state.recording.audioContext.state !== "closed") {
      try { await state.recording.audioContext.close(); } catch (_) { /* Browser already closed it. */ }
    }
    state.recording.audioContext = null;
    state.recording.analyser = null;
    $("recordingLevelBar").style.width = "0%";
    $("browserRecorder").classList.remove("recording");
  }

  function clearRecordedFile() {
    if (state.recording.url) URL.revokeObjectURL(state.recording.url);
    state.recording.file = null;
    state.recording.url = null;
    state.recording.chunks = [];
    const preview = $("recordingPreview");
    preview.pause();
    preview.removeAttribute("src");
    preview.classList.add("hidden");
    $("recordingTimer").textContent = "00:00.0";
    $("recordingStatus").textContent = "允许 0.2–30 秒；KWS 建议录制一次简短、完整的发音，完成后先试听再上传。";
    setRecorderButtons(false, false);
  }

  function updateRecordingLevel() {
    const analyser = state.recording.analyser;
    if (!analyser) return;
    const values = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(values);
    const rms = Math.sqrt(values.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / values.length);
    $("recordingLevelBar").style.width = `${clamp(rms * 260, 1, 100).toFixed(1)}%`;
    state.recording.levelFrame = window.requestAnimationFrame(updateRecordingLevel);
  }

  function initializeRecorderSupport() {
    const secure = window.isSecureContext;
    const supported = secure && Boolean(navigator.mediaDevices?.getUserMedia) && Boolean(window.MediaRecorder);
    $("recordingSecurityNotice").classList.toggle("hidden", secure);
    $("recordingSupportText").textContent = supported
      ? "支持录音；首次使用需授予麦克风权限"
      : secure ? "当前浏览器不支持 MediaRecorder" : "需要 HTTPS 安全上下文";
    $("startRecording").disabled = !supported;
    $("browserRecorder").classList.toggle("unsupported", !supported);
  }

  async function startRecording() {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      initializeRecorderSupport();
      return toast("无法使用麦克风", "请通过 HTTPS 打开页面，或选择本地录音文件上传。", "error", 7000);
    }
    clearRecordedFile();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
      const mimeType = recordingMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      state.recording.stream = stream;
      state.recording.recorder = recorder;
      state.recording.chunks = [];
      state.recording.discardOnStop = false;
      state.recording.startedAt = performance.now();
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data?.size) state.recording.chunks.push(event.data);
      });
      recorder.addEventListener("stop", async () => {
        const elapsed = performance.now() - state.recording.startedAt;
        await releaseRecordingInput();
        if (state.recording.discardOnStop) {
          state.recording.discardOnStop = false;
          clearRecordedFile();
          return;
        }
        const type = recorder.mimeType || mimeType || "audio/webm";
        const blob = new Blob(state.recording.chunks, { type });
        if (!blob.size) {
          clearRecordedFile();
          return toast("录音失败", "浏览器未生成有效的音频数据。", "error");
        }
        if (elapsed < RECORDING_MIN_MS) {
          clearRecordedFile();
          return toast("录音太短", "真人样本至少需要 0.2 秒，请重新录制一次完整发音。", "error");
        }
        const extension = type.includes("ogg") ? "ogg" : type.includes("mp4") ? "m4a" : type.includes("wav") ? "wav" : "webm";
        state.recording.file = new File([blob], `browser_recording_${Date.now()}.${extension}`, { type });
        state.recording.url = URL.createObjectURL(blob);
        $("recordingPreview").src = state.recording.url;
        $("recordingPreview").classList.remove("hidden");
        $("recordingTimer").textContent = formatRecordingTime(elapsed);
        $("recordingStatus").textContent = `录音已就绪（${formatRecordingTime(elapsed)}），请试听后上传或点击重录。`;
        state.recording.recorder = null;
        setRecorderButtons(false, true);
      }, { once: true });
      recorder.start(250);
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) {
        state.recording.audioContext = new AudioContext();
        state.recording.analyser = state.recording.audioContext.createAnalyser();
        state.recording.analyser.fftSize = 256;
        state.recording.audioContext.createMediaStreamSource(stream).connect(state.recording.analyser);
        updateRecordingLevel();
      }
      state.recording.timer = window.setInterval(() => {
        const elapsed = performance.now() - state.recording.startedAt;
        $("recordingTimer").textContent = formatRecordingTime(elapsed);
        if (elapsed >= RECORDING_MAX_MS - 100 && recorder.state === "recording") {
          recorder.stop();
          toast("已达录音上限", "真人样本最长 30 秒，已自动停止；KWS 建议使用更短的单次发音。", "info");
        }
      }, 100);
      $("recordingStatus").textContent = "正在录音……请在完成后点击停止。";
      $("browserRecorder").classList.add("recording");
      setRecorderButtons(true, false);
    } catch (error) {
      await releaseRecordingInput();
      state.recording.recorder = null;
      initializeRecorderSupport();
      toast("麦克风启动失败", `${error.message}；请检查浏览器权限和 HTTPS 连接。`, "error", 7000);
    }
  }

  function stopRecording() {
    const recorder = state.recording.recorder;
    if (recorder?.state === "recording") {
      $("stopRecording").disabled = true;
      recorder.stop();
    }
  }

  function resetRecording() {
    const recorder = state.recording.recorder;
    if (recorder?.state === "recording") {
      state.recording.discardOnStop = true;
      recorder.stop();
      return;
    }
    clearRecordedFile();
    initializeRecorderSupport();
  }

  async function uploadRealSamples(event) {
    event.preventDefault();
    const files = [...$("sampleUploadFiles").files];
    if (state.recording.file) files.push(state.recording.file);
    if (!files.length) return toast("请选择或录制音频", "可一次选择多条真人音频，也可使用网页麦克风录音。", "error");
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    form.append("text", $("sampleUploadText").value.trim());
    form.append("label", $("sampleUploadLabel").value.trim());
    form.append("keyword_id", state.projectSettings.keyword_id);
    form.append("target_kind", $("sampleUploadTargetKind").value);
    form.append("speaker_id", $("sampleUploadSpeaker").value.trim());
    form.append("split", $("sampleUploadSplit").value);
    form.append("gender", $("sampleUploadGender").value);
    form.append("age_group", $("sampleUploadAge").value);
    const button = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = `正在上传 ${files.length} 个文件…`;
    try {
      const result = await api("/api/samples/upload", { method: "POST", body: form });
      $("uploadSampleDialog").close();
      event.target.reset();
      $("sampleUploadText").value = state.projectSettings.display_text;
      $("sampleUploadLabel").value = state.projectSettings.label;
      resetRecording();
      await Promise.all([loadSampleKeywords(), loadSamples(true), loadStatus()]);
      const extra = `${result.duplicates?.length || 0} 重复，${result.failures?.length || 0} 失败`;
      toast("真人样本导入完成", `${result.imported || 0} 成功，${extra}`, result.failures?.length ? "info" : "success", 6500);
    } catch (error) {
      toast("导入失败", error.message, "error", 7000);
    } finally {
      button.disabled = false;
      button.innerHTML = `${icon("upload")}上传并校验`;
    }
  }

  async function loadNoises() {
    state.noises = await api("/api/noises");
    const valid = new Set(state.noises.map((noise) => noise.id));
    state.selectedNoises.forEach((id) => { if (!valid.has(id)) state.selectedNoises.delete(id); });
    renderNoises();
  }

  function renderNoises() {
    if (!state.noises.length) {
      $("noiseList").innerHTML = '<div class="empty-inline">暂无噪声文件，请上传真实连续环境录音。</div>';
      updateAugmentationEstimate();
      return;
    }
    $("noiseList").innerHTML = state.noises.map((noise) => `<div class="noise-item ${noise.enabled ? "" : "disabled"}" data-noise-id="${escapeHtml(noise.id)}">
      <input class="noise-check" type="checkbox" ${state.selectedNoises.has(noise.id) ? "checked" : ""} ${noise.enabled ? "" : "disabled"} aria-label="选择噪声" />
      <div><strong title="${escapeHtml(noise.name)}">${escapeHtml(noise.name)}</strong><small>${escapeHtml(noise.category)} · ${formatDuration(noise.duration)} · SNR ${Number(noise.min_snr).toFixed(0)}–${Number(noise.max_snr).toFixed(0)} dB${noise.metadata?.source ? ` · ${escapeHtml(noise.metadata.source)}` : ""}</small></div>
      <button class="row-action" data-action="play-noise" title="试听噪声">${icon(state.noiseAudioId === noise.id && state.noiseAudio && !state.noiseAudio.paused ? "pause" : "play")}</button>
      <button class="mini-toggle" data-action="toggle-noise">${noise.enabled ? "已启用" : "已停用"}</button>
      <button class="row-action danger" data-action="delete-noise" title="删除噪声">${icon("trash")}</button>
    </div>`).join("");
    updateAugmentationEstimate();
  }

  async function uploadNoises() {
    const files = [...$("noiseFiles").files];
    if (!files.length) return toast("请选择噪声文件", "支持 WAV、FLAC、MP3 等 ffmpeg 可解码格式。", "error");
    const button = $("uploadNoises");
    button.disabled = true;
    let succeeded = 0;
    const failures = [];
    for (const file of files) {
      button.textContent = `上传 ${succeeded + failures.length + 1} / ${files.length}`;
      const form = new FormData();
      form.append("file", file);
      form.append("name", file.name);
      form.append("category", $("noiseCategory").value);
      form.append("weight", "1");
      form.append("min_snr", $("snrMin").value);
      form.append("max_snr", $("snrMax").value);
      form.append("source", $("noiseSource").value.trim());
      form.append("license", $("noiseLicense").value.trim());
      try {
        await api("/api/noises/upload", { method: "POST", body: form });
        succeeded += 1;
      } catch (error) {
        failures.push(`${file.name}: ${error.message}`);
      }
    }
    button.disabled = false;
    button.textContent = "上传所选文件";
    $("noiseFiles").value = "";
    $("noiseUploadMeta").classList.add("hidden");
    await Promise.all([loadNoises(), loadStatus()]);
    toast("噪声上传完成", `${succeeded} 成功，${failures.length} 失败${failures.length ? `；${failures[0]}` : ""}`, failures.length ? "info" : "success", 7000);
  }

  async function toggleNoise(noiseId) {
    const noise = state.noises.find((item) => item.id === noiseId);
    if (!noise) return;
    try {
      await apiJson(`/api/noises/${encodeURIComponent(noiseId)}`, "PATCH", { enabled: !noise.enabled });
      if (noise.enabled) state.selectedNoises.delete(noiseId);
      await loadNoises();
    } catch (error) {
      toast("噪声状态更新失败", error.message, "error");
    }
  }

  async function toggleNoiseAudio(noiseId) {
    try {
      if (state.noiseAudioId === noiseId && state.noiseAudio) {
        if (state.noiseAudio.paused) await state.noiseAudio.play(); else state.noiseAudio.pause();
        renderNoises();
        return;
      }
      if (state.noiseAudio) state.noiseAudio.pause();
      const cacheKey = `noise:${noiseId}`;
      let url = state.audioUrls.get(cacheKey);
      if (!url) {
        const response = await request(`/api/noises/${encodeURIComponent(noiseId)}/audio`);
        url = URL.createObjectURL(await response.blob());
        state.audioUrls.set(cacheKey, url);
      }
      state.noiseAudioId = noiseId;
      state.noiseAudio = new Audio(url);
      state.noiseAudio.addEventListener("ended", renderNoises, { once: true });
      await state.noiseAudio.play();
      renderNoises();
    } catch (error) {
      toast("噪声试听失败", error.message, "error");
    }
  }

  async function deleteNoise(noiseId) {
    const noise = state.noises.find((item) => item.id === noiseId);
    if (!noise) return;
    if (!await confirmAction(
      "删除噪声",
      `“${noise.name}”将从当前列表移除并移入服务器回收目录。本页面暂不提供噪声回收站，可通过恢复 API 或服务器数据目录恢复。`,
      "确认删除",
    )) return;
    try {
      await api(`/api/noises/${encodeURIComponent(noiseId)}`, { method: "DELETE" });
      state.selectedNoises.delete(noiseId);
      await Promise.all([loadNoises(), loadStatus()]);
      toast("噪声已移出当前列表", "如需恢复，请使用恢复 API 或服务器数据目录。", "success", 7000);
    } catch (error) {
      toast("删除失败", error.message, "error");
    }
  }

  async function materializeNoises() {
    const enabled = state.noises.filter((noise) => noise.enabled);
    const chosen = state.selectedNoises.size
      ? enabled.filter((noise) => state.selectedNoises.has(noise.id))
      : enabled;
    if (!chosen.length) return toast("没有可入库的背景噪声", "请先上传并启用背景噪声，或勾选需要的条目。", "error");
    const keyword = state.projectSettings.display_text;
    if (!await confirmAction(
      "将背景噪声加入样本库",
      `将 ${chosen.length} 条背景噪声关联到当前唤醒词“${keyword}”，并标注为背景噪声样本。该本地操作不会调用云端 TTS。`,
      `入库 ${chosen.length} 条`,
    )) return;
    const button = $("materializeNoises");
    button.disabled = true;
    try {
      const result = await apiJson("/api/noises/materialize", "POST", {
        noise_ids: chosen.map((noise) => noise.id),
        keyword_id: state.projectSettings.keyword_id,
      });
      await Promise.all([loadTasks(), loadSampleKeywords(), loadSamples(true), loadStatus()]);
      const created = result?.created ?? result?.sample_ids?.length ?? 0;
      const reused = result?.duplicates ?? result?.duplicate_sample_ids?.length ?? 0;
      toast("背景噪声已加入样本库", `新建 ${created} 条，复用 ${reused} 条；可在样本库按“背景噪声”筛选。`, "success", 6500);
    } catch (error) {
      toast("背景噪声入库失败", error.message, "error", 7000);
    } finally {
      button.disabled = false;
    }
  }

  function updateAugmentationEstimate() {
    const variants = Math.max(0, Number($("augmentVariants").value || 0));
    const selected = state.selectedSamples.size;
    const textValue = selected ? `${selected * variants} 条增强样本` : `全部干净样本 × ${variants}（服务端计数）`;
    $("augmentationEstimate").querySelector("strong").textContent = textValue;
    updateSelectedSampleCountLabelOnly();
  }

  function updateSelectedSampleCountLabelOnly() {
    const selected = state.selectedSamples.size;
    $("noiseSampleSelection").textContent = selected ? `已选择 ${selected} 条待增强样本` : "未选择时将增强服务端全部干净样本";
  }

  async function submitAugmentation(event) {
    event.preventDefault();
    const enabledNoises = state.noises.filter((noise) => noise.enabled);
    const chosen = state.selectedNoises.size ? enabledNoises.filter((noise) => state.selectedNoises.has(noise.id)) : enabledNoises;
    if (!chosen.length) return toast("没有可用噪声", "请先上传并启用至少一条背景噪声。", "error");
    const minSnr = Number($("snrMin").value);
    const maxSnr = Number($("snrMax").value);
    if (maxSnr < minSnr) return toast("SNR 范围无效", "最高 SNR 必须大于或等于最低 SNR。", "error");
    const count = Number($("augmentVariants").value);
    const sourceText = state.selectedSamples.size ? `${state.selectedSamples.size} 条已选样本` : "全部干净样本";
    if (!await confirmAction("确认批量增强", `${sourceText} × ${count} 份；从 ${chosen.length} 条噪声随机选择，SNR ${minSnr}–${maxSnr} dB。\n原始干净音频不会被覆盖。`, "开始增强")) return;
    const button = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = "正在创建任务…";
    try {
      await runWithLimit(chosen, 5, (noise) => apiJson(`/api/noises/${encodeURIComponent(noise.id)}`, "PATCH", { min_snr: minSnr, max_snr: maxSnr }));
      await apiJson("/api/augment", "POST", {
        sample_ids: [...state.selectedSamples],
        noise_ids: state.selectedNoises.size ? [...state.selectedNoises] : [],
        keyword_id: currentSampleKeywordId(),
        count,
        seed: Number($("augmentSeed").value || 20260804),
      });
      await Promise.all([loadTasks(), loadStatus()]);
      toast("增强任务已创建", "可在任务中心查看混音进度。", "success");
      navigate("tasks");
    } catch (error) {
      toast("增强任务创建失败", error.message, "error", 7000);
    } finally {
      button.disabled = false;
      button.innerHTML = `${icon("settings")}开始批量增强`;
    }
  }

  async function loadExports(silent = false) {
    try {
      state.exports = await api("/api/exports");
      renderExports();
    } catch (error) {
      if (!silent) toast("导出记录读取失败", error.message, "error");
    }
  }

  function renderExports() {
    if (!state.exports.length) {
      $("exportList").innerHTML = '<div class="empty-state"><p>还没有导出记录</p><span>配置参数后创建第一份可复现数据包。</span></div>';
      return;
    }
    $("exportList").innerHTML = state.exports.map((item) => {
      const deleting = state.deletingExports.has(item.id);
      return `<div class="export-item">
      <div><strong>${escapeHtml(item.name)}</strong><small>${item.sample_count} 条 · ${escapeHtml(item.format)} · ${escapeHtml(formatDate(item.created_at))}</small></div>
      <div class="export-actions">
        <button class="button secondary compact" data-export-action="download" data-export-id="${escapeHtml(item.id)}"${deleting ? " disabled" : ""}>${icon("download")}下载 ZIP</button>
        <button class="button quiet danger compact" data-export-action="delete" data-export-id="${escapeHtml(item.id)}" title="将导出包移入回收目录"${deleting ? " disabled" : ""}>${icon("trash")}${deleting ? "删除中…" : "删除"}</button>
      </div>
    </div>`;
    }).join("");
  }

  async function fetchAllSamples(extraParams = {}) {
    const pageSize = 200;
    const firstParams = new URLSearchParams({ ...extraParams, page: "1", page_size: String(pageSize) });
    const first = await api(`/api/samples?${firstParams}`);
    const expectedTotal = Number(first.total || 0);
    const pages = Math.ceil(expectedTotal / pageSize);
    const collected = [...(first.items || [])];
    for (let page = 2; page <= pages; page += 1) {
      const params = new URLSearchParams({ ...extraParams, page: String(page), page_size: String(pageSize) });
      const result = await api(`/api/samples?${params}`);
      const items = result.items || [];
      if (!items.length && collected.length < expectedTotal) {
        throw new Error(`样本列表在分页收集期间发生变化：预计 ${expectedTotal} 条，仅取得 ${collected.length} 条。请刷新后重试，未创建不完整导出。`);
      }
      collected.push(...items);
    }
    const unique = [...new Map(collected.map((sample) => [sample.id, sample])).values()];
    if (unique.length !== expectedTotal) {
      throw new Error(`样本列表在分页收集期间发生变化：预计 ${expectedTotal} 条，取得 ${unique.length} 条唯一记录。请刷新后重试，未创建不完整导出。`);
    }
    return unique;
  }

  function updateSplitValidation() {
    const train = clamp(Number($("trainSplit").value || 0), 0, 100);
    const dev = clamp(Number($("devSplit").value || 0), 0, 100);
    const test = clamp(Number($("testSplit").value || 0), 0, 100);
    $("trainSplitBar").style.width = `${train}%`;
    $("devSplitBar").style.width = `${dev}%`;
    $("testSplitBar").style.width = `${test}%`;
    const total = train + dev + test;
    $("splitValidation").textContent = `总计 ${total}%${total === 100 ? "" : "，必须等于 100%"}`;
    $("splitValidation").classList.toggle("error", total !== 100);
    return total === 100;
  }

  async function submitExport(event) {
    event.preventDefault();
    if (!updateSplitValidation()) return toast("数据划分无效", "Train、Dev、Test 的总和必须为 100%。", "error");
    const button = event.submitter || event.target.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = "正在收集完整样本清单…";
    const scope = $("exportScope").value;
    const includeAugmented = $("exportAugmented").checked;
    const keywordId = currentSampleKeywordId();
    let sampleIds = [];
    try {
      if (scope === "selected") {
        sampleIds = [...state.selectedSamples];
        if (!sampleIds.length) return toast("没有选择样本", "回到样本库选择后再导出，或改为全部范围。", "error");
        if (!includeAugmented) {
          const selectedSet = new Set(sampleIds);
          sampleIds = (await fetchAllSamples({ keyword_id: keywordId })).filter((sample) => selectedSet.has(sample.id) && !sample.parent_id).map((sample) => sample.id);
          if (!sampleIds.length) return toast("所选范围为空", "当前选择仅包含增强样本；请开启“包含增强样本”或重选。", "error");
        }
      } else if (scope === "accepted") {
        const [passed, accepted] = await Promise.all([
          fetchAllSamples({ quality: "pass", keyword_id: keywordId }),
          fetchAllSamples({ quality: "accepted", keyword_id: keywordId }),
        ]);
        const unique = new Map([...passed, ...accepted].map((sample) => [sample.id, sample]));
        sampleIds = [...unique.values()].filter((sample) => includeAugmented || !sample.parent_id).map((sample) => sample.id);
        if (!sampleIds.length) return toast("没有已通过样本", "请先在样本库试听并完成审核。", "error");
      } else if (!includeAugmented) {
        sampleIds = (await fetchAllSamples({ keyword_id: keywordId })).filter((sample) => !sample.parent_id).map((sample) => sample.id);
      }
      const train = Number($("trainSplit").value) / 100;
      const dev = Number($("devSplit").value) / 100;
      const format = document.querySelector('input[name="exportFormat"]:checked')?.value || "generic";
      const countText = sampleIds.length ? `${sampleIds.length} 条指定样本` : "服务端全部样本";
      if (!await confirmAction("创建导出任务", `${countText}；格式 ${format.toUpperCase()}；Train / Dev / Test = ${Math.round(train * 100)} / ${Math.round(dev * 100)} / ${Math.round((1 - train - dev) * 100)}。`, "开始导出")) return;
      await apiJson("/api/exports", "POST", {
        sample_ids: sampleIds,
        keyword_id: keywordId,
        name: $("exportDatasetName").value.trim() || `${state.projectSettings.keyword_id}_dataset`,
        format,
        include_audio: $("exportAudio").checked,
        train_ratio: train,
        dev_ratio: dev,
        seed: 20260804,
      });
      await loadTasks();
      toast("导出任务已创建", "完成后会出现在右侧导出记录中。", "success");
    } catch (error) {
      toast("导出任务创建失败", error.message, "error", 7000);
    } finally {
      button.disabled = false;
      button.innerHTML = `${icon("box")}创建导出任务`;
    }
  }

  async function downloadExport(exportId) {
    try {
      const response = await request(`/api/exports/${encodeURIComponent(exportId)}/download`);
      const disposition = response.headers.get("content-disposition") || "";
      const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
      const basic = disposition.match(/filename="?([^";]+)"?/i)?.[1];
      const name = encoded ? decodeURIComponent(encoded) : (basic || `${state.projectSettings.keyword_id}_${exportId.slice(0, 8)}.zip`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30000);
      toast("下载已开始", name, "success");
    } catch (error) {
      toast("下载失败", error.message, "error");
    }
  }

  async function restoreExport(exportId) {
    try {
      await api(`/api/exports/${encodeURIComponent(exportId)}/restore`, { method: "POST" });
      await loadExports();
      toast("导出记录已恢复", "ZIP 已移回导出目录。", "success");
    } catch (error) {
      toast("恢复失败", error.message, "error", 7000);
    }
  }

  async function deleteExport(exportId) {
    if (state.deletingExports.has(exportId)) return;
    const item = state.exports.find((entry) => entry.id === exportId);
    if (!item) return;
    const exportLabel = `${item.name}（${formatDate(item.created_at)}）`;
    const confirmed = await confirmAction(
      "删除导出记录？",
      `“${exportLabel}”及其 ZIP 将移入服务器回收目录。原始样本和任务记录不会被删除。`,
      "确认删除",
    );
    if (!confirmed) return;
    state.deletingExports.add(exportId);
    renderExports();
    try {
      const result = await api(`/api/exports/${encodeURIComponent(exportId)}`, { method: "DELETE" });
      await loadExports();
      const notice = toast(
        "导出记录已删除",
        result.recoverable ? `“${exportLabel}”的 ZIP 已移入回收目录，可立即撤销。` : `“${exportLabel}”的记录已移除；原 ZIP 此前已不存在。`,
        "success",
        result.recoverable ? 12000 : 7000,
        result.recoverable ? { label: "撤销", handler: () => restoreExport(exportId) } : null,
      );
      if (notice.actionButton) notice.actionButton.focus({ preventScroll: true });
      else $("refreshExports").focus({ preventScroll: true });
    } catch (error) {
      toast("删除失败", error.message, "error", 7000);
    } finally {
      state.deletingExports.delete(exportId);
      if (state.exports.some((entry) => entry.id === exportId)) renderExports();
    }
  }

  function bindEvents() {
    document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
      if (button.dataset.view === "providers") openProviderSettings();
      else navigate(button.dataset.view);
    }));
    document.querySelectorAll("[data-go]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.go)));
    $("menuToggle").addEventListener("click", () => $("sidebar").classList.toggle("open"));
    $("projectSettingsButton").addEventListener("click", openProjectSettings);
    $("projectSettingsForm").addEventListener("submit", saveProjectSettings);
    $("projectDisplayText").addEventListener("input", () => {
      const changed = $("projectDisplayText").value.trim() !== state.projectSettings.display_text;
      $("projectDerivedHint").textContent = changed
        ? "保存后由服务器重新生成训练标签和 Keyword ID。"
        : "派生字段以服务器当前保存的配置为准。";
    });
    $("accessTokenButton").addEventListener("click", async () => {
      const message = token() ? "修改访问令牌；输入空字符串可清除（仅当前浏览器会话）：" : "输入 TTS_APP_API_TOKEN（仅当前浏览器会话）：";
      const value = window.prompt(message, "");
      if (value === null) return;
      setToken(value.trim());
      toast(value.trim() ? "访问令牌已设置" : "访问令牌已清除", "将重新连接受保护接口。", "success");
      try { await loadProviders(); } catch (error) { toast("令牌验证失败", error.message, "error"); }
    });
    $("providerSettingsButton").addEventListener("click", () => openProviderSettings());
    $("reloadProviderSettings").addEventListener("click", () => openProviderSettings());
    $("providerCards").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-config-provider]");
      if (button) openProviderSettings(button.dataset.configProvider);
    });
    document.querySelectorAll("[data-provider-settings-tab]").forEach((button) => button.addEventListener("click", () => selectProviderSettingsTab(button.dataset.providerSettingsTab)));
    document.querySelectorAll("[data-provider-settings-form]").forEach((form) => form.addEventListener("submit", saveProviderSettings));
    document.querySelectorAll("[data-test-provider]").forEach((button) => button.addEventListener("click", () => testProviderSettings(button.dataset.testProvider, button)));
    document.querySelectorAll("[data-clear-provider]").forEach((button) => button.addEventListener("click", () => clearProviderSettings(button.dataset.clearProvider, button)));
    ["aliyun", "volcengine"].forEach((prefix) => $(`${prefix}PricingUnit`).addEventListener("change", (event) => {
      if (event.target.value) return;
      $(`${prefix}PricingRate`).value = "";
      $(`${prefix}PricingCurrency`).value = "";
    }));

    ["voiceSearch", "voiceProviderFilter", "voiceGenderFilter", "voiceAgeFilter"].forEach((id) => $(id).addEventListener("input", renderVoices));
    $("voiceGrid").addEventListener("change", (event) => {
      const card = event.target.closest("[data-voice-id]");
      if (!card) return;
      const id = Number(card.dataset.voiceId);
      if (event.target.checked) state.selectedVoices.add(id); else state.selectedVoices.delete(id);
      renderVoices();
    });
    $("selectVisibleVoices").addEventListener("click", () => { filteredVoices().forEach((voice) => state.selectedVoices.add(Number(voice.id))); renderVoices(); });
    $("clearSelectedVoices").addEventListener("click", () => { state.selectedVoices.clear(); renderVoices(); });
    $("refreshVoiceButton").addEventListener("click", () => refreshVoices());
    $("refreshAllVoices").addEventListener("click", () => refreshVoices());
    ["generationTexts", "generationCount"].forEach((id) => $(id).addEventListener("input", updateGenerationEstimate));
    $("generationSpeed").addEventListener("input", (event) => { $("speedOutput").textContent = `${Number(event.target.value).toFixed(2)}×`; });
    $("generationPitch").addEventListener("input", (event) => { $("pitchOutput").textContent = `${event.target.value} st`; });
    $("generationVolume").addEventListener("input", (event) => { $("volumeOutput").textContent = `${event.target.value} / 100`; });
    ["randomSpeed", "randomPitch", "randomVolume"].forEach((id) => $(id).addEventListener("change", updateRandomParameterState));
    $("generationForm").addEventListener("submit", submitGeneration);

    $("refreshTasks").addEventListener("click", () => loadTasks());
    ["taskList", "dashboardTasks"].forEach((id) => $(id).addEventListener("click", (event) => {
      const button = event.target.closest("button[data-retry-task]");
      if (button) retryTask(button);
    }));
    $("taskStatusTabs").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-status]");
      if (!button) return;
      state.taskStatus = button.dataset.status;
      $("taskStatusTabs").querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
      renderTasks();
    });

    let sampleSearchTimer;
    $("sampleSearch").addEventListener("input", () => {
      state.selectedSamples.clear();
      updateSelectedSampleCount();
      clearTimeout(sampleSearchTimer);
      sampleSearchTimer = setTimeout(() => loadSamples(true), 260);
    });
    ["sampleProviderFilter", "sampleGenderFilter", "sampleAgeFilter", "sampleTypeFilter", "sampleReviewFilter", "sampleTargetKindFilter", "sampleJobFilter", "sampleKeywordFilter"].forEach((id) => $(id).addEventListener("change", resetSampleSelectionAndLoad));
    $("samplePrev").addEventListener("click", () => { if (state.samplePage > 1) { state.samplePage -= 1; loadSamples(); } });
    $("sampleNext").addEventListener("click", () => { if (state.samplePage < state.samplePages) { state.samplePage += 1; loadSamples(); } });
    $("sampleGoPage").addEventListener("click", goToSamplePage);
    $("samplePageInput").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); goToSamplePage(); } });
    $("selectFilteredSamples").addEventListener("click", selectFilteredSamples);
    $("clearSelectedSamples").addEventListener("click", clearSelectedSamples);
    $("selectAllSamples").addEventListener("change", (event) => {
      state.samples.forEach((sample) => { if (event.target.checked) state.selectedSamples.add(sample.id); else state.selectedSamples.delete(sample.id); });
      renderSamples();
    });
    $("sampleTableBody").addEventListener("change", (event) => {
      if (!event.target.classList.contains("sample-check")) return;
      const id = event.target.closest("tr").dataset.sampleId;
      if (event.target.checked) state.selectedSamples.add(id); else state.selectedSamples.delete(id);
      updateSelectedSampleCount();
    });
    $("sampleTableBody").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const id = button.closest("tr").dataset.sampleId;
      if (button.dataset.action === "play") toggleSampleAudio(id);
      if (button.dataset.action === "inspect") inspectSample(id);
      if (button.dataset.action === "delete") deleteSamples([id]);
    });
    $("deleteSelectedSamples").addEventListener("click", () => deleteSamples([...state.selectedSamples]));
    $("acceptSelectedSamples").addEventListener("click", () => acceptSamples([...state.selectedSamples]));
    $("sendSamplesToNoise").addEventListener("click", () => { if (!state.selectedSamples.size) toast("未选择样本", "将使用服务端全部干净原音；也可以返回样本库先多选。", "info"); navigate("noise"); });
    $("exportSelectedSamples").addEventListener("click", () => { if (!state.selectedSamples.size) return toast("没有选择样本", "请先选择要导出的样本。", "error"); $("exportScope").value = "selected"; navigate("export"); });
    $("sampleMetadataForm").addEventListener("submit", saveSampleMetadata);
    $("openUploadSample").addEventListener("click", () => {
      initializeRecorderSupport();
      $("uploadSampleDialog").showModal();
    });
    $("uploadSampleForm").addEventListener("submit", uploadRealSamples);
    $("startRecording").addEventListener("click", startRecording);
    $("stopRecording").addEventListener("click", stopRecording);
    $("resetRecording").addEventListener("click", resetRecording);
    $("uploadSampleDialog").addEventListener("close", resetRecording);

    $("noiseFiles").addEventListener("change", () => $("noiseUploadMeta").classList.toggle("hidden", !$("noiseFiles").files.length));
    ["dragenter", "dragover"].forEach((name) => $("noiseDropzone").addEventListener(name, (event) => {
      event.preventDefault();
      $("noiseDropzone").classList.add("dragging");
    }));
    $("noiseDropzone").addEventListener("dragleave", () => $("noiseDropzone").classList.remove("dragging"));
    $("noiseDropzone").addEventListener("drop", (event) => {
      event.preventDefault();
      $("noiseDropzone").classList.remove("dragging");
      if (event.dataTransfer?.files?.length) {
        $("noiseFiles").files = event.dataTransfer.files;
        $("noiseUploadMeta").classList.remove("hidden");
      }
    });
    $("uploadNoises").addEventListener("click", uploadNoises);
    $("noiseList").addEventListener("change", (event) => {
      if (!event.target.classList.contains("noise-check")) return;
      const id = event.target.closest("[data-noise-id]").dataset.noiseId;
      if (event.target.checked) state.selectedNoises.add(id); else state.selectedNoises.delete(id);
      updateAugmentationEstimate();
    });
    $("noiseList").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const id = button.closest("[data-noise-id]").dataset.noiseId;
      if (button.dataset.action === "play-noise") toggleNoiseAudio(id);
      if (button.dataset.action === "toggle-noise") toggleNoise(id);
      if (button.dataset.action === "delete-noise") deleteNoise(id);
    });
    $("selectAllNoises").addEventListener("click", () => {
      const enabled = state.noises.filter((noise) => noise.enabled);
      const allSelected = enabled.length && enabled.every((noise) => state.selectedNoises.has(noise.id));
      enabled.forEach((noise) => { if (allSelected) state.selectedNoises.delete(noise.id); else state.selectedNoises.add(noise.id); });
      renderNoises();
    });
    $("materializeNoises").addEventListener("click", materializeNoises);
    $("augmentVariants").addEventListener("input", updateAugmentationEstimate);
    document.querySelectorAll(".preset-button").forEach((button) => button.addEventListener("click", () => {
      const presets = { balanced: [0, 20], farfield: [-5, 10], light: [15, 30] };
      const values = presets[button.dataset.preset] || presets.balanced;
      $("snrMin").value = values[0]; $("snrMax").value = values[1];
      document.querySelectorAll(".preset-button").forEach((item) => item.classList.toggle("active", item === button));
    }));
    $("augmentationForm").addEventListener("submit", submitAugmentation);

    ["trainSplit", "devSplit", "testSplit"].forEach((id) => $(id).addEventListener("input", updateSplitValidation));
    $("exportForm").addEventListener("submit", submitExport);
    $("refreshExports").addEventListener("click", () => loadExports());
    $("exportList").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-export-id]");
      if (!button) return;
      if (button.dataset.exportAction === "download") downloadExport(button.dataset.exportId);
      if (button.dataset.exportAction === "delete") deleteExport(button.dataset.exportId);
    });
    window.addEventListener("resize", () => { if ($("sampleDialog").open && state.currentAnalysis) drawAnalysis(state.currentAnalysis); });
    $("sampleDialog").addEventListener("close", () => $("sampleAudioPlayer").pause());
    document.addEventListener("visibilitychange", () => { if (!document.hidden) loadTasks(true); });
  }

  function markFixedBackendOptions() {
    const fixed = [
      ["silenceBefore", "暂不支持", "当前后端由厂商输出后统一转码，尚未追加前置留白"],
      ["silenceAfter", "暂不支持", "当前后端由厂商输出后统一转码，尚未追加后置留白"],
      ["namingPattern", "当前固定", "服务端当前固定使用不可冲突的 UUID 命名"],
      ["generationDeduplicate", "当前固定", "服务端始终进行 SHA-256 去重"],
      ["augmentProbability", "当前固定 100%", "当前服务端对选中样本全部应用增强"],
      ["maxNoiseLayers", "当前固定 1 层", "当前服务端每份增强样本叠加一条噪声"],
      ["peakNormalize", "当前自动", "当前服务端仅在发生削波时自动缩放"],
      ["randomNoiseOffset", "当前固定开启", "当前服务端始终使用可复现的随机起点"],
      ["loopShortNoise", "当前固定开启", "当前服务端始终循环短噪声"],
      ["preserveClean", "当前固定开启", "当前服务端始终保留干净原音"],
      ["exportChecksums", "当前固定开启", "复制音频时服务端始终生成 SHA256SUMS，并在清单中记录每条样本摘要"],
    ];
    fixed.forEach(([id, label, title]) => {
      const item = $(id);
      if (!item) return;
      item.disabled = true;
      item.title = title;
      const wrapper = item.closest(".switch-row") || item.closest("label") || item.parentElement;
      wrapper.classList.add("fixed-option");
      wrapper.dataset.fixedLabel = label;
      wrapper.title = title;
    });
  }

  async function initialize() {
    mountProviderSettingsPage();
    bindEvents();
    markFixedBackendOptions();
    updateTokenButton();
    updateGenerationEstimate();
    updateSplitValidation();
    initializeRecorderSupport();
    navigate((location.hash || "#dashboard").slice(1));
    const online = await loadStatus();
    if (!online) return;
    try {
      await loadProjectSettings({ applyDefaults: true });
    } catch (error) {
      state.projectSettings = { ...DEFAULT_PROJECT_SETTINGS };
      renderProjectSummary();
      applyProjectDefaults({ force: true });
      toast("唤醒词设置读取失败", `${error.message}；暂时使用默认值“${DEFAULT_PROJECT_SETTINGS.display_text}”。`, "error", 7000);
    }
    try {
      await loadProviders();
      await Promise.all([loadVoices(), loadTasks(), loadSampleKeywords(), loadNoises(), loadExports(), loadProviderSettings()]);
      await loadSamples();
    } catch (error) {
      toast("初始化失败", error.message, "error", 7500);
    }
    state.pollTimer = window.setInterval(async () => {
      if (document.hidden) return;
      const hasRunning = state.tasks.some((task) => ACTIVE_TASK_STATES.has(task.status));
      if (hasRunning) {
        await Promise.all([loadTasks(true), loadStatus()]);
        if (!state.tasks.some((task) => ACTIVE_TASK_STATES.has(task.status))) {
          await Promise.all([loadSampleKeywords(), loadSamples(), loadExports(true)]);
        }
      }
    }, 3000);
  }

  window.addEventListener("beforeunload", () => {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.recording.stream?.getTracks().forEach((track) => track.stop());
    if (state.recording.url) URL.revokeObjectURL(state.recording.url);
    state.audioUrls.forEach((url) => URL.revokeObjectURL(url));
  });
  window.addEventListener("DOMContentLoaded", initialize);
})();
