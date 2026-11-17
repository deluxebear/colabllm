import { initCopilotStudio } from "./copilot.js";

const csrfMeta = document.querySelector('meta[name="colabllm-csrf"]');
const readOnlyMeta = document.querySelector('meta[name="colabllm-readonly"]');
const CSRF_TOKEN = csrfMeta ? csrfMeta.content : "";
const READ_ONLY = readOnlyMeta ? readOnlyMeta.content === "true" : false;

const CATEGORY_ORDER = ["llm", "image", "audio", "video", "ocr", "other"];

const CATEGORY_LABELS = {
  llm: "LLM 与推理",
  image: "图像生成与编辑",
  audio: "语音合成 / 转写 / 音效",
  video: "视频与原生工作流 UI",
  ocr: "文档 OCR",
  other: "其他运行时",
};

const FLEET_VIEW_STORAGE_KEY = "colabllm-fleet-view";
const SORT_COLLATOR = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function loadSavedFleetView() {
  try {
    const saved = localStorage.getItem(FLEET_VIEW_STORAGE_KEY);
    if (saved === "cards" || saved === "table" || saved === "grouped") return saved;
  } catch {
    // Ignore storage restrictions.
  }
  return "cards";
}

const VIEW_META = {
  fleet: ["舰队与实例", "管理 Google Colab 与 Kaggle 运行时生命周期、实时日志与公网隧道"],
  copilot: ["AI 演练场 · Copilot Studio", "基于同源代理的 OpenAI 兼容、多模态与运行时原生 API 演练场"],
  workbench: ["工作台与命令", "动态解析 CLI 子命令表单与后台任务执行流"],
  network: ["隧道与账户", "配置项目全局主域名、核验 FRP 与只读查看 Colab/Kaggle 多账户配额"],
  doctor: ["环境体检", "一键核验 Colab / Kaggle 本机依赖、隧道后端与授权状态"],
};

const COMMON_UP_FLAGS = new Set([
  "-i",
  "--instance",
  "-a",
  "--account",
  "--drive-account",
  "--domain",
  "--base-domain",
  "--quick-tunnel",
  "--gpu",
  "--accelerator",
  "--internet",
  "--no-internet",
  "--transport",
  "--skip",
  "--hf-token",
  "--no-drive",
  "--new-credentials",
  "--auto-restart",
  "--restart-delay",
  "--idle-check",
  "--max-restarts",
  "--max-uptime",
  "--startup-timeout",
  "--ready-timeout",
  "--detach",
  "--dry-run",
  "--show-secrets",
]);

const state = {
  version: "",
  readOnly: READ_ONLY,
  defaultAccount: "",
  services: [],
  servicesMap: new Map(),
  schemas: new Map(),
  rawInstances: [],
  instances: [],
  accounts: null,
  accountsUsageLoading: false,
  tunnel: null,
  tasks: [],
  bootstrapSeeded: false,
  knownTaskStatuses: new Map(),
  notifiedTaskIds: new Set(),
  recentTaskFailures: new Map(),
  activeView: "fleet",
  activeFleetPanel: "fleet-panel-instances",
  fleetView: loadSavedFleetView(),
  instanceStatusFilter: "all",
  instanceSortKey: "status",
  instanceSortDir: "ascending",
  activeTaskId: null,
  activeTaskSource: null,
  eventsSource: null,
  logsSource: null,
  logsRawText: "",
  logsDisplayedText: "",
  logsTarget: null,
  downTarget: null,
  removeTarget: null,
  connectTarget: null,
  connectSecrets: null,
  deferredToasts: [],
  themeMq: window.matchMedia("(prefers-color-scheme: dark)"),
};
let copilotController = null;

const instanceNodeCache = new Map();
const groupNodeCache = new Map();
let instanceSlotSeq = 0;

function el(tag, attrs = null, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "className") {
        node.className = String(v);
      } else if (k === "textContent") {
        node.textContent = String(v);
      } else if (k === "dataset" && typeof v === "object") {
        for (const [dk, dv] of Object.entries(v)) {
          if (dv !== undefined && dv !== null) node.dataset[dk] = String(dv);
        }
      } else if (k === "styleProps" && typeof v === "object") {
        for (const [sk, sv] of Object.entries(v)) {
          if (sv !== undefined && sv !== null) node.style.setProperty(sk, String(sv));
        }
      } else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (v === true) {
        node.setAttribute(k, "");
      } else {
        node.setAttribute(k, String(v));
      }
    }
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (typeof child === "string" || typeof child === "number") {
      node.appendChild(document.createTextNode(String(child)));
    } else {
      node.appendChild(child);
    }
  }
  return node;
}

function escapeShellArg(arg) {
  const s = String(arg);
  if (/^[A-Za-z0-9_./:=+-]+$/.test(s)) return s;
  return `'${s.replace(/'/g, "'\"'\"'")}'`;
}

function safeExternalUrl(raw) {
  try {
    const parsed = new URL(String(raw || ""));
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.href;
    }
  } catch {
    return null;
  }
  return null;
}

function enhanceSwitch(input) {
  if (!input || ("switch" in HTMLInputElement.prototype)) return input;
  input.setAttribute("role", "switch");
  const sync = () => input.setAttribute("aria-checked", input.checked ? "true" : "false");
  sync();
  input.addEventListener("change", sync);
  return input;
}

function closePopover(pop) {
  if (!pop) return;
  try {
    pop.hidePopover?.();
  } catch {
    // Ignore if not open or unsupported.
  }
  pop.classList.remove(":popover-open");
}

function setupFeatureFallbacks() {
  if (!("switch" in HTMLInputElement.prototype)) {
    document.documentElement.classList.add("no-native-switch");
    document.querySelectorAll('input[type="checkbox"][switch]').forEach(enhanceSwitch);
  }

  if (!("commandForElement" in HTMLButtonElement.prototype)) {
    document.addEventListener("click", (evt) => {
      const btn = evt.target instanceof Element ? evt.target.closest("button[commandfor][command]") : null;
      if (!btn) return;
      const targetId = btn.getAttribute("commandfor");
      const cmd = btn.getAttribute("command");
      const target = targetId ? document.getElementById(targetId) : null;
      if (!target) return;
      if (target instanceof HTMLDialogElement) {
        if (cmd === "show-modal" && !target.open) target.showModal();
        else if (cmd === "close" || cmd === "request-close") target.close();
      } else if (target.hasAttribute("popover")) {
        if (cmd === "toggle-popover") target.togglePopover?.();
        else if (cmd === "show-popover") target.showPopover?.();
        else if (cmd === "hide-popover") target.hidePopover?.();
      }
    });
  }

  if (!Object.hasOwn(HTMLElement.prototype, "popover")) {
    const closeAllAutoPopovers = (except = null) => {
      document.querySelectorAll('[popover="auto"]').forEach((node) => {
        if (node !== except) node.classList.remove(":popover-open");
      });
    };
    document.addEventListener("click", (evt) => {
      const trigger = evt.target instanceof Element ? evt.target.closest("[popovertarget]") : null;
      if (trigger) {
        const targetId = trigger.getAttribute("popovertarget");
        const target = targetId ? document.getElementById(targetId) : null;
        if (!target || !target.hasAttribute("popover")) return;
        const action = trigger.getAttribute("popovertargetaction") || "toggle";
        const isOpen = target.classList.contains(":popover-open");
        if (action === "hide" || (action === "toggle" && isOpen)) {
          target.classList.remove(":popover-open");
        } else {
          if (target.getAttribute("popover") === "auto") closeAllAutoPopovers(target);
          target.classList.add(":popover-open");
        }
        return;
      }
      const insideAuto = evt.target instanceof Element ? evt.target.closest('[popover="auto"]') : null;
      if (!insideAuto) {
        closeAllAutoPopovers();
      }
    });
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape") closeAllAutoPopovers();
    });
  }

  if (!("onbeforematch" in HTMLElement.prototype)) {
    document.querySelectorAll('[hidden="until-found"]').forEach((node) => {
      if (node.getAttribute("role") === "tabpanel") {
        // Keep inactive tabpanels cleanly hidden so Workbench & Fleet tab exclusivity remains intact.
        node.setAttribute("hidden", "");
      } else {
        node.removeAttribute("hidden");
      }
    });
  }

  if (!Object.hasOwn(HTMLElement.prototype, "focusGroup")) {
    document.querySelectorAll(".tablist[focusgroup]").forEach((tablist) => {
      tablist.setAttribute("role", "tablist");
      const tabs = Array.from(tablist.querySelectorAll(".tab-btn"));
      tabs.forEach((t) => {
        t.setAttribute("role", "tab");
        t.tabIndex = t.getAttribute("aria-selected") === "true" ? 0 : -1;
      });
      tablist.addEventListener("keydown", (evt) => {
        const current = document.activeElement;
        const idx = tabs.indexOf(current);
        if (idx < 0) return;
        let next = -1;
        if (evt.key === "ArrowRight") next = (idx + 1) % tabs.length;
        else if (evt.key === "ArrowLeft") next = (idx - 1 + tabs.length) % tabs.length;
        else if (evt.key === "Home") next = 0;
        else if (evt.key === "End") next = tabs.length - 1;
        if (next >= 0) {
          evt.preventDefault();
          tabs.forEach((t, i) => {
            t.tabIndex = i === next ? 0 : -1;
          });
          tabs[next].focus();
        }
      });
    });
  }
}

/* Modal-aware Toast Notifications (popover="manual") */
let toastSeq = 0;

function repositionToasts() {
  const region = document.getElementById("toast-region");
  if (!region) return;
  const items = Array.from(region.querySelectorAll(".toast-item:not([data-closing='1'])"));
  let offset = 0;
  for (const item of items) {
    item.style.setProperty("--toast-offset", `${offset}px`);
    offset += (item.getBoundingClientRect().height || 52) + 8;
  }
}

function announceScreenReader(text) {
  if (typeof document.body.ariaNotify === "function") {
    document.body.ariaNotify(text);
    return;
  }
  const region = document.getElementById("sr-announcer");
  if (region) {
    region.textContent = "";
    window.setTimeout(() => {
      region.textContent = text;
    }, 10);
  }
}

function dismissToast(item, { restoreFocus = false, priorFocus = null } = {}) {
  if (!item || !item.isConnected || item.dataset.closing === "1") return;
  const wasFocused = restoreFocus || item.contains(document.activeElement);
  item.dataset.closing = "1";

  let nextFocus = null;
  if (wasFocused) {
    const region = document.getElementById("toast-region");
    const remaining = region
      ? Array.from(region.querySelectorAll(".toast-item:not([data-closing='1'])"))
      : [];
    const nextBtn = remaining[0]?.querySelector("button");
    if (nextBtn instanceof HTMLElement) {
      nextFocus = nextBtn;
    } else if (priorFocus instanceof HTMLElement && priorFocus.isConnected) {
      nextFocus = priorFocus;
    }
  }

  try {
    if (typeof item.matches === "function" && item.matches(":popover-open")) {
      item.hidePopover?.();
    } else {
      item.classList.remove(":popover-open");
    }
  } catch {
    item.classList.remove(":popover-open");
  }
  repositionToasts();

  if (nextFocus) {
    nextFocus.focus({ preventScroll: true });
  }

  window.setTimeout(() => {
    item.remove();
    repositionToasts();
  }, 240);
}

function renderToastNow(message, tone = "info", duration = 4500) {
  const region = document.getElementById("toast-region");
  if (!region) return;

  const priorFocus =
    document.activeElement instanceof HTMLElement && !region.contains(document.activeElement)
      ? document.activeElement
      : null;
  const toastId = `toast-item-${++toastSeq}`;
  const closeBtn = el("button", {
    type: "button",
    className: "btn icon-btn sm",
    popovertarget: toastId,
    popovertargetaction: "hide",
    "aria-label": "关闭通知",
    textContent: "✕",
  });
  const item = el(
    "div",
    { id: toastId, className: "toast-item", popover: "manual", "data-tone": tone },
    el("span", { textContent: message }),
    closeBtn,
  );
  region.prepend(item);

  try {
    if (typeof item.showPopover === "function") item.showPopover();
    else item.classList.add(":popover-open");
  } catch {
    item.classList.add(":popover-open");
  }
  repositionToasts();
  announceScreenReader(message);

  let timer = null;
  let focusInsideBeforeHide = false;
  const startTimer = () => {
    if (duration <= 0) return;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => dismissToast(item, { priorFocus }), duration);
  };
  const pauseTimer = () => window.clearTimeout(timer);

  item.addEventListener("pointerenter", pauseTimer);
  item.addEventListener("pointerleave", startTimer);
  item.addEventListener("focusin", pauseTimer);
  item.addEventListener("focusout", startTimer);
  item.addEventListener("beforetoggle", (event) => {
    if (event.newState === "closed") {
      focusInsideBeforeHide = focusInsideBeforeHide || item.contains(document.activeElement);
    }
  });
  item.addEventListener("toggle", (event) => {
    if (event.newState === "closed") {
      pauseTimer();
      dismissToast(item, { restoreFocus: focusInsideBeforeHide, priorFocus });
    }
  });
  closeBtn.addEventListener("click", () => {
    pauseTimer();
    focusInsideBeforeHide = focusInsideBeforeHide || item.contains(document.activeElement);
    if (!Object.hasOwn(HTMLElement.prototype, "popover") || typeof item.hidePopover !== "function") {
      dismissToast(item, { restoreFocus: focusInsideBeforeHide, priorFocus });
    }
  });
  startTimer();
}

function showToast(message, tone = "info", duration = 4500) {
  if (document.querySelector("dialog:modal")) {
    state.deferredToasts.push({ message, tone, duration });
    announceScreenReader(message);
    return;
  }
  renderToastNow(message, tone, duration);
}

function flushDeferredToasts() {
  if (document.querySelector("dialog:modal") || state.deferredToasts.length === 0) return;
  const pending = state.deferredToasts.splice(0, state.deferredToasts.length);
  for (const item of pending) {
    renderToastNow(item.message, item.tone, item.duration);
  }
}

/* API client */
async function apiFetch(url, options = {}) {
  const { json, timeoutMs = 20000, ...fetchOpts } = options;
  const method = (fetchOpts.method || "GET").toUpperCase();
  const headers = new Headers(fetchOpts.headers || {});
  if (method !== "GET" && method !== "HEAD" && CSRF_TOKEN) {
    headers.set("X-ColabLLM-CSRF", CSRF_TOKEN);
  }
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  let signal = fetchOpts.signal;
  if (!signal && timeoutMs > 0 && typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    signal = AbortSignal.timeout(timeoutMs);
  }
  let resp;
  try {
    resp = await fetch(url, {
      ...fetchOpts,
      method,
      headers,
      signal,
      credentials: "same-origin",
      body: json !== undefined ? JSON.stringify(json) : fetchOpts.body,
    });
  } catch (err) {
    if (err && (err.name === "TimeoutError" || err.name === "AbortError")) {
      throw new Error("请求本机控制台超时：若打开了过多控制台标签页，请关闭多余标签页或刷新后重试");
    }
    throw err;
  }
  const ct = resp.headers.get("Content-Type") || "";
  if (ct.includes("application/json")) {
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || `请求失败 (${resp.status})`);
    }
    return data;
  }
  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(text || `请求失败 (${resp.status})`);
  }
  return text;
}

/* Theme management */
function initTheme() {
  const toggle = document.getElementById("theme-toggle");
  const apply = (pref) => {
    if (pref === "light" || pref === "dark") {
      document.documentElement.dataset.theme = pref;
      toggle.checked = pref === "dark";
    } else {
      delete document.documentElement.dataset.theme;
      toggle.checked = state.themeMq.matches;
    }
  };
  let saved = null;
  try {
    saved = localStorage.getItem("colabllm-theme");
  } catch {
    saved = null;
  }
  apply(saved);
  toggle.addEventListener("change", () => {
    const mode = toggle.checked ? "dark" : "light";
    try {
      localStorage.setItem("colabllm-theme", mode);
    } catch {
      // Ignore storage restrictions.
    }
    apply(mode);
  });
  state.themeMq.addEventListener("change", () => {
    let current = null;
    try {
      current = localStorage.getItem("colabllm-theme");
    } catch {
      current = null;
    }
    if (!current) apply(null);
  });
}

/* Navigation & Tabs */
function syncKpiStates() {
  const onInstances = state.activeFleetPanel === "fleet-panel-instances";
  const onCatalog = state.activeFleetPanel === "fleet-panel-catalog";
  document.getElementById("kpi-card-running")?.setAttribute(
    "aria-pressed",
    onInstances && state.instanceStatusFilter === "running" ? "true" : "false",
  );
  document.getElementById("kpi-card-ready")?.setAttribute(
    "aria-pressed",
    onInstances && state.instanceStatusFilter === "ready" ? "true" : "false",
  );
  document.getElementById("kpi-card-services")?.setAttribute(
    "aria-pressed",
    onCatalog ? "true" : "false",
  );
}

function switchView(viewName) {
  state.activeView = viewName;
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    if (btn.dataset.view === viewName) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
  document.querySelectorAll(".view-section").forEach((sec) => {
    sec.hidden = sec.id !== `view-${viewName}`;
  });
  const [title, subtitle] = VIEW_META[viewName] || ["ColabLLM", ""];
  document.getElementById("view-title").textContent = title;
  document.getElementById("view-subtitle").textContent = subtitle;
  if (viewName === "tunnel" && !state.accounts?.usage_summary && !state.accountsUsageLoading) {
    void refreshAccountsUsage({ force: false, silent: true });
  }
}

function activateTab(panelId) {
  const targetPanel = document.getElementById(panelId);
  if (!targetPanel) return;
  const scope = targetPanel.closest(".tabs-shell") || document;
  const inactiveHiddenVal = "onbeforematch" in HTMLElement.prototype ? "until-found" : "";

  scope.querySelectorAll(":scope > .tablist > .tab-btn").forEach((btn) => {
    const active = btn.dataset.panel === panelId;
    btn.setAttribute("aria-selected", active ? "true" : "false");
    if (active) btn.setAttribute("focusgroupstart", "");
    else btn.removeAttribute("focusgroupstart");
    if (!Object.hasOwn(HTMLElement.prototype, "focusGroup")) {
      btn.tabIndex = active ? 0 : -1;
    }
  });
  scope.querySelectorAll(':scope > [role="tabpanel"]').forEach((panel) => {
    if (panel.id === panelId) panel.removeAttribute("hidden");
    else panel.setAttribute("hidden", inactiveHiddenVal);
  });

  if (scope.closest("#view-fleet")) {
    state.activeFleetPanel = panelId;
    syncKpiStates();
  }
}

function initNavigation() {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
  document.querySelectorAll(".tabs-shell:not(.ck-tabs-shell) > .tablist > .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.panel));
  });
  document.querySelectorAll('[role="tabpanel"]').forEach((panel) => {
    panel.addEventListener("beforematch", () => {
      if (panel.closest("#view-fleet")) {
        switchView("fleet");
      } else if (panel.closest("#view-copilot")) {
        switchView("copilot");
      } else if (panel.closest("#view-workbench")) {
        switchView("workbench");
      }
      activateTab(panel.id);
    });
  });
}

/* Dialogs & shared schema field rendering */
function fillSelectOptions(selectEl, items, { emptyLabel = null, valueFn = (x) => x, labelFn = (x) => x } = {}) {
  const prev = selectEl.value;
  const opts = [];
  if (emptyLabel !== null) {
    opts.push(el("option", { value: "", textContent: emptyLabel }));
  }
  for (const item of items) {
    opts.push(el("option", { value: valueFn(item), textContent: labelFn(item) }));
  }
  selectEl.replaceChildren(...opts);
  if (prev && Array.from(selectEl.options).some((o) => o.value === prev)) {
    selectEl.value = prev;
  }
}

function buildSchemaControl(field, idPrefix) {
  const inputId = `${idPrefix}-${field.id.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
  const labelTitle = field.flags && field.flags.length ? field.flags.join(" / ") : field.name;
  const helpText = field.help || "";
  let widget;
  let readValue;

  if (field.kind === "switch" || field.kind === "store_false") {
    widget = el("input", { id: inputId, type: "checkbox", switch: true });
    enhanceSwitch(widget);
    readValue = () => widget.checked;
    const row = el(
      "label",
      { className: "switch-row", title: helpText },
      el("span", { textContent: labelTitle }),
      widget,
    );
    return { element: row, input: widget, readValue };
  }

  if (field.kind === "boolean_optional") {
    const defLabel = field.default === true ? "默认(开启)" : field.default === false ? "默认(关闭)" : "使用默认";
    widget = el(
      "select",
      { id: inputId },
      el("option", { value: "", textContent: defLabel }),
      el("option", { value: "true", textContent: `启用 (${field.flags[0] || "--flag"})` }),
      el("option", { value: "false", textContent: `禁用 (${field.flags[1] || "--no-flag"})` }),
    );
    readValue = () => (widget.value === "" ? null : widget.value === "true");
  } else if (field.kind === "select") {
    const opts = [];
    if (!field.required) {
      const defSuffix = field.default !== null && field.default !== undefined ? ` (默认: ${field.default})` : "";
      opts.push(el("option", { value: "", textContent: `使用默认${defSuffix}` }));
    }
    for (const choice of field.choices || []) {
      opts.push(el("option", { value: String(choice), textContent: String(choice) }));
    }
    widget = el("select", { id: inputId, required: field.required }, ...opts);
    if (field.required && field.default !== null && field.default !== undefined) {
      widget.value = String(field.default);
    }
    readValue = () => (widget.value === "" ? null : widget.value);
  } else if (field.kind === "optional_value") {
    const chk = el("input", { id: `${inputId}-chk`, type: "checkbox", switch: true });
    enhanceSwitch(chk);
    const valInput = el("input", {
      id: inputId,
      type: field.secret ? "password" : "text",
      autocomplete: "off",
      placeholder: field.const ? `开启时默认=${field.const}` : "可选参数值",
      disabled: true,
    });
    chk.addEventListener("change", () => {
      valInput.disabled = !chk.checked;
    });
    const wrap = el(
      "div",
      { className: "card", title: helpText },
      el("label", { className: "switch-row" }, el("span", { textContent: labelTitle }), chk),
      valInput,
      helpText ? el("small", { className: "muted", textContent: helpText }) : null,
    );
    readValue = () => (chk.checked ? { enabled: true, value: valInput.value.trim() } : null);
    return { element: wrap, input: valInput, readValue };
  } else if (field.kind === "repeatable") {
    widget = el("textarea", {
      id: inputId,
      rows: 2,
      placeholder: "每行填入一个值（可多次传入）",
      required: field.required,
    });
    readValue = () => {
      const lines = widget.value
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter(Boolean);
      return lines.length ? lines : null;
    };
  } else {
    const isNum = field.kind === "int" || field.kind === "float";
    const placeholder =
      field.default !== null && field.default !== undefined
        ? `默认: ${field.default}`
        : field.metavar || "";
    widget = el("input", {
      id: inputId,
      type: field.secret ? "password" : isNum ? "number" : "text",
      step: field.kind === "int" ? "1" : isNum ? "any" : undefined,
      autocomplete: field.secret ? "off" : undefined,
      placeholder,
      required: field.required,
    });
    readValue = () => {
      const val = widget.value.trim();
      return val === "" ? null : val;
    };
  }

  const labelEl = el(
    "label",
    { title: helpText },
    el("span", { textContent: field.required ? `${labelTitle} *` : labelTitle }),
    widget,
    helpText ? el("small", { className: "muted", textContent: helpText }) : null,
  );
  return { element: labelEl, input: widget, readValue };
}

/* State loading & Fleet Rendering */
async function ensureSchema(serviceName) {
  if (state.schemas.has(serviceName)) return state.schemas.get(serviceName);
  const schema = await apiFetch(`/api/services/${encodeURIComponent(serviceName)}/schema`);
  state.schemas.set(serviceName, schema);
  return schema;
}

const fetchingFailedTaskIds = new Set();

function tokenizeShellPreview(cmd) {
  const tokens = [];
  const src = String(cmd || "");
  let cur = "";
  let quote = "";
  let inToken = false;
  for (let i = 0; i < src.length; i += 1) {
    const ch = src[i];
    if (quote === "'") {
      if (ch === "'") quote = "";
      else cur += ch;
      inToken = true;
      continue;
    }
    if (quote === '"') {
      if (ch === '"') quote = "";
      else if (ch === "\\" && i + 1 < src.length) {
        i += 1;
        cur += src[i];
      } else {
        cur += ch;
      }
      inToken = true;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      inToken = true;
    } else if (ch === "\\" && i + 1 < src.length) {
      i += 1;
      cur += src[i];
      inToken = true;
    } else if (/\s/.test(ch)) {
      if (inToken) {
        tokens.push(cur);
        cur = "";
        inToken = false;
      }
    } else {
      cur += ch;
      inToken = true;
    }
  }
  if (inToken) tokens.push(cur);
  return tokens;
}

function getServiceProvider(serviceName) {
  if (!serviceName) return "colab";
  const svc = state.servicesMap.get(serviceName);
  if (svc && svc.provider) return svc.provider;
  return String(serviceName).startsWith("kaggle-") ? "kaggle" : "colab";
}

function createProviderBadge(provider) {
  const isKaggle = provider === "kaggle";
  return el("span", {
    className: isKaggle ? "badge provider-kaggle" : "badge provider-colab",
    textContent: isKaggle ? "Kaggle" : "Colab",
  });
}

function parseLifecycleTask(task) {
  if (
    !task ||
    !task.service ||
    (task.subcommand !== "up" && task.subcommand !== "down" && task.subcommand !== "restart")
  ) {
    return null;
  }
  const tokens = tokenizeShellPreview(task.command_preview);
  let instanceArg = "";
  let account = "";
  let driveAccount = "";
  let transport = "";
  let gpu = "";
  let downAll = false;

  const readFlagValue = (idx, eqPrefixList) => {
    const tok = tokens[idx];
    for (const prefix of eqPrefixList) {
      if (tok.startsWith(prefix)) {
        return { value: tok.slice(prefix.length), consumed: 0 };
      }
    }
    if (idx + 1 < tokens.length) {
      return { value: tokens[idx + 1], consumed: 1 };
    }
    return { value: "", consumed: 0 };
  };

  for (let i = 0; i < tokens.length; i += 1) {
    const tok = tokens[i];
    if (tok === "-i" || tok === "--instance" || tok.startsWith("-i=") || tok.startsWith("--instance=")) {
      const res = readFlagValue(i, ["-i=", "--instance="]);
      instanceArg = res.value === "default" ? "" : res.value;
      i += res.consumed;
    } else if (tok === "-a" || tok === "--account" || tok.startsWith("-a=") || tok.startsWith("--account=")) {
      const res = readFlagValue(i, ["-a=", "--account="]);
      account = res.value;
      i += res.consumed;
    } else if (tok === "--drive-account" || tok.startsWith("--drive-account=")) {
      const res = readFlagValue(i, ["--drive-account="]);
      driveAccount = res.value;
      i += res.consumed;
    } else if (tok === "--transport" || tok.startsWith("--transport=")) {
      const res = readFlagValue(i, ["--transport="]);
      transport = res.value;
      i += res.consumed;
    } else if (
      tok === "--gpu" ||
      tok.startsWith("--gpu=") ||
      tok === "--accelerator" ||
      tok.startsWith("--accelerator=")
    ) {
      const res = readFlagValue(i, ["--gpu=", "--accelerator="]);
      gpu = res.value;
      i += res.consumed;
    } else if (tok === "--all" && task.subcommand === "down") {
      downAll = true;
    }
  }

  const provider = getServiceProvider(task.service);
  const sessionName = instanceArg ? `${task.service}-${instanceArg}` : task.service;
  const displayLabel = instanceArg ? `${task.service} · #${instanceArg}` : task.service;
  return {
    taskId: task.task_id,
    service: task.service,
    provider,
    subcommand: task.subcommand,
    instanceArg,
    account,
    driveAccount,
    transport,
    gpu,
    downAll,
    sessionName,
    displayLabel,
  };
}

function inferLifecycleTaskHint(subcommand, rawOutput) {
  const text = String(rawOutput || "");
  if (subcommand === "down") {
    return /delete-tunnel|tunnel/i.test(text)
      ? "正在停止实例并清理隧道..."
      : "正在停止实例与清理远端会话...";
  }
  if (subcommand === "restart") {
    return "正在重启远端 Kaggle 内核并复用缓存...";
  }
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  for (const line of lines.slice(-20).reverse()) {
    if (/CT_STAGE=/i.test(line)) {
      const stageVal = line.split("CT_STAGE=")[1]?.trim();
      if (stageVal) return `阶段：${stageVal.slice(0, 48)}`;
    }
    if (/runner started/i.test(line)) return "后台进程已启动，正在连接 Notebook...";
    if (/远端服务已在运行/.test(line)) return "远端服务已在运行，正在接管保活...";
    if (/kaggle session create/i.test(line)) return "正在创建 Kaggle 会话...";
    if (/reusing.*kaggle session/i.test(line)) return "正在复用运行中的 Kaggle 会话...";
    if (/colab dfs mount/i.test(line)) return "正在挂载 Google Drive...";
    if (/colab new/i.test(line)) return "正在申请 Colab GPU 会话 (约 10–30 秒)...";
    if (/reusing Colab session/i.test(line)) return "正在复用已有 Colab 会话...";
    if (/recreating|stale Colab session/i.test(line)) return "旧会话已回收，正在重建 Colab 会话...";
    if (/tunnel|frp|route|cname/i.test(line)) return "正在配置公网隧道与路由...";
    if (/^service\s+\S+:/i.test(line) || /cell\s+\d+/i.test(line)) {
      return "正在校验参数与配置公网隧道...";
    }
  }
  return "正在准备启动环境...";
}

function extractTaskFailureReason(rawOutput, returncode) {
  const lines = String(rawOutput || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !/^Traceback \(most recent call last\):$/i.test(line));
  if (lines.length > 0) {
    const last = lines[lines.length - 1];
    return last.length > 120 ? `${last.slice(0, 117)}...` : last;
  }
  return `退出码 ${returncode ?? "?"}`;
}

function rebuildEffectiveInstances() {
  const bySession = new Map();
  for (const inst of state.rawInstances) {
    if (inst.running) {
      state.recentTaskFailures.delete(inst.session_name);
    }
    bySession.set(inst.session_name, {
      ...inst,
      _rawRunning: Boolean(inst.running),
      provisioning: false,
      stopping: false,
      _pendingTask: null,
      _failedTask: null,
      _virtual: false,
    });
  }

  for (const t of [...state.tasks].reverse()) {
    if (t.status !== "running") continue;
    const parsed = parseLifecycleTask(t);
    if (!parsed) continue;
    const rawTail = t.output_tail || t.output || "";

    if (parsed.subcommand === "up") {
      state.recentTaskFailures.delete(parsed.sessionName);
      const hint = inferLifecycleTaskHint("up", rawTail) || "正在准备启动环境...";
      const ts = Math.max(t.created_at || 0, 0) || Date.now() / 1000;
      if (bySession.has(parsed.sessionName)) {
        const item = bySession.get(parsed.sessionName);
        item._pendingTask = t;
        if (!item._rawRunning) {
          item.running = true;
          item.ready = false;
          item.provisioning = true;
          item.state_label = "STARTING";
          item.stage_hint = hint;
          item.failed_exit = false;
          item.corrupt = false;
          item.status_rank = 1;
          item.updated_ts = Math.max(item.updated_ts || 0, ts);
          if (parsed.account) item.account = parsed.account;
          if (parsed.driveAccount) item.drive_account = parsed.driveAccount;
          if (parsed.transport) item.transport = parsed.transport;
          if (parsed.gpu) item.session_gpu = parsed.gpu;
        }
      } else {
        const svcMeta = state.servicesMap.get(parsed.service);
        const cat = svcMeta?.category || "other";
        bySession.set(parsed.sessionName, {
          service: parsed.service,
          instance: parsed.instanceArg || "default",
          instance_arg: parsed.instanceArg,
          session_name: parsed.sessionName,
          running: true,
          ready: false,
          provisioning: true,
          stopping: false,
          _rawRunning: false,
          _pendingTask: t,
          _failedTask: null,
          _virtual: true,
          state_label: "STARTING",
          stage_hint: hint,
          failed_exit: false,
          status_rank: 1,
          url: null,
          hostname: null,
          transport: parsed.transport || state.tunnel?.transport || "cloudflare",
          account: parsed.account || state.defaultAccount || null,
          drive_account: parsed.driveAccount || null,
          session_gpu: parsed.gpu || svcMeta?.gpu || null,
          log_exists: false,
          corrupt: false,
          updated_ts: ts,
          category: cat,
          category_label: svcMeta?.category_label || CATEGORY_LABELS[cat] || cat,
          has_web_ui: Boolean(svcMeta?.has_web_ui),
          chat_compatible: Boolean(svcMeta?.chat_compatible),
        });
      }
    } else if (parsed.subcommand === "down") {
      const hint = inferLifecycleTaskHint("down", rawTail) || "正在停止实例与清理 Colab 会话...";
      const ts = Math.max(t.created_at || 0, 0) || Date.now() / 1000;
      const targets = parsed.downAll
        ? Array.from(bySession.values()).filter((item) => item.service === parsed.service && item.running)
        : bySession.has(parsed.sessionName)
          ? [bySession.get(parsed.sessionName)]
          : [];
      for (const item of targets) {
        item.stopping = true;
        item.provisioning = false;
        item._pendingTask = t;
        item.ready = false;
        item.state_label = "STOPPING";
        item.stage_hint = hint;
        item.status_rank = 1;
        item.updated_ts = Math.max(item.updated_ts || 0, ts);
      }
    }
  }

  for (const [sessionName, failInfo] of state.recentTaskFailures.entries()) {
    if (bySession.has(sessionName)) {
      const item = bySession.get(sessionName);
      if (!item.running && !item.provisioning && !item.stopping) {
        item.failed_exit = true;
        item.state_label = "ERROR";
        item.stage_hint = `启动失败: ${failInfo.message}`;
        item.status_rank = 2;
        item._failedTask = failInfo;
        item.updated_ts = Math.max(item.updated_ts || 0, failInfo.ts);
      }
    } else {
      const { parsed } = failInfo;
      const svcMeta = state.servicesMap.get(parsed.service);
      const cat = svcMeta?.category || "other";
      bySession.set(sessionName, {
        service: parsed.service,
        instance: parsed.instanceArg || "default",
        instance_arg: parsed.instanceArg,
        session_name: sessionName,
        running: false,
        ready: false,
        provisioning: false,
        stopping: false,
        _rawRunning: false,
        _pendingTask: null,
        _failedTask: failInfo,
        _virtual: true,
        state_label: "ERROR",
        stage_hint: `启动失败: ${failInfo.message}`,
        failed_exit: true,
        status_rank: 2,
        url: null,
        hostname: null,
        transport: parsed.transport || state.tunnel?.transport || "cloudflare",
        account: parsed.account || state.defaultAccount || null,
        drive_account: parsed.driveAccount || null,
        session_gpu: parsed.gpu || svcMeta?.gpu || null,
        log_exists: false,
        corrupt: false,
        updated_ts: failInfo.ts,
        category: cat,
        category_label: svcMeta?.category_label || CATEGORY_LABELS[cat] || cat,
        has_web_ui: Boolean(svcMeta?.has_web_ui),
        chat_compatible: Boolean(svcMeta?.chat_compatible),
      });
    }
  }

  state.instances = Array.from(bySession.values());
}

function refreshEffectiveFleet() {
  rebuildEffectiveInstances();
  populateGroupedServiceSelect(document.getElementById("up-service"));
  populateGroupedServiceSelect(document.getElementById("wb-service"));
  renderKPIs();
  renderInstances();
  renderServices();
  copilotController?.syncCopilotState();
  renderTasksList();
}

function recordLifecycleFailure(task, parsed, rawOutput) {
  if (!task || state.notifiedTaskIds.has(task.task_id)) return;
  state.notifiedTaskIds.add(task.task_id);
  const reason = extractTaskFailureReason(rawOutput, task.returncode);
  if (parsed.subcommand === "up") {
    state.recentTaskFailures.set(parsed.sessionName, {
      taskId: task.task_id,
      parsed,
      message: reason,
      output: rawOutput || "",
      commandPreview: task.command_preview || "",
      ts: task.finished_at || Date.now() / 1000,
    });
  }
  refreshEffectiveFleet();
  const actionLabel = parsed.subcommand === "down" ? "停止" : "启动";
  showToast(`${parsed.displayLabel} ${actionLabel}失败: ${reason}`, "danger", 8000);
}

function reconcileTasksAndInstances(rawInstances, tasks, { seedOnly = false } = {}) {
  state.rawInstances = rawInstances || [];
  state.tasks = tasks || [];

  const newlyFailed = [];
  for (const t of state.tasks) {
    const parsed = parseLifecycleTask(t);
    if (seedOnly) {
      state.knownTaskStatuses.set(t.task_id, t.status);
      if (t.status !== "running") {
        state.notifiedTaskIds.add(t.task_id);
      }
      continue;
    }

    if (!parsed) continue;
    state.knownTaskStatuses.set(t.task_id, t.status);
    if (t.status === "failed" && !state.notifiedTaskIds.has(t.task_id) && !fetchingFailedTaskIds.has(t.task_id)) {
      newlyFailed.push({ task: t, parsed });
    }
  }

  rebuildEffectiveInstances();

  for (const { task: t, parsed } of newlyFailed) {
    if (typeof t.output === "string" || (typeof t.output_tail === "string" && !t.truncated)) {
      recordLifecycleFailure(t, parsed, t.output ?? t.output_tail ?? "");
      continue;
    }
    fetchingFailedTaskIds.add(t.task_id);
    apiFetch(`/api/tasks/${encodeURIComponent(t.task_id)}`)
      .then((detail) => {
        recordLifecycleFailure(detail, parsed, detail.output || detail.output_tail || "");
      })
      .catch(() => {
        recordLifecycleFailure(t, parsed, t.output_tail || "");
      })
      .finally(() => {
        fetchingFailedTaskIds.delete(t.task_id);
      });
  }
}

function renderKPIs() {
  const running = state.instances.filter((i) => i.running).length;
  const ready = state.instances.filter((i) => i.running && i.ready).length;
  document.getElementById("kpi-running").textContent = String(running);
  document.getElementById("kpi-ready").textContent = String(ready);
  document.getElementById("kpi-transport").textContent = state.tunnel?.transport || "cloudflare";
  document.getElementById("kpi-services").textContent = String(state.services.length);
  syncKpiStates();
}

function isInstanceError(inst) {
  if (inst.corrupt) return true;
  if (typeof inst.failed_exit === "boolean") return inst.failed_exit;
  return Boolean(!inst.running && inst.runner_exit !== null && inst.runner_exit !== undefined && inst.runner_exit !== 0);
}

function getInstanceStatusRank(inst) {
  if (typeof inst.status_rank === "number" && Number.isFinite(inst.status_rank)) {
    return inst.status_rank;
  }
  if (inst.running && inst.ready) return 0;
  if (inst.running) return 1;
  if (isInstanceError(inst)) return 2;
  return 3;
}

function getInstanceUpdatedTs(inst) {
  if (typeof inst.updated_ts === "number" && Number.isFinite(inst.updated_ts)) {
    return inst.updated_ts;
  }
  if (inst.started) {
    const n = Number(inst.started);
    if (Number.isFinite(n) && n > 0) return n;
    const parsed = Date.parse(String(inst.started));
    if (Number.isFinite(parsed)) return parsed / 1000;
  }
  return 0;
}

function getInstanceCategory(inst) {
  if (inst.category) return inst.category;
  return state.servicesMap.get(inst.service)?.category || "other";
}

function getInstanceCategoryLabel(inst) {
  if (inst.category_label) return inst.category_label;
  const cat = getInstanceCategory(inst);
  return state.servicesMap.get(inst.service)?.category_label || CATEGORY_LABELS[cat] || cat;
}

function getInstanceVisualState(inst) {
  if (inst.stopping) return { stateKey: "starting", badgeTone: "warn", isBusy: true };
  if (isInstanceError(inst)) return { stateKey: "error", badgeTone: "danger", isBusy: false };
  if (inst.running && inst.ready) return { stateKey: "ready", badgeTone: "ok", isBusy: false };
  if (inst.running) return { stateKey: "starting", badgeTone: "warn", isBusy: true };
  return { stateKey: "stopped", badgeTone: "", isBusy: false };
}

function formatRelativeTime(ts) {
  if (!ts || !Number.isFinite(ts) || ts <= 0) return { short: "-", full: "" };
  const date = new Date(ts * 1000);
  const full = Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN");
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  let short;
  if (diffSec < 60) short = "刚刚";
  else if (diffSec < 3600) short = `${Math.floor(diffSec / 60)} 分钟前`;
  else if (diffSec < 86400) short = `${Math.floor(diffSec / 3600)} 小时前`;
  else if (diffSec < 86400 * 7) short = `${Math.floor(diffSec / 86400)} 天前`;
  else short = date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  return { short, full };
}

async function copyTextToClipboard(text, successMsg) {
  try {
    if (!navigator.clipboard?.writeText) {
      throw new Error("clipboard unavailable");
    }
    await navigator.clipboard.writeText(text);
    showToast(successMsg, "ok", 2000);
  } catch {
    if (legacyCopyText(text)) {
      showToast(successMsg, "ok", 2000);
    } else {
      showToast("浏览器拒绝剪贴板访问，请手动复制", "warn");
    }
  }
}

const copyTimers = new WeakMap();
let lastCopyAnnouncement = "";

function legacyCopyText(text) {
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.cssText = "position:fixed;inset-block-start:0;opacity:0";
  document.body.append(field);
  field.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    field.remove();
  }
}

function resetCopyButtonState(button) {
  if (!button) return;
  window.clearTimeout(copyTimers.get(button));
  copyTimers.delete(button);
  button.removeAttribute("data-copied");
  button.removeAttribute("data-failed");
  const tip = button.nextElementSibling;
  if (tip && tip.classList.contains("copy-tip")) {
    tip.textContent = "";
  }
}

function reportCopyButton(button, ok, label) {
  if (!button) return;
  const tip = button.nextElementSibling;
  window.clearTimeout(copyTimers.get(button));
  button.removeAttribute("data-copied");
  button.removeAttribute("data-failed");
  let message = label || (ok ? "已复制" : "复制失败");
  if (message === lastCopyAnnouncement) message += "\u00A0";
  lastCopyAnnouncement = message;
  if (tip && tip.classList.contains("copy-tip")) {
    tip.textContent = "";
  }
  void button.offsetWidth;
  button.setAttribute(ok ? "data-copied" : "data-failed", "");
  if (tip && tip.classList.contains("copy-tip")) {
    window.requestAnimationFrame(() => {
      tip.textContent = message;
    });
  }
  copyTimers.set(
    button,
    window.setTimeout(() => {
      button.removeAttribute("data-copied");
      button.removeAttribute("data-failed");
      if (tip && tip.classList.contains("copy-tip")) {
        tip.textContent = "";
      }
    }, 2000),
  );
}

async function handleCopyCommand(button, target, command = "--copy") {
  if (!button || !target || !command?.startsWith("--copy")) return;
  let text = "";
  if (command === "--copy-token") {
    const inst = state.connectTarget;
    if (!inst) return;
    let token = state.connectSecrets?.api_token;
    if (!token) {
      try {
        const sec = await revealConnectSecrets(inst);
        token = sec?.api_token;
      } catch {
        reportCopyButton(button, false, "获取凭据失败");
        return;
      }
    }
    if (!token) {
      reportCopyButton(button, false, "未配置 Token");
      return;
    }
    text = token;
  } else if (command === "--copy-curl" || command === "--copy-py") {
    const raw = target.textContent || "";
    const parts = raw.split("\n\n# Python OpenAI SDK\n");
    text =
      command === "--copy-curl"
        ? parts[0].replace(/^# curl[^\n]*\n/, "").trim()
        : (parts[1] || raw).trim();
  } else if (target.id === "logs-viewer") {
    text = target.dataset.copyEmpty === "1" ? "" : state.logsDisplayedText || "";
  } else {
    text = target.matches("input, textarea") ? target.value : (target.innerText || target.textContent || "").trim();
  }
  if (!text) {
    reportCopyButton(
      button,
      false,
      target.id === "logs-viewer" ? "暂无可复制日志" : "暂无可复制内容",
    );
    return;
  }
  try {
    if (!navigator.clipboard?.writeText) {
      throw new Error("clipboard unavailable");
    }
    await navigator.clipboard.writeText(text);
    reportCopyButton(button, true, "已复制");
  } catch {
    if (legacyCopyText(text)) {
      reportCopyButton(button, true, "已复制");
    } else {
      reportCopyButton(button, false, "复制失败");
    }
  }
}

function jumpToWorkbench(inst) {
  switchView("workbench");
  activateTab("panel-cli");
  document.getElementById("wb-instance").value = inst.instance_arg || "";
  if (inst.account) {
    const acctSel = document.getElementById("wb-account");
    if (Array.from(acctSel.options).some((o) => o.value === inst.account)) {
      acctSel.value = inst.account;
    }
  }
  selectWorkbenchService(inst.service);
}

function openRemoveDialog(inst) {
  if (inst?._virtual) {
    state.recentTaskFailures.delete(inst.session_name);
    refreshEffectiveFleet();
    return;
  }
  if (state.readOnly) {
    showToast("当前处于只读模式 (--read-only)，禁止移除实例记录", "warn");
    return;
  }
  if (inst.running) {
    showToast("运行中的实例不可移除记录，请先停止实例", "warn");
    return;
  }
  state.removeTarget = inst;
  document.getElementById("remove-dialog-desc").textContent = `确定移除已停止实例 ${inst.session_name} 的本地状态记录吗？`;
  document.getElementById("remove-dialog").showModal();
}

function getPrunableReplicas() {
  return state.instances.filter((i) => !i.running && !i._virtual && Boolean(i.instance_arg));
}

async function cancelProvisioningTask(inst) {
  const taskId = inst?._pendingTask?.task_id;
  if (!taskId) return;
  if (state.readOnly) {
    showToast("当前处于只读模式 (--read-only)，禁止取消任务", "warn");
    return;
  }
  try {
    await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
    showToast(`已取消启动 ${inst.session_name}`, "warn");
    await loadBootstrap();
  } catch (err) {
    showToast(`取消失败: ${err.message}`, "danger");
  }
}

function openPruneDialog() {
  if (state.readOnly) {
    showToast("当前处于只读模式 (--read-only)，禁止清理临时副本", "warn");
    return;
  }
  const replicas = getPrunableReplicas();
  if (!replicas.length) {
    showToast("当前没有可清理的已停止编号副本 (#*)", "info");
    return;
  }
  document.getElementById("prune-dialog-desc").textContent = `即将清理 ${replicas.length} 个已停止编号副本的本地状态目录：`;
  const listEl = document.getElementById("prune-targets-list");
  listEl.replaceChildren(...replicas.map((r) => el("span", { className: "badge", textContent: r.session_name })));
  document.getElementById("prune-dialog").showModal();
}

function reorderContainer(container, desiredNodes) {
  const wanted = new Set(desiredNodes);
  for (const child of Array.from(container.children)) {
    if (!wanted.has(child)) {
      child.remove();
    }
  }
  const current = container.children;
  if (current.length === desiredNodes.length) {
    let same = true;
    for (let i = 0; i < desiredNodes.length; i += 1) {
      if (current[i] !== desiredNodes[i]) {
        same = false;
        break;
      }
    }
    if (same) return;
  }
  const hasMoveBefore = Object.hasOwn(Element.prototype, "moveBefore");
  const move = hasMoveBefore
    ? (n) => (n.isConnected && container.isConnected ? container.moveBefore(n, null) : container.append(n))
    : (n) => container.append(n);
  for (const node of desiredNodes) {
    move(node);
  }
}

async function restartKaggleInstance(inst) {
  if (state.readOnly) {
    showToast("当前处于只读模式 (--read-only)，禁止重启内核", "warn");
    return;
  }
  try {
    const task = await apiFetch(`/api/instances/${encodeURIComponent(inst.service)}/restart`, {
      method: "POST",
      json: {
        instance: inst.instance_arg || "",
        account: inst.account || "",
      },
    });
    const nextTasks = [task, ...state.tasks.filter((x) => x.task_id !== task.task_id)];
    reconcileTasksAndInstances(state.rawInstances, nextTasks);
    refreshEffectiveFleet();
    showToast(`已触发重启 ${inst.session_name} 内核...`, "ok");
    loadBootstrap().catch(() => {});
  } catch (err) {
    showToast(`重启失败: ${err.message}`, "danger");
  }
}

function createActionCluster(surface, idSeq, getInst) {
  const popId = `inst-pop-${surface}-${idSeq}`;
  const anchorName = `--anc-${surface}-${idSeq}`;
  const moreBtn = el("button", {
    type: "button",
    className: "btn sm secondary icon-btn",
    popovertarget: popId,
    "aria-label": "更多操作",
    styleProps: { "anchor-name": anchorName },
    textContent: "⋯",
  });
  const popEl = el("div", {
    id: popId,
    popover: "auto",
    className: "action-popover",
    styleProps: { "position-anchor": anchorName },
  });
  const actionsEl = el(
    "div",
    { className: surface === "table" ? "table-actions-cell" : "card-actions" },
    moreBtn,
    popEl,
  );

  let lastInlineSig = "";
  let lastPopSig = "";

  const sync = (inst) => {
    const urlHref = safeExternalUrl(inst.url);
    const titleText = inst.instance_arg ? `${inst.service} · #${inst.instance_arg}` : inst.service;
    const inlineSig = `${Boolean(inst.running)}|${Boolean(inst.provisioning)}|${Boolean(inst.stopping)}|${Boolean(inst._failedTask)}|${Boolean(inst._virtual)}|${Boolean(inst.supports_restart)}|${urlHref || ""}|${Boolean(inst.has_web_ui)}|${Boolean(inst.chat_compatible)}|${Boolean(state.readOnly)}|${titleText}`;
    if (inlineSig !== lastInlineSig) {
      lastInlineSig = inlineSig;
      const inlineButtons = [];
      if (inst.stopping) {
        inlineButtons.push(
          el("button", {
            type: "button",
            className: "btn sm secondary is-busy",
            disabled: true,
            textContent: "正在停止...",
          }),
          el("button", {
            type: "button",
            className: "btn sm secondary",
            textContent: "实时日志",
            onClick: () => openLogsDialog(getInst()),
          }),
        );
      } else if (inst.provisioning) {
        inlineButtons.push(
          el("button", {
            type: "button",
            className: "btn sm secondary",
            textContent: "实时日志",
            onClick: () => openLogsDialog(getInst()),
          }),
          el("button", {
            type: "button",
            className: "btn sm danger",
            disabled: state.readOnly,
            textContent: "取消启动",
            onClick: () => cancelProvisioningTask(getInst()),
          }),
        );
      } else if (inst.running) {
        if (urlHref && inst.has_web_ui) {
          inlineButtons.push(
            el(
              "a",
              {
                className: "btn sm primary",
                href: urlHref,
                target: "_blank",
                rel: "noopener noreferrer",
              },
              "打开 Web UI",
              el("span", { className: "sr-only", textContent: ` (${titleText})` }),
            ),
          );
        } else if (urlHref) {
          inlineButtons.push(
            el("button", {
              type: "button",
              className: "btn sm primary",
              textContent: "AI 演练场",
              onClick: () => copilotController?.openInstanceInCopilot(getInst()),
            }),
          );
        }
        if (inst.supports_restart) {
          inlineButtons.push(
            el("button", {
              type: "button",
              className: "btn sm secondary",
              disabled: state.readOnly,
              textContent: "重启内核",
              onClick: () => restartKaggleInstance(getInst()),
            }),
          );
        }
        inlineButtons.push(
          el("button", {
            type: "button",
            className: "btn sm secondary",
            textContent: "实时日志",
            onClick: () => openLogsDialog(getInst()),
          }),
          el("button", {
            type: "button",
            className: "btn sm danger",
            disabled: state.readOnly,
            textContent: "停止",
            onClick: () => openDownDialog(getInst()),
          }),
        );
      } else {
        inlineButtons.push(
          el("button", {
            type: "button",
            className: "btn sm secondary",
            disabled: state.readOnly,
            textContent: "重新启动",
            onClick: () => {
              const cur = getInst();
              openUpDialog(cur.service, cur.instance_arg, cur.account || "");
            },
          }),
          el("button", {
            type: "button",
            className: "btn sm secondary",
            textContent: "实时日志",
            onClick: () => openLogsDialog(getInst()),
          }),
        );
      }
      while (actionsEl.firstChild && actionsEl.firstChild !== moreBtn) {
        actionsEl.firstChild.remove();
      }
      actionsEl.prepend(...inlineButtons);
      moreBtn.setAttribute("aria-label", `更多操作 (${titleText})`);
    }

    const popSig = `${Boolean(inst.running)}|${Boolean(inst.provisioning)}|${Boolean(inst.stopping)}|${Boolean(inst._failedTask)}|${Boolean(inst._virtual)}|${Boolean(inst.url)}|${Boolean(inst.chat_compatible)}|${Boolean(state.readOnly)}`;
    if (popSig !== lastPopSig) {
      lastPopSig = popSig;
      const popItems = [];
      if (!inst._virtual) {
        popItems.push(
          el("button", {
            type: "button",
            className: "btn sm secondary",
            textContent: "连接/凭据",
            onClick: () => {
              closePopover(popEl);
              openConnectDialog(getInst());
            },
          }),
        );
      }
      if (inst.running && !inst.provisioning && !inst.stopping && !inst._virtual) {
        popItems.push(
          el("button", {
            type: "button",
            className: "btn sm secondary",
            textContent: "探活",
            onClick: () => {
              closePopover(popEl);
              runProbeInstance(getInst());
            },
          }),
        );
      }
      if (inst.running && !inst.stopping && inst.url) {
        popItems.push(
          el("button", {
            type: "button",
            className: "btn sm secondary",
            textContent: "AI 演练场",
            onClick: () => {
              closePopover(popEl);
              copilotController?.openInstanceInCopilot(getInst());
            },
          }),
          el("button", {
            type: "button",
            className: "btn sm secondary",
            textContent: "快捷 Copilot",
            onClick: () => {
              closePopover(popEl);
              copilotController?.openDrawer(getInst().session_name);
            },
          }),
        );
      }
      popItems.push(
        el("button", {
          type: "button",
          className: "btn sm secondary",
          textContent: "工作台命令",
          onClick: () => {
            closePopover(popEl);
            jumpToWorkbench(getInst());
          },
        }),
      );
      if (!inst.running && !inst.stopping) {
        if (inst._virtual) {
          popItems.push(
            el("div", { className: "action-popover-divider", "aria-hidden": "true" }),
            el("button", {
              type: "button",
              className: "btn sm secondary",
              textContent: "清除失败提示",
              onClick: () => {
                closePopover(popEl);
                const cur = getInst();
                state.recentTaskFailures.delete(cur.session_name);
                refreshEffectiveFleet();
              },
            }),
          );
        } else {
          popItems.push(
            el("div", { className: "action-popover-divider", "aria-hidden": "true" }),
            el("button", {
              type: "button",
              className: "btn sm danger",
              disabled: state.readOnly,
              textContent: "移除记录",
              onClick: () => {
                closePopover(popEl);
                openRemoveDialog(getInst());
              },
            }),
          );
        }
      }
      popEl.replaceChildren(...popItems);
    }
  };

  return { el: actionsEl, sync };
}

function createUrlCopyRow(linkId, urlHref, linkClass, wrapClass) {
  return el(
    "div",
    { className: wrapClass },
    el("a", {
      id: linkId,
      className: linkClass,
      href: urlHref,
      target: "_blank",
      rel: "noopener noreferrer",
      title: urlHref,
      textContent: urlHref,
    }),
    el(
      "span",
      { className: "copy-wrap" },
      el(
        "button",
        {
          type: "button",
          className: "btn sm secondary copy-btn",
          command: "--copy",
          commandfor: linkId,
        },
        el("span", { className: "copy-icon", "aria-hidden": "true" }),
        el("span", { textContent: "复制" }),
      ),
      el("span", { className: "copy-tip", role: "status" }),
    ),
  );
}

function getOrUpdateInstanceNodes(inst) {
  let entry = instanceNodeCache.get(inst.session_name);
  if (!entry) {
    instanceSlotSeq += 1;
    const idSeq = instanceSlotSeq;

    // 1. Card node
    const cardTitleEl = el("strong");
    const cardStageEl = el("span", { className: "muted" });
    const cardProviderEl = el("span", { className: "badge" });
    const cardBadgeEl = el("span", { className: "badge" });
    const metaAccountEl = el("span");
    const metaDriveEl = el("span");
    const metaTransportEl = el("span");
    const metaGpuEl = el("span");
    const metaUpdatedEl = el("span");
    const cardUrlBox = el("div");
    const cardActions = createActionCluster("card", idSeq, () => entry.inst);
    const cardArticle = el(
      "article",
      { className: "card instance-card" },
      el(
        "div",
        { className: "card-head" },
        el("div", { className: "card-title-group" }, cardTitleEl, cardStageEl),
        el("div", { className: "card-badges" }, cardProviderEl, cardBadgeEl),
      ),
      el(
        "div",
        { className: "card-layout" },
        el(
          "div",
          { className: "card-info" },
          el(
            "div",
            { className: "card-meta" },
            metaAccountEl,
            metaDriveEl,
            metaTransportEl,
            metaGpuEl,
            metaUpdatedEl,
          ),
          cardUrlBox,
        ),
        cardActions.el,
      ),
    );
    const cardSlot = el("div", { className: "card-slot" }, cardArticle);

    // 2. Table row node
    const tdStatusBadge = el("span", { className: "badge" });
    const tdStatusStage = el("span", { className: "table-subline" });
    const tdStatus = el("td", null, tdStatusBadge, tdStatusStage);

    const tdNameTitle = el("strong");
    const tdNameCat = el("span", { className: "table-subline" });
    const tdName = el("td", null, tdNameTitle, tdNameCat);

    const tdAcctMain = el("span");
    const tdAcctDrive = el("span", { className: "table-subline" });
    const tdAccount = el("td", null, tdAcctMain, tdAcctDrive);

    const tdGpuMain = el("span");
    const tdGpuTrans = el("span", { className: "table-subline" });
    const tdGpu = el("td", null, tdGpuMain, tdGpuTrans);

    const tdUpdatedText = el("span");
    const tdUpdated = el("td", null, tdUpdatedText);

    const tdUrl = el("td");
    const rowActions = createActionCluster("table", idSeq, () => entry.inst);
    const tdActions = el("td", null, rowActions.el);

    const row = el("tr", null, tdStatus, tdName, tdAccount, tdGpu, tdUpdated, tdUrl, tdActions);

    // 3. Grouped replica row node
    const grpInstLabel = el("strong");
    const grpBadgeEl = el("span", { className: "badge" });
    const grpStageEl = el("span", { className: "muted" });
    const grpMetaEl = el("div", { className: "card-meta" });
    const grpUrlBox = el("div");
    const groupActions = createActionCluster("group", idSeq, () => entry.inst);
    const groupRow = el(
      "div",
      { className: "grouped-replica-row" },
      el(
        "div",
        { className: "grouped-replica-main" },
        el("div", { className: "grouped-replica-head" }, grpInstLabel, grpBadgeEl, grpStageEl),
        grpMetaEl,
        grpUrlBox,
      ),
      groupActions.el,
    );

    entry = {
      idSeq,
      inst,
      urlSig: null,
      cardSlot,
      cardArticle,
      cardTitleEl,
      cardStageEl,
      cardProviderEl,
      cardBadgeEl,
      metaAccountEl,
      metaDriveEl,
      metaTransportEl,
      metaGpuEl,
      metaUpdatedEl,
      cardUrlBox,
      cardActions,
      row,
      tdStatus,
      tdStatusBadge,
      tdStatusStage,
      tdName,
      tdNameTitle,
      tdNameCat,
      tdAccount,
      tdAcctMain,
      tdAcctDrive,
      tdGpu,
      tdGpuMain,
      tdGpuTrans,
      tdUpdated,
      tdUpdatedText,
      tdUrl,
      rowActions,
      groupRow,
      grpInstLabel,
      grpBadgeEl,
      grpStageEl,
      grpMetaEl,
      grpUrlBox,
      groupActions,
    };
    instanceNodeCache.set(inst.session_name, entry);
  }

  entry.inst = inst;
  const { stateKey, badgeTone, isBusy } = getInstanceVisualState(inst);
  const badgeCls = [badgeTone ? `badge ${badgeTone}` : "badge", isBusy ? "is-busy" : ""].filter(Boolean).join(" ");
  const urlHref = safeExternalUrl(inst.url);
  const titleText = inst.instance_arg ? `${inst.service} · #${inst.instance_arg}` : inst.service;
  const stageText = inst.stage_hint || inst.state_label || "";
  const statusRank = getInstanceStatusRank(inst);
  const updatedTs = getInstanceUpdatedTs(inst);
  const relTime = formatRelativeTime(updatedTs);
  const catLabel = getInstanceCategoryLabel(inst);

  const provider = inst.provider || getServiceProvider(inst.service);
  const isKaggle = provider === "kaggle";
  const providerLabel = isKaggle ? "Kaggle" : "Colab";

  // Patch Card
  entry.cardArticle.dataset.state = stateKey;
  entry.cardTitleEl.textContent = titleText;
  entry.cardStageEl.textContent = stageText;
  entry.cardProviderEl.className = isKaggle ? "badge provider-kaggle" : "badge provider-colab";
  entry.cardProviderEl.textContent = providerLabel;
  entry.cardBadgeEl.className = badgeCls;
  entry.cardBadgeEl.textContent = inst.state_label || "";
  entry.metaAccountEl.textContent = `账户: ${inst.account || "-"}`;
  entry.metaDriveEl.textContent = isKaggle
    ? `内核: ${inst.kernel_session_id || inst.stage || "-"}`
    : `Drive: ${inst.drive_account || "无"}`;
  entry.metaTransportEl.textContent = `传输: ${inst.transport || "-"}`;
  entry.metaGpuEl.textContent = isKaggle
    ? `加速: ${inst.accelerator || inst.session_gpu || "cpu"}`
    : `GPU: ${inst.session_gpu || "-"}`;
  entry.metaUpdatedEl.textContent = `活跃: ${relTime.short}`;
  if (relTime.full) entry.metaUpdatedEl.title = relTime.full;
  else entry.metaUpdatedEl.removeAttribute("title");

  // Patch Table Row
  entry.tdStatus.dataset.value = String(statusRank);
  entry.tdStatusBadge.className = badgeCls;
  entry.tdStatusBadge.textContent = inst.state_label || "";
  entry.tdStatusStage.textContent = stageText;

  const nameSortVal = inst.instance_arg ? `${inst.service} #${inst.instance_arg}` : inst.service;
  entry.tdName.dataset.value = nameSortVal;
  entry.tdNameTitle.textContent = titleText;
  entry.tdNameCat.textContent = `[${providerLabel}] ${catLabel}`;

  const acctSortVal = `${inst.account || ""} ${inst.drive_account || inst.kernel_session_id || ""}`.trim();
  entry.tdAccount.dataset.value = acctSortVal;
  entry.tdAcctMain.textContent = inst.account || "-";
  entry.tdAcctDrive.textContent = isKaggle
    ? `内核: ${inst.kernel_session_id || "-"}`
    : `Drive: ${inst.drive_account || "无"}`;

  const gpuDisplay = isKaggle ? (inst.accelerator || inst.session_gpu || "cpu") : (inst.session_gpu || "-");
  const gpuSortVal = `${gpuDisplay} ${inst.transport || ""}`.trim();
  entry.tdGpu.dataset.value = gpuSortVal;
  entry.tdGpuMain.textContent = gpuDisplay;
  entry.tdGpuTrans.textContent = inst.transport || "-";

  entry.tdUpdated.dataset.value = String(updatedTs);
  entry.tdUpdatedText.textContent = relTime.short;
  if (relTime.full) entry.tdUpdatedText.title = relTime.full;
  else entry.tdUpdatedText.removeAttribute("title");

  // Patch Grouped Row
  entry.grpInstLabel.textContent = inst.instance_arg ? `#${inst.instance_arg}` : "默认实例";
  entry.grpBadgeEl.className = badgeCls;
  entry.grpBadgeEl.textContent = inst.state_label || "";
  entry.grpStageEl.textContent = `[${providerLabel}] ${stageText}`;
  entry.grpMetaEl.replaceChildren(
    el("span", { textContent: `账户: ${inst.account || "-"}` }),
    el("span", {
      textContent: isKaggle
        ? `内核: ${inst.kernel_session_id || inst.stage || "-"}`
        : `Drive: ${inst.drive_account || "无"}`,
    }),
    el("span", {
      textContent: isKaggle
        ? `加速: ${inst.accelerator || inst.session_gpu || "cpu"}`
        : `GPU: ${inst.session_gpu || "-"}`,
    }),
    el("span", { textContent: `传输: ${inst.transport || "-"}` }),
    el("span", { textContent: `活跃: ${relTime.short}`, title: relTime.full || undefined }),
  );

  // Patch URL displays only if changed
  const nextUrlSig = `${urlHref || ""}|${inst.hostname || ""}`;
  if (entry.urlSig !== nextUrlSig) {
    entry.urlSig = nextUrlSig;
    if (urlHref) {
      entry.cardUrlBox.className = "";
      entry.cardUrlBox.replaceChildren(
        createUrlCopyRow(`inst-card-url-${entry.idSeq}`, urlHref, "card-url", "card-url-row"),
      );

      entry.tdUrl.replaceChildren(
        createUrlCopyRow(`inst-table-url-${entry.idSeq}`, urlHref, "table-url-link", "table-url-cell"),
      );

      entry.grpUrlBox.className = "";
      entry.grpUrlBox.replaceChildren(
        createUrlCopyRow(`inst-grp-url-${entry.idSeq}`, urlHref, "card-url", "card-url-row"),
      );
    } else {
      const noUrlMsg = inst.hostname ? `预设域名: ${inst.hostname}` : "暂无公网 URL";
      entry.cardUrlBox.className = "muted";
      entry.cardUrlBox.textContent = noUrlMsg;

      entry.tdUrl.replaceChildren(
        el("span", {
          className: "muted",
          textContent: inst.hostname ? `预设: ${inst.hostname}` : "-",
        }),
      );

      entry.grpUrlBox.className = "muted";
      entry.grpUrlBox.textContent = noUrlMsg;
    }
  }

  entry.cardActions.sync(inst);
  entry.rowActions.sync(inst);
  entry.groupActions.sync(inst);

  return entry;
}

function populateInstanceFilterSelects() {
  const acctSel = document.getElementById("instance-account-filter");
  const catSel = document.getElementById("instance-category-filter");
  if (!acctSel || !catSel) return;

  const accts = new Set();
  let hasEmptyAcct = false;
  const catCounts = new Map();
  const catLabels = new Map();

  for (const inst of state.instances) {
    if (inst.account) accts.add(inst.account);
    else hasEmptyAcct = true;
    const cat = getInstanceCategory(inst);
    catCounts.set(cat, (catCounts.get(cat) || 0) + 1);
    if (!catLabels.has(cat)) catLabels.set(cat, getInstanceCategoryLabel(inst));
  }

  const sortedAccts = Array.from(accts).sort((a, b) => SORT_COLLATOR.compare(a, b));
  const acctSig = `${sortedAccts.join(",")}|${hasEmptyAcct}`;
  if (acctSel.dataset.optSig !== acctSig) {
    acctSel.dataset.optSig = acctSig;
    const prevAcct = acctSel.value || "all";
    const acctOpts = [el("option", { value: "all", textContent: "全部账户" })];
    for (const name of sortedAccts) {
      acctOpts.push(el("option", { value: name, textContent: name }));
    }
    if (hasEmptyAcct) {
      acctOpts.push(el("option", { value: "__none__", textContent: "未指定账户" }));
    }
    acctSel.replaceChildren(...acctOpts);
    acctSel.value = Array.from(acctSel.options).some((o) => o.value === prevAcct) ? prevAcct : "all";
  }

  const cats = [
    ...CATEGORY_ORDER.filter((c) => catCounts.has(c)),
    ...Array.from(catCounts.keys()).filter((c) => !CATEGORY_ORDER.includes(c)),
  ];
  const catSig = cats.map((c) => `${c}:${catCounts.get(c)}`).join(",");
  if (catSel.dataset.optSig !== catSig) {
    catSel.dataset.optSig = catSig;
    const prevCat = catSel.value || "all";
    const catOpts = [el("option", { value: "all", textContent: `全部分类 (${state.instances.length})` })];
    for (const c of cats) {
      const label = catLabels.get(c) || CATEGORY_LABELS[c] || c;
      catOpts.push(el("option", { value: c, textContent: `${label} (${catCounts.get(c)})` }));
    }
    catSel.replaceChildren(...catOpts);
    catSel.value = Array.from(catSel.options).some((o) => o.value === prevCat) ? prevCat : "all";
  }
}

function matchesInstanceStatus(inst, statusFilter) {
  if (statusFilter === "all") return true;
  if (statusFilter === "running") return Boolean(inst.running);
  if (statusFilter === "ready") return Boolean(inst.running && inst.ready);
  if (statusFilter === "error") return isInstanceError(inst);
  if (statusFilter === "stopped") return Boolean(!inst.running && !isInstanceError(inst));
  return true;
}

function sortInstancesList(items) {
  const key = state.instanceSortKey;
  const dirMult = state.instanceSortDir === "descending" ? -1 : 1;
  const entries = items.map((inst) => ({ inst, node: getOrUpdateInstanceNodes(inst) }));

  entries.sort((a, b) => {
    let cmp = 0;
    if (key === "status") {
      const valA = Number(a.node.tdStatus.dataset.value);
      const valB = Number(b.node.tdStatus.dataset.value);
      cmp = valA - valB;
    } else if (key === "updated") {
      const valA = Number(a.node.tdUpdated.dataset.value);
      const valB = Number(b.node.tdUpdated.dataset.value);
      cmp = valA - valB;
    } else if (key === "name") {
      cmp = SORT_COLLATOR.compare(a.node.tdName.dataset.value || "", b.node.tdName.dataset.value || "");
    } else if (key === "account") {
      cmp = SORT_COLLATOR.compare(a.node.tdAccount.dataset.value || "", b.node.tdAccount.dataset.value || "");
    } else if (key === "gpu") {
      cmp = SORT_COLLATOR.compare(a.node.tdGpu.dataset.value || "", b.node.tdGpu.dataset.value || "");
    }
    if (cmp !== 0) return cmp * dirMult;

    // Deterministic tie-breakers
    const rankDiff = getInstanceStatusRank(a.inst) - getInstanceStatusRank(b.inst);
    if (rankDiff !== 0) return rankDiff;
    const updDiff = getInstanceUpdatedTs(b.inst) - getInstanceUpdatedTs(a.inst);
    if (updDiff !== 0) return updDiff;
    return SORT_COLLATOR.compare(a.inst.session_name, b.inst.session_name);
  });

  return entries;
}

function syncTableSortHeaders() {
  document.querySelectorAll("#instances-table thead th[data-sort-key]").forEach((th) => {
    if (th.dataset.sortKey === state.instanceSortKey) {
      th.setAttribute("aria-sort", state.instanceSortDir);
    } else {
      th.removeAttribute("aria-sort");
    }
  });
}

function syncFleetViewControls() {
  document.querySelectorAll("[data-fleet-view]").forEach((btn) => {
    btn.setAttribute("aria-pressed", btn.dataset.fleetView === state.fleetView ? "true" : "false");
  });
  const collapseWrap = document.getElementById("collapse-stopped-wrap");
  if (collapseWrap) {
    collapseWrap.hidden = state.fleetView !== "cards";
  }
}

function syncStatusFilterPills(baseItems) {
  const counts = {
    all: baseItems.length,
    running: 0,
    ready: 0,
    error: 0,
    stopped: 0,
  };
  for (const inst of baseItems) {
    if (inst.running) counts.running += 1;
    if (inst.running && inst.ready) counts.ready += 1;
    if (isInstanceError(inst)) counts.error += 1;
    else if (!inst.running) counts.stopped += 1;
  }

  document.querySelectorAll("[data-status-filter]").forEach((btn) => {
    const f = btn.dataset.statusFilter;
    btn.setAttribute("aria-pressed", f === state.instanceStatusFilter ? "true" : "false");
    const countSpan = btn.querySelector("[data-count-for]");
    if (countSpan && Object.hasOwn(counts, f)) {
      countSpan.textContent = String(counts[f]);
    }
  });
}

function renderGroupedInstances(sortedEntries) {
  const container = document.getElementById("instances-grouped");
  const groupsMap = new Map();
  for (const item of sortedEntries) {
    const svc = item.inst.service;
    if (!groupsMap.has(svc)) groupsMap.set(svc, []);
    groupsMap.get(svc).push(item);
  }

  const desiredDetails = [];
  for (const [svcName, items] of groupsMap.entries()) {
    const runningCount = items.filter((x) => x.inst.running).length;
    const errorCount = items.filter((x) => isInstanceError(x.inst)).length;
    const anyRunning = runningCount > 0;

    let grp = groupNodeCache.get(svcName);
    if (!grp) {
      const titleName = el("strong", { textContent: svcName });
      const summaryText = el("span", { className: "muted" });
      const runningBadge = el("span", { className: "badge ok" });
      const errorBadge = el("span", { className: "badge danger" });
      const totalBadge = el("span", { className: "badge" });
      const listEl = el("div", { className: "grouped-replica-list" });
      const detailsEl = el(
        "details",
        { className: "service-group-card", open: anyRunning },
        el(
          "summary",
          { className: "service-group-summary" },
          el("div", { className: "service-group-title" }, titleName, summaryText),
          el("div", { className: "service-group-badges" }, runningBadge, errorBadge, totalBadge),
        ),
        listEl,
      );
      grp = { detailsEl, summaryText, runningBadge, errorBadge, totalBadge, listEl, wasRunning: anyRunning };
      groupNodeCache.set(svcName, grp);
    }

    if (anyRunning && !grp.wasRunning) {
      grp.detailsEl.open = true;
    }
    grp.wasRunning = anyRunning;

    const svcMeta = state.servicesMap.get(svcName);
    grp.summaryText.textContent = svcMeta?.summary || svcMeta?.category_label || "";
    grp.runningBadge.hidden = runningCount === 0;
    grp.runningBadge.textContent = `${runningCount} 运行中`;
    grp.errorBadge.hidden = errorCount === 0;
    grp.errorBadge.textContent = `${errorCount} 异常`;
    grp.totalBadge.textContent = `共 ${items.length} 个实例`;

    reorderContainer(grp.listEl, items.map((x) => x.node.groupRow));
    desiredDetails.push(grp.detailsEl);
  }

  for (const [svcName, grp] of groupNodeCache.entries()) {
    if (!groupsMap.has(svcName)) {
      grp.detailsEl.remove();
      groupNodeCache.delete(svcName);
    }
  }

  reorderContainer(container, desiredDetails);
}

function renderInstances() {
  const activeSessionNames = new Set(state.instances.map((i) => i.session_name));
  for (const [sess, cached] of instanceNodeCache.entries()) {
    if (!activeSessionNames.has(sess)) {
      cached.cardSlot.remove();
      cached.row.remove();
      cached.groupRow.remove();
      instanceNodeCache.delete(sess);
    }
  }

  const pruneBtn = document.getElementById("prune-replicas-btn");
  const prunableReplicas = getPrunableReplicas();
  if (pruneBtn) {
    pruneBtn.disabled = state.readOnly || prunableReplicas.length === 0;
    pruneBtn.textContent = prunableReplicas.length > 0 ? `清理临时副本 (${prunableReplicas.length})` : "清理临时副本";
  }

  populateInstanceFilterSelects();
  syncFleetViewControls();
  syncTableSortHeaders();

  const q = (document.getElementById("instance-search")?.value || "").trim().toLowerCase();
  const acctFilter = document.getElementById("instance-account-filter")?.value || "all";
  const catFilter = document.getElementById("instance-category-filter")?.value || "all";

  const baseMatched = state.instances.filter((inst) => {
    if (acctFilter !== "all") {
      if (acctFilter === "__none__") {
        if (inst.account) return false;
      } else if (inst.account !== acctFilter) {
        return false;
      }
    }
    if (catFilter !== "all" && getInstanceCategory(inst) !== catFilter) return false;
    if (!q) return true;
    const haystack = [
      inst.service,
      inst.instance_arg ? `#${inst.instance_arg}` : "",
      inst.instance_arg || "",
      inst.session_name,
      inst.url || "",
      inst.hostname || "",
      inst.account || "",
      inst.drive_account || "",
      inst.state_label || "",
      inst.stage_hint || "",
      getInstanceCategoryLabel(inst),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(q);
  });

  syncStatusFilterPills(baseMatched);

  const filtered = baseMatched.filter((inst) => matchesInstanceStatus(inst, state.instanceStatusFilter));
  const countEl = document.getElementById("instances-count");
  if (state.instances.length === 0) {
    countEl.textContent = "0 个实例";
  } else if (filtered.length === state.instances.length) {
    countEl.textContent = `${state.instances.length} 个实例`;
  } else {
    countEl.textContent = `显示 ${filtered.length} / ${state.instances.length} 个实例`;
  }

  const emptyAllEl = document.getElementById("empty-all-instances");
  const emptyFilteredEl = document.getElementById("empty-filtered-instances");
  const grid = document.getElementById("instances-grid");
  const stoppedDrawer = document.getElementById("stopped-instances-drawer");
  const stoppedGrid = document.getElementById("stopped-instances-grid");
  const tableWrap = document.getElementById("instances-table-wrap");
  const tbody = document.getElementById("instances-tbody");
  const groupedEl = document.getElementById("instances-grouped");

  if (state.instances.length === 0) {
    emptyAllEl.hidden = false;
    emptyFilteredEl.hidden = true;
    grid.hidden = true;
    stoppedDrawer.hidden = true;
    tableWrap.hidden = true;
    groupedEl.hidden = true;
    return;
  }

  emptyAllEl.hidden = true;
  if (filtered.length === 0) {
    emptyFilteredEl.hidden = false;
    grid.hidden = true;
    stoppedDrawer.hidden = true;
    tableWrap.hidden = true;
    groupedEl.hidden = true;
    return;
  }

  emptyFilteredEl.hidden = true;
  const sortedEntries = sortInstancesList(filtered);

  if (state.fleetView === "table") {
    grid.hidden = true;
    stoppedDrawer.hidden = true;
    groupedEl.hidden = true;
    tableWrap.hidden = false;
    reorderContainer(tbody, sortedEntries.map((x) => x.node.row));
  } else if (state.fleetView === "grouped") {
    grid.hidden = true;
    stoppedDrawer.hidden = true;
    tableWrap.hidden = true;
    groupedEl.hidden = false;
    renderGroupedInstances(sortedEntries);
  } else {
    tableWrap.hidden = true;
    groupedEl.hidden = true;
    const collapseStopped =
      Boolean(document.getElementById("collapse-stopped-toggle")?.checked) &&
      state.instanceStatusFilter !== "stopped";
    if (collapseStopped) {
      const primaryCards = [];
      const stoppedCards = [];
      for (const item of sortedEntries) {
        if (!item.inst.running && !isInstanceError(item.inst)) {
          stoppedCards.push(item.node.cardSlot);
        } else {
          primaryCards.push(item.node.cardSlot);
        }
      }
      if (primaryCards.length > 0) {
        grid.hidden = false;
        reorderContainer(grid, primaryCards);
      } else {
        grid.hidden = true;
        grid.replaceChildren();
      }
      if (stoppedCards.length > 0) {
        stoppedDrawer.hidden = false;
        document.getElementById("stopped-instances-summary").textContent = `已折叠 ${stoppedCards.length} 个已停止实例`;
        reorderContainer(stoppedGrid, stoppedCards);
      } else {
        stoppedDrawer.hidden = true;
        stoppedGrid.replaceChildren();
      }
    } else {
      stoppedDrawer.hidden = true;
      stoppedGrid.replaceChildren();
      grid.hidden = false;
      reorderContainer(grid, sortedEntries.map((x) => x.node.cardSlot));
    }
  }
}

function populateCatalogCategoryFilter() {
  const select = document.getElementById("category-filter");
  if (!select) return;
  const counts = new Map();
  const labels = new Map();
  for (const svc of state.services) {
    const cat = svc.category || "other";
    counts.set(cat, (counts.get(cat) || 0) + 1);
    if (!labels.has(cat)) labels.set(cat, svc.category_label || CATEGORY_LABELS[cat] || cat);
  }
  const orderedCats = [
    ...CATEGORY_ORDER.filter((c) => counts.has(c)),
    ...Array.from(counts.keys()).filter((c) => !CATEGORY_ORDER.includes(c)),
  ];
  const sig = orderedCats.map((c) => `${c}:${counts.get(c)}`).join(",");
  if (select.dataset.catSig === sig) return;
  select.dataset.catSig = sig;

  const prev = select.value || "all";
  const opts = [el("option", { value: "all", textContent: `全部分类 (${state.services.length})` })];
  for (const cat of orderedCats) {
    opts.push(el("option", { value: cat, textContent: `${labels.get(cat)} (${counts.get(cat)})` }));
  }
  select.replaceChildren(...opts);
  select.value = Array.from(select.options).some((o) => o.value === prev) ? prev : "all";
}

function populateGroupedServiceSelect(selectEl) {
  if (!selectEl) return;
  const runningServices = new Set(state.instances.filter((i) => i.running).map((i) => i.service));
  const sig = `${state.services.map((s) => s.name).join(",")}|${Array.from(runningServices).sort().join(",")}`;
  if (selectEl.dataset.groupSig === sig) return;
  selectEl.dataset.groupSig = sig;

  const prev = selectEl.value;
  const grouped = new Map();
  const labels = new Map();
  for (const svc of state.services) {
    const cat = svc.category || "other";
    if (!grouped.has(cat)) grouped.set(cat, []);
    grouped.get(cat).push(svc);
    if (!labels.has(cat)) labels.set(cat, svc.category_label || CATEGORY_LABELS[cat] || cat);
  }
  const orderedCats = [
    ...CATEGORY_ORDER.filter((c) => grouped.has(c)),
    ...Array.from(grouped.keys()).filter((c) => !CATEGORY_ORDER.includes(c)),
  ];

  const groups = orderedCats.map((cat) => {
    const opts = grouped.get(cat).map((svc) => {
      const runningMark = runningServices.has(svc.name) ? " (● 运行中)" : "";
      const summary = svc.summary || svc.category_label || "";
      return el("option", {
        value: svc.name,
        textContent: `${svc.name}${runningMark} — ${summary}`,
      });
    });
    return el("optgroup", { label: labels.get(cat) || cat }, ...opts);
  });

  selectEl.replaceChildren(...groups);
  if (prev && Array.from(selectEl.options).some((o) => o.value === prev)) {
    selectEl.value = prev;
  }
}

function renderServices() {
  const q = (document.getElementById("service-search").value || "").trim().toLowerCase();
  const cat = document.getElementById("category-filter").value;
  const grid = document.getElementById("services-grid");

  const runningCountBySvc = new Map();
  for (const inst of state.instances) {
    if (inst.running) {
      runningCountBySvc.set(inst.service, (runningCountBySvc.get(inst.service) || 0) + 1);
    }
  }

  const filtered = state.services.filter((svc) => {
    const svcCat = svc.category || "other";
    if (cat !== "all" && svcCat !== cat) return false;
    if (!q) return true;
    return `${svc.name} ${svc.summary || ""} ${svc.category_label || ""}`.toLowerCase().includes(q);
  });

  if (filtered.length === 0) {
    grid.replaceChildren(
      el(
        "div",
        { className: "card empty-state-card" },
        el("p", { className: "muted", textContent: "没有匹配当前筛选条件的运行时服务。" }),
      ),
    );
    return;
  }

  const cards = filtered.map((svc) => {
    const runningNum = runningCountBySvc.get(svc.name) || 0;
    const provider = svc.provider || getServiceProvider(svc.name);
    const isKaggle = provider === "kaggle";
    const badges = [createProviderBadge(provider)];
    if (runningNum > 0) {
      badges.push(el("span", { className: "badge ok", textContent: `● ${runningNum} 运行中` }));
    }
    if (svc.openai_compatible) {
      badges.push(el("span", { className: "badge ok", textContent: "OpenAI /v1" }));
    }
    badges.push(
      el("span", {
        className: "badge",
        textContent: svc.category_label || CATEGORY_LABELS[svc.category || "other"] || svc.category || "other",
      }),
    );

    const card = el(
      "article",
      { className: "card service-card" },
      el(
        "div",
        { className: "card-head" },
        el(
          "div",
          { className: "card-title-group" },
          el("strong", { textContent: svc.name }),
          el("span", { className: "muted", textContent: svc.summary || svc.category_label }),
        ),
        el("div", { className: "card-badges" }, ...badges),
      ),
      el(
        "div",
        { className: "card-meta" },
        el("span", { textContent: isKaggle ? `加速: ${svc.accelerator || "cpu"}` : `GPU: ${svc.gpu || "CPU"}` }),
        el("span", { textContent: `端口: ${svc.service_port} -> ${svc.public_port}` }),
        el("span", { textContent: `鉴权: ${svc.auth}` }),
      ),
      el(
        "div",
        { className: "card-actions" },
        el("button", {
          type: "button",
          className: "btn sm primary",
          disabled: state.readOnly,
          textContent: "启动实例",
          onClick: () => openUpDialog(svc.name),
        }),
        el("button", {
          type: "button",
          className: "btn sm secondary",
          textContent: "AI 演练场",
          onClick: () => copilotController?.openServiceInCopilot(svc.name),
        }),
        el("button", {
          type: "button",
          className: "btn sm secondary",
          textContent: "工作台命令",
          onClick: () => {
            switchView("workbench");
            activateTab("panel-cli");
            selectWorkbenchService(svc.name);
          },
        }),
      ),
    );
    return el("div", { className: "card-slot" }, card);
  });

  grid.replaceChildren(...cards);
}

function renderTasksList() {
  const box = document.getElementById("tasks-list");
  if (!state.tasks.length) {
    box.replaceChildren(el("p", { className: "muted", textContent: "当前会话尚未执行后台命令。" }));
    return;
  }
  const rows = state.tasks.map((t) => {
    const tone = t.status === "succeeded" ? "ok" : t.status === "running" ? "warn" : "danger";
    return el(
      "div",
      {
        className: "task-row",
        tabindex: "0",
        role: "button",
        onClick: () => {
          switchView("workbench");
          activateTab("panel-cli");
          attachTaskStream(t.task_id, t);
        },
      },
      el(
        "div",
        null,
        el("strong", { textContent: `${t.service} ${t.subcommand}` }),
        el("div", null, el("code", { textContent: t.command_preview })),
      ),
      el("span", { className: `badge ${tone}`, textContent: `${t.status}${t.returncode !== null && t.returncode !== undefined ? ` (${t.returncode})` : ""}` }),
    );
  });
  box.replaceChildren(...rows);
}

function formatAccountQuotaCell(quota, isLoading) {
  if (quota && quota.usage) {
    const bal = Number(quota.usage.balance || 0);
    const estHours = quota.usage.estimated_hours;
    const badgeCls = bal > 0 ? (bal >= 10 ? "badge ok" : "badge warn") : "badge";
    const badgeText = bal > 0 ? `${bal.toFixed(2)} CCU` : "0.00 CCU · 免费额度";
    const children = [el("span", { className: badgeCls, textContent: badgeText })];
    if (estHours) {
      children.push(el("span", { className: "quota-sub", textContent: `约可跑 ${estHours}h` }));
    }
    return el("div", { className: "quota-stack" }, ...children);
  }
  if (quota && quota.error) {
    const shortErr = String(quota.error).slice(0, 72);
    return el(
      "div",
      { className: "quota-stack" },
      el("span", { className: "badge danger", title: quota.error, textContent: "查询失败" }),
      el("span", { className: "quota-sub", title: quota.error, textContent: shortErr }),
    );
  }
  return el("span", { className: "muted", textContent: isLoading ? "查询中..." : "待刷新" });
}

function formatAccountSessionsCell(quota, isLoading) {
  if (quota && quota.usage) {
    const rate = Number(quota.usage.rate_hourly || 0);
    const cnt = Number(quota.usage.assignments_count || 0);
    const primary = `${rate.toFixed(2)}/h · ${cnt > 0 ? `${cnt} 台活跃` : "空闲"}`;
    const children = [el("span", { textContent: primary })];
    if (Array.isArray(quota.assignments) && quota.assignments.length) {
      const desc = quota.assignments
        .map((a) => (a.name && a.name !== "?" ? `${a.name} (${a.hardware || "CPU"})` : (a.hardware || "CPU")))
        .join("、");
      children.push(el("span", { className: "quota-sub", textContent: desc }));
    }
    return el("div", { className: "quota-stack" }, ...children);
  }
  if (quota && quota.error) {
    return el("span", { className: "muted", textContent: "-" });
  }
  return el("span", { className: "muted", textContent: isLoading ? "查询中..." : "-" });
}

function formatKaggleQuotaCell(quota, available, isLoading) {
  if (quota && quota.remaining_hours !== null && quota.remaining_hours !== undefined) {
    const rem = Number(quota.remaining_hours);
    const total = quota.total_hours !== null && quota.total_hours !== undefined ? Number(quota.total_hours) : null;
    const used = quota.used_hours !== null && quota.used_hours !== undefined ? Number(quota.used_hours) : null;
    const cls = rem > 2 ? "badge ok" : rem > 0.15 ? "badge warn" : "badge danger";
    const text = total !== null ? `${rem.toFixed(1)} / ${total.toFixed(1)}h` : `剩余 ${rem.toFixed(1)}h`;
    const children = [el("span", { className: cls, textContent: text })];
    if (used !== null) {
      children.push(el("span", { className: "quota-sub", textContent: `已用 ${used.toFixed(1)}h` }));
    }
    return el("div", { className: "quota-stack" }, ...children);
  }
  if (!available) {
    return el("span", { className: "muted", textContent: isLoading ? "查询中..." : "待刷新" });
  }
  return el("span", { className: "muted", textContent: "-" });
}

function renderColabAccountsSection(ac, isLoading) {
  const acctSummary = document.getElementById("accounts-summary");
  const us = ac.usage_summary || null;
  let usageTotalText = "待查询（点击刷新可用量）";
  if (state.accountsUsageLoading && !us) {
    usageTotalText = "正在查询实时额度...";
  } else if (us) {
    if (!us.supported) {
      usageTotalText = us.error || "colab CLI 不可用";
    } else if (us.error && !(us.totals && us.totals.queried_accounts > 0)) {
      usageTotalText = `查询失败: ${us.error}`;
    } else {
      const tot = us.totals || {};
      const bal = Number(tot.total_balance || 0).toFixed(2);
      const rate = Number(tot.total_rate_hourly || 0).toFixed(2);
      const activeCnt = Number(tot.total_assignments || 0);
      usageTotalText = `${bal} CCU · ${rate}/h · ${activeCnt} 台活跃 (${us.fetched_time || "-"})`;
    }
  }
  acctSummary.replaceChildren(
    el("span", { className: "kv-key", textContent: "生效计算账户" }),
    el("span", { className: "kv-val", textContent: `${ac.effective_account || "未解析"} (${ac.effective_source || "-"})` }),
    el("span", { className: "kv-key", textContent: "生效共享 Drive" }),
    el("span", { className: "kv-val", textContent: `${ac.effective_drive_account || "无"} (${ac.drive_source || "-"})` }),
    el("span", { className: "kv-key", textContent: "Colab 可用量合计" }),
    el("span", { className: "kv-val", textContent: usageTotalText }),
  );

  const tbody = document.getElementById("accounts-tbody");
  const list = ac.accounts || [];
  if (!list.length) {
    if (ac.legacy_quota) {
      const hasConsent = Boolean(ac.legacy_drive_consent);
      tbody.replaceChildren(
        el(
          "tr",
          null,
          el("th", { scope: "row", textContent: "默认账户 (单账户)" }),
          el("td", { textContent: "-" }),
          el("td", null, formatAccountQuotaCell(ac.legacy_quota, isLoading)),
          el("td", null, formatAccountSessionsCell(ac.legacy_quota, isLoading)),
          el("td", { textContent: "-" }),
          el("td", null, el("span", { className: `badge ${hasConsent ? "ok" : "warn"}`, textContent: hasConsent ? "已授权文件" : "无本地授权" })),
        ),
      );
      return;
    }
    tbody.replaceChildren(
      el("tr", null, el("td", { colSpan: "6", className: "muted", textContent: "未检测到 colab-ext 多账户配置" })),
    );
    return;
  }
  const trs = list.map((a) =>
    el(
      "tr",
      null,
      el("th", { scope: "row", textContent: `${a.name}${a.active ? " (默认)" : ""}` }),
      el("td", { textContent: a.email || "-" }),
      el("td", null, formatAccountQuotaCell(a.quota, isLoading)),
      el("td", null, formatAccountSessionsCell(a.quota, isLoading)),
      el("td", { textContent: a.drive_account || "-" }),
      el("td", null, el("span", { className: `badge ${a.has_drive_consent ? "ok" : "warn"}`, textContent: a.has_drive_consent ? "已授权文件" : "无本地授权" })),
    ),
  );
  tbody.replaceChildren(...trs);
}

function renderKaggleAccountsSection(ac, isLoading) {
  const kSummary = document.getElementById("kaggle-accounts-summary");
  const kTbody = document.getElementById("kaggle-accounts-tbody");
  if (!kSummary || !kTbody) return;

  const kSnap = ac.kaggle || null;
  let kStatusText = "待查询（点击刷新可用量）";
  if (isLoading && !kSnap) {
    kStatusText = "正在查询 Kaggle 配额...";
  } else if (ac.kaggle_error) {
    kStatusText = `配置错误: ${ac.kaggle_error}`;
  } else if (kSnap) {
    kStatusText = kSnap.available
      ? `已连接 (${Object.keys(kSnap.accounts || {}).length} 个账户)`
      : `降级/不可用: ${kSnap.error || "kaggle CLI 未就绪"}`;
  }
  kSummary.replaceChildren(
    el("span", { className: "kv-key", textContent: "生效 Kaggle 账户" }),
    el("span", { className: "kv-val", textContent: `${ac.kaggle_effective_account || "未解析"} (${ac.kaggle_effective_source || "-"})` }),
    el("span", { className: "kv-key", textContent: "配额查询状态" }),
    el("span", { className: "kv-val", textContent: kStatusText }),
  );

  const kList = ac.kaggle_accounts || [];
  if (!kList.length) {
    const emptyMsg = ac.kaggle_error || (kSnap && !kSnap.available && kSnap.error) || "未检测到 kaggle-ext 账户配置";
    kTbody.replaceChildren(
      el("tr", null, el("td", { colSpan: "6", className: "muted", textContent: emptyMsg })),
    );
    return;
  }

  const available = Boolean(kSnap && kSnap.available);
  const kTrs = kList.map((ka) => {
    const u = ka.usage || (kSnap?.accounts || {})[ka.name] || null;
    const sessText =
      u && u.active_sessions_count !== null && u.active_sessions_count !== undefined
        ? `${u.active_sessions_count} 个`
        : available
          ? "未获取"
          : "-";
    let authNode;
    if (u) {
      const ok = Boolean(u.authenticated);
      const cls = ok ? "badge ok" : "badge danger";
      const lbl = `${u.auth_method || "auth"} · ${ok ? "已认证" : u.error || "未认证"}`;
      authNode = el("span", { className: cls, textContent: lbl });
    } else if (kSnap && !available && kSnap.error) {
      authNode = el("span", { className: "badge warn", title: kSnap.error, textContent: "离线索引" });
    } else {
      authNode = el("span", { className: "muted", textContent: isLoading ? "查询中..." : "待刷新" });
    }
    return el(
      "tr",
      null,
      el("th", { scope: "row", textContent: `${ka.name}${ka.active ? " (默认)" : ""}` }),
      el("td", { textContent: ka.username ? `@${ka.username.replace(/^@/, "")}` : "-" }),
      el("td", null, formatKaggleQuotaCell(u?.gpu_quota, available, isLoading)),
      el("td", null, formatKaggleQuotaCell(u?.tpu_quota, available, isLoading)),
      el("td", { textContent: sessText }),
      el("td", null, authNode),
    );
  });
  kTbody.replaceChildren(...kTrs);
}

function renderTunnelAndAccounts() {
  const tunnelSummary = document.getElementById("tunnel-summary");
  const t = state.tunnel || {};
  const frp = t.frp || {};
  const items = [
    ["当前后端 (--transport)", t.transport || "cloudflare"],
    ["项目主域名", t.base_domain || "未设置（Named Tunnel 将回退 Quick Tunnel）"],
    ["环境变量覆盖", t.environment_override || "无"],
    ["FRP 配置", frp.configured ? `${frp.server_addr || ""}:${frp.server_port || ""} (domain=${frp.domain || ""})` : "未配置"],
  ];
  const nodes = [];
  for (const [k, v] of items) {
    nodes.push(el("span", { className: "kv-key", textContent: k }), el("span", { className: "kv-val", textContent: v }));
  }
  tunnelSummary.replaceChildren(...nodes);
  document.getElementById("tunnel-base-domain").value = t.base_domain || "";

  const ac = state.accounts || {};
  const isLoading = Boolean(state.accountsUsageLoading);
  renderColabAccountsSection(ac, isLoading);
  renderKaggleAccountsSection(ac, isLoading);
}

function getProviderAccountOptions(provider) {
  if (provider === "kaggle") {
    return (state.accounts?.kaggle_accounts || []).map((a) => ({
      value: a.name,
      label: a.username ? `${a.name} (@${a.username.replace(/^@/, "")})` : a.name,
    }));
  }
  return (state.accounts?.accounts || []).map((a) => {
    const u = a.quota?.usage;
    if (!u) return { value: a.name, label: a.name };
    const bal = Number(u.balance || 0).toFixed(1);
    const cnt = Number(u.assignments_count || 0);
    return { value: a.name, label: `${a.name} (${bal} CCU · ${cnt > 0 ? `${cnt}活跃` : "空闲"})` };
  });
}

function refreshUpAccountSelector() {
  const svc = document.getElementById("up-service")?.value || "";
  const opts = getProviderAccountOptions(getServiceProvider(svc));
  fillSelectOptions(document.getElementById("up-account"), opts, {
    emptyLabel: "自动解析",
    valueFn: (o) => o.value,
    labelFn: (o) => o.label,
  });
}

function refreshWorkbenchAccountSelector() {
  const svc = document.getElementById("wb-service")?.value || "";
  const opts = getProviderAccountOptions(getServiceProvider(svc));
  fillSelectOptions(document.getElementById("wb-account"), opts, {
    emptyLabel: "默认规则解析",
    valueFn: (o) => o.value,
    labelFn: (o) => o.label,
  });
}

function refreshDoctorAccountSelector() {
  const provider = document.getElementById("doctor-provider")?.value || "colab";
  let opts;
  if (provider === "kaggle") {
    opts = getProviderAccountOptions("kaggle");
  } else if (provider === "all") {
    opts = [
      ...getProviderAccountOptions("colab").map((o) => ({ value: o.value, label: `[Colab] ${o.label}` })),
      ...getProviderAccountOptions("kaggle").map((o) => ({ value: o.value, label: `[Kaggle] ${o.label}` })),
    ];
  } else {
    opts = getProviderAccountOptions("colab");
  }
  fillSelectOptions(document.getElementById("doctor-account"), opts, {
    emptyLabel: "自动解析",
    valueFn: (o) => o.value,
    labelFn: (o) => o.label,
  });
}

function populateAccountSelectors() {
  const accts = state.accounts?.accounts || [];
  const names = accts.map((a) => a.name);
  refreshUpAccountSelector();
  fillSelectOptions(document.getElementById("up-drive-account"), names, { emptyLabel: "自动解析" });
  refreshWorkbenchAccountSelector();
  refreshDoctorAccountSelector();
  fillSelectOptions(document.getElementById("doctor-drive-account"), names, { emptyLabel: "跟随账户绑定" });
}

async function refreshAccountsUsage({ force = false, silent = true } = {}) {
  if (state.accountsUsageLoading) return;
  const btns = [
    document.getElementById("accounts-usage-refresh-btn"),
    document.getElementById("kaggle-accounts-usage-refresh-btn"),
  ].filter(Boolean);
  state.accountsUsageLoading = true;
  for (const btn of btns) {
    btn.disabled = true;
    btn.classList.add("is-busy");
    btn.textContent = "查询中...";
  }
  renderTunnelAndAccounts();
  try {
    const q = force ? "?refresh=1" : "";
    state.accounts = await apiFetch(`/api/accounts/usage${q}`, { timeoutMs: 25000 });
    populateAccountSelectors();
    if (!silent) {
      showToast("已更新 Colab 与 Kaggle 账户配额", "ok", 2200);
    }
  } catch (err) {
    if (!silent) {
      showToast(`查询账户可用量失败: ${err.message}`, "danger");
    }
  } finally {
    state.accountsUsageLoading = false;
    for (const btn of btns) {
      btn.disabled = false;
      btn.classList.remove("is-busy");
      btn.textContent = "刷新可用量";
    }
    renderTunnelAndAccounts();
  }
}

async function loadBootstrap() {
  const data = await apiFetch("/api/bootstrap");
  state.version = data.version || "";
  state.readOnly = Boolean(data.read_only);
  state.defaultAccount = data.default_account || "";
  state.services = data.services || [];
  state.servicesMap = new Map(state.services.map((s) => [s.name, s]));
  state.accounts = data.accounts || null;
  state.tunnel = data.tunnel || null;
  const seedOnly = !state.bootstrapSeeded;
  state.bootstrapSeeded = true;
  reconcileTasksAndInstances(data.instances || [], data.tasks || [], { seedOnly });

  document.getElementById("app-version").textContent = `v${state.version}`;
  document.getElementById("readonly-badge").hidden = !state.readOnly;
  document.querySelectorAll("[data-open-up]").forEach((btn) => {
    btn.disabled = state.readOnly;
  });

  populateGroupedServiceSelect(document.getElementById("up-service"));
  populateGroupedServiceSelect(document.getElementById("wb-service"));
  populateCatalogCategoryFilter();
  populateAccountSelectors();
  renderKPIs();
  renderInstances();
  renderServices();
  copilotController?.syncCopilotState();
  renderTasksList();
  renderTunnelAndAccounts();
  if (!state.accounts?.usage_summary) {
    void refreshAccountsUsage({ force: false, silent: true });
  }

  if (state.services.length && !document.getElementById("wb-subcommand").options.length) {
    const initialWbSvc = document.getElementById("wb-service").value || state.services[0].name;
    await selectWorkbenchService(initialWbSvc);
  }
}

/* SSE live updates + cross-tab multiplexing */
const eventsChannel = typeof BroadcastChannel === "function" ? new BroadcastChannel("colabllm-web-events") : null;

function applyEventsPayload(payload, { broadcast = false } = {}) {
  if (!payload || typeof payload !== "object") return;
  reconcileTasksAndInstances(payload.instances || [], payload.tasks || []);
  populateGroupedServiceSelect(document.getElementById("up-service"));
  populateGroupedServiceSelect(document.getElementById("wb-service"));
  renderKPIs();
  renderInstances();
  renderServices();
  copilotController?.syncCopilotState();
  renderTasksList();
  if (broadcast && eventsChannel) {
    try {
      eventsChannel.postMessage({ type: "state", payload });
    } catch {
      // Ignore closed channel errors.
    }
  }
}

function disconnectEventsSse({ paused = false } = {}) {
  if (state.eventsSource) {
    state.eventsSource.close();
    state.eventsSource = null;
  }
  const dot = document.getElementById("sse-dot");
  const label = document.getElementById("sse-label");
  if (dot && label && paused) {
    dot.dataset.state = "open";
    label.textContent = "后台标签页同步中";
  }
}

if (eventsChannel) {
  eventsChannel.addEventListener("message", (evt) => {
    const msg = evt.data;
    if (msg && msg.type === "state" && msg.payload) {
      applyEventsPayload(msg.payload, { broadcast: false });
    }
  });
}

function connectEventsSse() {
  if (document.visibilityState === "hidden") {
    disconnectEventsSse({ paused: true });
    return;
  }
  if (state.eventsSource) {
    state.eventsSource.close();
    state.eventsSource = null;
  }
  const dot = document.getElementById("sse-dot");
  const label = document.getElementById("sse-label");
  dot.dataset.state = "connecting";
  label.textContent = "正在连接事件流";

  const src = new EventSource("/api/events");
  state.eventsSource = src;
  src.addEventListener("open", () => {
    dot.dataset.state = "open";
    label.textContent = "实时状态流已连接";
  });
  src.addEventListener("state", (evt) => {
    try {
      const payload = JSON.parse(evt.data);
      applyEventsPayload(payload, { broadcast: true });
    } catch {
      // Ignore malformed frame.
    }
  });
  src.addEventListener("error", () => {
    if (document.visibilityState === "hidden") {
      disconnectEventsSse({ paused: true });
      return;
    }
    dot.dataset.state = "error";
    label.textContent = "事件流重连中...";
  });
}

/* Launch / Dry-run Dialog */
let upExtraControllers = [];

function getUpPayload(dryRun) {
  const extra = {};
  for (const ctrl of upExtraControllers) {
    const val = ctrl.readValue();
    if (val !== null && val !== false) {
      extra[ctrl.field.id] = val;
    }
  }
  const svcName = document.getElementById("up-service").value;
  const isKaggle = getServiceProvider(svcName) === "kaggle";
  const mode = document.getElementById("up-tunnel-mode").value;
  const domainVal = document.getElementById("up-domain-input").value.trim();
  const basePayload = {
    instance: document.getElementById("up-instance").value.trim(),
    account: document.getElementById("up-account").value.trim(),
    tunnel_mode: mode,
    domain: mode === "domain" ? domainVal : "",
    base_domain: mode === "base_domain" ? domainVal : "",
    transport: document.getElementById("up-transport").value.trim(),
    auto_restart: document.getElementById("up-auto-restart").checked,
    new_credentials: document.getElementById("up-new-credentials").checked,
    restart_delay: document.getElementById("up-restart-delay").value.trim(),
    idle_check: document.getElementById("up-idle-check").value.trim(),
    max_restarts: document.getElementById("up-max-restarts").value.trim(),
    ready_timeout: document.getElementById("up-ready-timeout").value.trim(),
    extra,
    dry_run: Boolean(dryRun),
  };
  if (isKaggle) {
    return {
      ...basePayload,
      accelerator: document.getElementById("up-accelerator").value.trim(),
      internet: document.getElementById("up-internet").checked,
      max_uptime: document.getElementById("up-max-uptime").value.trim(),
      startup_timeout: document.getElementById("up-startup-timeout").value.trim(),
    };
  }
  return {
    ...basePayload,
    drive_account: document.getElementById("up-drive-account").value.trim(),
    gpu: document.getElementById("up-gpu").value.trim(),
    skip: document.getElementById("up-skip").value.trim(),
    hf_token: document.getElementById("up-hf-token").value.trim(),
    no_drive: document.getElementById("up-no-drive").checked,
  };
}

function syncUpDialogProviderMode(serviceName) {
  const svcObj = state.servicesMap.get(serviceName);
  const isKaggle = (svcObj?.provider || getServiceProvider(serviceName)) === "kaggle";
  const toggleHidden = (id, hidden) => {
    const node = document.getElementById(id);
    if (node) node.hidden = hidden;
  };
  toggleHidden("up-drive-account-wrap", isKaggle);
  toggleHidden("up-gpu-wrap", isKaggle);
  toggleHidden("up-colab-extra-row", isKaggle);
  toggleHidden("up-no-drive-wrap", isKaggle);
  toggleHidden("up-accelerator-wrap", !isKaggle);
  toggleHidden("up-internet-wrap", !isKaggle);
  toggleHidden("up-max-uptime-wrap", !isKaggle);
  toggleHidden("up-startup-timeout-wrap", !isKaggle);

  const maxRestartsLbl = document.getElementById("up-max-restarts-label");
  const maxRestartsInput = document.getElementById("up-max-restarts");
  if (maxRestartsLbl && maxRestartsInput) {
    if (isKaggle) {
      maxRestartsLbl.textContent = "--max-restarts (默认 3，0 禁用重启)";
      maxRestartsInput.placeholder = "3";
    } else {
      maxRestartsLbl.textContent = "--max-restarts (0 不限)";
      maxRestartsInput.placeholder = "0";
    }
  }
  if (isKaggle) {
    const netEl = document.getElementById("up-internet");
    if (netEl && netEl.dataset.forService !== serviceName) {
      netEl.checked = svcObj?.enable_internet !== false;
      netEl.dataset.forService = serviceName;
    }
  }
  refreshUpAccountSelector();
}

function updateUpCommandPreview() {
  const svc = document.getElementById("up-service").value;
  if (!svc) return;
  const svcObj = state.servicesMap.get(svc);
  const isKaggle = (svcObj?.provider || getServiceProvider(svc)) === "kaggle";
  const p = getUpPayload(false);
  const args = isKaggle
    ? ["colabllm", "kaggle", svcObj?.short_name || svc.replace(/^kaggle-/, "")]
    : ["colabllm", svc];
  if (p.instance) args.push("-i", p.instance);
  if (p.account) args.push("--account", p.account);
  if (!isKaggle && p.drive_account) args.push("--drive-account", p.drive_account);
  args.push("up");
  if (p.tunnel_mode === "quick") args.push("--quick-tunnel");
  else if (p.tunnel_mode === "domain") args.push(p.domain ? `--domain=${p.domain}` : "--domain");
  else if (p.tunnel_mode === "base_domain" && p.base_domain) args.push("--base-domain", p.base_domain);
  if (isKaggle) {
    if (p.accelerator) args.push("--accelerator", p.accelerator);
    args.push(p.internet ? "--internet" : "--no-internet");
  } else {
    if (p.gpu) args.push("--gpu", p.gpu);
  }
  if (p.transport) args.push("--transport", p.transport);
  if (!isKaggle) {
    if (p.skip) args.push("--skip", p.skip);
    if (p.no_drive) args.push("--no-drive");
    if (p.hf_token) args.push("--hf-token", "***");
  }
  if (p.new_credentials) args.push("--new-credentials");
  if (p.auto_restart) args.push("--auto-restart");
  if (p.restart_delay) args.push("--restart-delay", p.restart_delay);
  if (p.idle_check) args.push("--idle-check", p.idle_check);
  if (p.max_restarts !== "" && (isKaggle ? Number(p.max_restarts) >= 0 : Number(p.max_restarts) > 0)) {
    args.push("--max-restarts", p.max_restarts);
  }
  if (isKaggle) {
    if (p.max_uptime) args.push("--max-uptime", p.max_uptime);
    if (p.startup_timeout) args.push("--startup-timeout", p.startup_timeout);
  }
  if (p.ready_timeout) args.push("--ready-timeout", p.ready_timeout);
  for (const ctrl of upExtraControllers) {
    const val = ctrl.readValue();
    if (val === null || val === false) continue;
    const flag = ctrl.field.primary_flag;
    if (!flag) continue;
    if (val === true) args.push(flag);
    else if (Array.isArray(val)) val.forEach((v) => args.push(flag, String(v)));
    else if (typeof val === "object" && val.enabled) args.push(val.value ? `${flag}=${val.value}` : flag);
    else args.push(flag, ctrl.field.secret ? "***" : String(val));
  }
  args.push("--detach");
  document.getElementById("up-cmd-preview").textContent = args.map(escapeShellArg).join(" ");
}

async function renderUpExtraFields(serviceName) {
  syncUpDialogProviderMode(serviceName);
  const container = document.getElementById("up-extra-fields");
  container.replaceChildren(el("p", { className: "muted", textContent: "加载参数中..." }));
  upExtraControllers = [];
  try {
    const schema = await ensureSchema(serviceName);
    const extras = (schema.up_options || []).filter((f) => !(f.flags || []).some((fl) => COMMON_UP_FLAGS.has(fl)));
    if (extras.length === 0) {
      container.replaceChildren(el("p", { className: "muted", textContent: "该服务无额外专属启动参数。" }));
    } else {
      const nodes = [];
      for (const field of extras) {
        const ctrl = buildSchemaControl(field, "up-extra");
        upExtraControllers.push({ field, ...ctrl });
        nodes.push(ctrl.element);
      }
      container.replaceChildren(...nodes);
    }
    updateUpCommandPreview();
  } catch (err) {
    container.replaceChildren(el("p", { className: "muted", textContent: `参数加载失败: ${err.message}` }));
  }
}

async function openUpDialog(serviceName = "", instanceArg = "", accountName = "") {
  if (state.readOnly) {
    showToast("当前处于只读模式 (--read-only)，禁止启动实例", "warn");
    return;
  }
  const dlg = document.getElementById("up-dialog");
  const svcSelect = document.getElementById("up-service");
  if (serviceName) svcSelect.value = serviceName;
  syncUpDialogProviderMode(svcSelect.value);
  document.getElementById("up-instance").value = instanceArg || "";
  document.getElementById("up-account").value = accountName || "";
  document.getElementById("up-dryrun-output").hidden = true;
  document.getElementById("up-dryrun-output").textContent = "";
  if (!dlg.open) dlg.showModal();
  await renderUpExtraFields(svcSelect.value);
}

/* Down Confirmation Dialog */
function openDownDialog(inst) {
  if (state.readOnly) {
    showToast("当前处于只读模式 (--read-only)，禁止停止实例", "warn");
    return;
  }
  state.downTarget = inst;
  document.getElementById("down-dialog-desc").textContent = `确定停止实例 ${inst.session_name} 吗？可按需勾选删除命名隧道或停止全部同服务实例。`;
  document.getElementById("down-delete-tunnel").checked = false;
  document.getElementById("down-all").checked = false;
  document.getElementById("down-dialog").showModal();
}

/* Logs Dialog */
function renderLogsFiltered() {
  const viewer = document.getElementById("logs-viewer");
  const filter = (document.getElementById("logs-filter").value || "").trim().toLowerCase();
  const autoScroll = document.getElementById("logs-autoscroll").checked;
  if (!state.logsRawText) {
    state.logsDisplayedText = "";
    viewer.dataset.copyEmpty = "1";
    viewer.textContent = "(日志文件为空或尚未生成)";
  } else if (filter) {
    const lines = state.logsRawText
      .replace(/\r?\n$/, "")
      .split(/\r?\n/)
      .filter((line) => line.toLowerCase().includes(filter));
    if (lines.length) {
      state.logsDisplayedText = lines.join("\n");
      delete viewer.dataset.copyEmpty;
      viewer.textContent = state.logsDisplayedText;
    } else {
      state.logsDisplayedText = "";
      viewer.dataset.copyEmpty = "1";
      viewer.textContent = "(无匹配过滤关键词的日志行)";
    }
  } else {
    state.logsDisplayedText = state.logsRawText.replace(/\r?\n$/, "");
    delete viewer.dataset.copyEmpty;
    viewer.textContent = state.logsRawText;
  }
  if (autoScroll) {
    viewer.scrollTop = viewer.scrollHeight;
  }
}

function attachInstanceRunLogStream(inst, { append = false } = {}) {
  if (state.logsSource) {
    state.logsSource.close();
  }
  const url = `/api/instances/${encodeURIComponent(inst.service)}/logs?instance=${encodeURIComponent(inst.instance_arg || "default")}&lines=250&stream=1`;
  const src = new EventSource(url);
  state.logsSource = src;
  let initialized = false;
  src.addEventListener("init", (evt) => {
    try {
      const data = JSON.parse(evt.data);
      const initContent = data.content || "";
      const normalized = initContent && !initContent.endsWith("\n") ? `${initContent}\n` : initContent;
      if (append && !initialized) {
        if (normalized) {
          state.logsRawText += normalized;
        }
      } else {
        state.logsRawText = normalized;
      }
      initialized = true;
      renderLogsFiltered();
    } catch {
      // Ignore parse error.
    }
  });
  src.addEventListener("chunk", (evt) => {
    try {
      const data = JSON.parse(evt.data);
      state.logsRawText += data.text || "";
      renderLogsFiltered();
    } catch {
      // Ignore parse error.
    }
  });
}

function openLogsDialog(inst) {
  state.logsTarget = inst;
  state.logsRawText = "";
  state.logsDisplayedText = "";
  const viewer = document.getElementById("logs-viewer");
  const titleEl = document.getElementById("logs-dialog-title");
  viewer.dataset.copyEmpty = "1";
  resetCopyButtonState(document.getElementById("logs-copy-btn"));
  document.getElementById("logs-filter").value = "";

  if (state.logsSource) {
    state.logsSource.close();
    state.logsSource = null;
  }

  const dlg = document.getElementById("logs-dialog");
  if (inst.provisioning && inst._pendingTask) {
    titleEl.textContent = `实时日志 · ${inst.session_name}（正在准备环境）`;
    viewer.textContent = "正在连接启动任务输出...";
    dlg.showModal();
    const taskId = inst._pendingTask.task_id;
    const src = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/stream`);
    state.logsSource = src;
    src.addEventListener("open", () => {
      if (state.logsSource === src) {
        state.logsRawText = "";
      }
    });
    src.addEventListener("chunk", (evt) => {
      try {
        const data = JSON.parse(evt.data);
        state.logsRawText += data.text || "";
        renderLogsFiltered();
      } catch {
        // Ignore parse error.
      }
    });
    src.addEventListener("done", (evt) => {
      src.close();
      if (state.logsSource === src) {
        state.logsSource = null;
      }
      try {
        const summary = JSON.parse(evt.data);
        if (summary.status === "succeeded" && dlg.open && state.logsTarget?.session_name === inst.session_name) {
          state.logsRawText += "\n--- 环境准备完成，切换至实例运行日志 ---\n";
          titleEl.textContent = `实时日志 · ${inst.session_name}`;
          renderLogsFiltered();
          attachInstanceRunLogStream(inst, { append: true });
        }
      } catch {
        // Ignore parse error.
      }
    });
    return;
  }

  if (!inst.running && inst._failedTask?.output) {
    titleEl.textContent = `启动失败输出 · ${inst.session_name}`;
    const prefix = inst._failedTask.commandPreview ? `$ ${inst._failedTask.commandPreview}\n\n` : "";
    state.logsRawText = `${prefix}${inst._failedTask.output}`;
    dlg.showModal();
    renderLogsFiltered();
    return;
  }

  titleEl.textContent = `实时日志 · ${inst.session_name}`;
  viewer.textContent = "正在连接日志流...";
  dlg.showModal();
  attachInstanceRunLogStream(inst, { append: false });
}

/* Connection & Secrets Dialog */
function buildConnectMetaRows(inst, sec) {
  const isUnsloth = inst.service === "unsloth-studio";
  if (!sec) {
    const rows = [
      ["状态", inst.state_label],
      ["公网 URL", inst.url || "尚未分配"],
      ["鉴权模式", inst.auth_mode || "-"],
    ];
    if (isUnsloth) {
      rows.push([
        "Web 登录凭据",
        inst.has_app_login ? "已生成（已脱敏，请点击下方按钮获取）" : "等待首次启动完成",
      ]);
    } else {
      if (inst.has_api_token || inst.auth_mode === "builtin") {
        rows.push(["API Token", inst.has_api_token ? "已生成（已脱敏，可按需获取）" : "无"]);
      }
      if (inst.auth_mode === "caddy" || inst.has_basic_auth) {
        rows.push(["Basic Auth", inst.has_basic_auth ? "已生成（已脱敏，可按需获取）" : "无"]);
      }
      if (inst.has_app_login) {
        rows.push(["Web 登录凭据", "已生成（已脱敏，可按需获取）"]);
      }
      if (inst.has_web_ui && inst.has_api_token) {
        rows.push(["Web UI 鉴权", "打开 Web UI 后在页面设置中填入 API Token"]);
      }
    }
    return rows;
  }

  const rows = [
    ["公网 URL", sec.url || "无"],
    ["鉴权模式", sec.auth_mode || "-"],
  ];
  if (isUnsloth) {
    rows.push(["Web 登录用户名", sec.login_user || "尚未生成"]);
    rows.push(["Web 登录密码", sec.login_pass || "尚未生成"]);
    return rows;
  }
  if (sec.api_token || inst.has_api_token) {
    rows.push(["API Token", sec.api_token || "无"]);
  }
  if (sec.auth_user || sec.auth_pass || sec.auth_mode === "caddy" || inst.has_basic_auth) {
    rows.push(["Basic Auth 用户", sec.auth_user || "无"]);
    rows.push(["Basic Auth 密码", sec.auth_pass || "无"]);
  }
  if (sec.login_user || sec.login_pass || inst.has_app_login) {
    rows.push(["Web 登录用户名", sec.login_user || "无"]);
    rows.push(["Web 登录密码", sec.login_pass || "无"]);
  }
  if (inst.has_web_ui && (inst.has_api_token || sec.api_token)) {
    rows.push(["Web UI 鉴权", "打开 Web UI 后在页面设置中填入 API Token"]);
  }
  return rows;
}

function renderConnectMeta(inst, sec) {
  const nodes = [];
  for (const [k, v] of buildConnectMetaRows(inst, sec)) {
    nodes.push(el("span", { className: "kv-key", textContent: k }), el("span", { className: "kv-val", textContent: v }));
  }
  document.getElementById("connect-meta").replaceChildren(...nodes);
}

function renderConnectSnippets() {
  const inst = state.connectTarget;
  if (!inst) return;
  const sec = state.connectSecrets;
  const url = sec?.url || inst.url || "https://example.trycloudflare.com";
  const isAppLogin = inst.service === "unsloth-studio" || (inst.has_app_login && !inst.has_api_token && !inst.has_basic_auth);
  document.getElementById("connect-copy-py").hidden = isAppLogin;

  if (isAppLogin) {
    const loginUser = sec?.login_user || "unsloth";
    const loginPass = sec?.login_pass || "<LOGIN_PASS>";
    const statusCmd = `curl -sS ${escapeShellArg(`${url}/api/auth/status`)}`;
    const loginCmd = [
      `curl -sS -X POST ${escapeShellArg(`${url}/api/auth/login`)}`,
      `  -H 'Content-Type: application/json'`,
      `  -d ${escapeShellArg(JSON.stringify({ username: loginUser, password: loginPass }))}`,
    ].join(" \\\n");
    document.getElementById("connect-snippet").textContent = `# curl (检查登录状态与获取 Bearer JWT)\n${statusCmd}\n${loginCmd}`;
    return;
  }

  const token = sec?.api_token || (inst.has_api_token ? "<YOUR_API_TOKEN>" : "");
  const basicUser = sec?.auth_user || (inst.has_basic_auth ? "<AUTH_USER>" : "");
  const basicPass = sec?.auth_pass || (inst.has_basic_auth ? "<AUTH_PASS>" : "");

  let curl = `curl -sS ${escapeShellArg(`${url}/v1/models`)}`;
  if (token) curl += ` \\\n  -H ${escapeShellArg(`Authorization: Bearer ${token}`)}`;
  else if (basicUser) curl += ` \\\n  -u ${escapeShellArg(`${basicUser}:${basicPass}`)}`;

  const pySnippet = [
    "from openai import OpenAI",
    `client = OpenAI(base_url=${JSON.stringify(`${url}/v1`)}, api_key=${JSON.stringify(token || "none")})`,
    'print(client.models.list())',
  ].join("\n");

  document.getElementById("connect-snippet").textContent = `# curl\n${curl}\n\n# Python OpenAI SDK\n${pySnippet}`;
}

async function revealConnectSecrets(inst) {
  const sec = await apiFetch(`/api/instances/${encodeURIComponent(inst.service)}/secret`, {
    method: "POST",
    json: { instance: inst.instance_arg || "" },
  });
  state.connectSecrets = sec;
  renderConnectMeta(inst, sec);
  renderConnectSnippets();
  return sec;
}

function openConnectDialog(inst) {
  state.connectTarget = inst;
  state.connectSecrets = null;
  const dlg = document.getElementById("connect-dialog");
  dlg.querySelectorAll(".copy-btn").forEach(resetCopyButtonState);
  document.getElementById("connect-dialog-title").textContent = `连接信息 · ${inst.session_name}`;
  renderConnectMeta(inst, null);
  document.getElementById("connect-reveal-btn").disabled = state.readOnly;
  const copyTokenBtn = document.getElementById("connect-copy-token");
  copyTokenBtn.hidden = !inst.has_api_token;
  copyTokenBtn.disabled = state.readOnly;
  renderConnectSnippets();
  dlg.showModal();
}

async function runProbeInstance(inst) {
  try {
    const q = new URLSearchParams({ instance: inst.instance_arg || "" });
    if (inst.account) q.set("account", inst.account);
    const res = await apiFetch(`/api/instances/${encodeURIComponent(inst.service)}/probe?${q.toString()}`, { timeoutMs: 40000 });
    const tone = res.healthy || res.ready ? "ok" : "warn";
    showToast(`${inst.session_name} 探活: running=${res.running} ready=${res.ready}${res.http_status ? ` HTTP ${res.http_status}` : ""}`, tone);
  } catch (err) {
    showToast(`探活失败: ${err.message}`, "danger");
  }
}

/* Workbench Schema CLI Form + Tasks */
let wbControllers = [];
let previewTimer = null;

function readWorkbenchValues() {
  const values = {};
  for (const ctrl of wbControllers) {
    const val = ctrl.readValue();
    if (val !== null && val !== false) {
      values[ctrl.field.id] = val;
    }
  }
  return values;
}

async function refreshWorkbenchPreview() {
  const svc = document.getElementById("wb-service").value;
  const sub = document.getElementById("wb-subcommand").value;
  if (!svc || !sub) return;
  try {
    const res = await apiFetch("/api/tasks/preview", {
      method: "POST",
      json: {
        service: svc,
        subcommand: sub,
        instance: document.getElementById("wb-instance").value.trim(),
        account: document.getElementById("wb-account").value.trim(),
        values: readWorkbenchValues(),
      },
    });
    document.getElementById("wb-cmd-preview").textContent = res.command;
  } catch (err) {
    document.getElementById("wb-cmd-preview").textContent = `参数错误: ${err.message}`;
  }
}

function scheduleWorkbenchPreview() {
  window.clearTimeout(previewTimer);
  previewTimer = window.setTimeout(refreshWorkbenchPreview, 90);
}

async function renderWorkbenchFields() {
  const svc = document.getElementById("wb-service").value;
  const sub = document.getElementById("wb-subcommand").value;
  const container = document.getElementById("wb-fields");
  wbControllers = [];
  if (!svc || !sub) {
    container.replaceChildren();
    return;
  }
  const schema = await ensureSchema(svc);
  const subObj = (schema.subcommands || []).find((s) => s.name === sub);
  document.getElementById("wb-subcommand-desc").textContent = subObj?.description || "";
  const fields = subObj?.fields || [];
  if (fields.length === 0) {
    container.replaceChildren(el("p", { className: "muted", textContent: "该子命令无需额外参数，可直接执行。" }));
  } else {
    const nodes = [];
    for (const field of fields) {
      const ctrl = buildSchemaControl(field, "wb-arg");
      wbControllers.push({ field, ...ctrl });
      nodes.push(ctrl.element);
    }
    container.replaceChildren(...nodes);
  }
  document.getElementById("wb-run-btn").disabled = state.readOnly;
  await refreshWorkbenchPreview();
}

async function selectWorkbenchService(serviceName, subcommandName = "") {
  const svcSelect = document.getElementById("wb-service");
  svcSelect.value = serviceName;
  refreshWorkbenchAccountSelector();
  const schema = await ensureSchema(serviceName);
  const subs = schema.subcommands || [];
  const subSelect = document.getElementById("wb-subcommand");
  fillSelectOptions(subSelect, subs, {
    valueFn: (s) => s.name,
    labelFn: (s) => (s.description ? `${s.name} — ${s.description}` : s.name),
  });
  if (subcommandName && subs.some((s) => s.name === subcommandName)) {
    subSelect.value = subcommandName;
  } else {
    const preferred = subs.find((s) => s.name !== "up" && s.name !== "down") || subs[0];
    if (preferred) subSelect.value = preferred.name;
  }
  await renderWorkbenchFields();
}

function attachTaskStream(taskId, initialSummary = null) {
  if (state.activeTaskSource) {
    state.activeTaskSource.close();
  }
  state.activeTaskId = taskId;
  const outEl = document.getElementById("wb-output");
  const statusEl = document.getElementById("wb-task-status");
  const cancelBtn = document.getElementById("wb-cancel-btn");
  outEl.textContent = initialSummary?.command_preview ? `$ ${initialSummary.command_preview}\n\n` : "";
  statusEl.className = "badge warn";
  statusEl.textContent = "运行中";
  cancelBtn.hidden = state.readOnly;

  const src = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/stream`);
  state.activeTaskSource = src;
  src.addEventListener("chunk", (evt) => {
    try {
      const data = JSON.parse(evt.data);
      outEl.textContent += data.text || "";
      outEl.scrollTop = outEl.scrollHeight;
    } catch {
      // Ignore parse failure.
    }
  });
  src.addEventListener("done", (evt) => {
    try {
      const summary = JSON.parse(evt.data);
      const ok = summary.status === "succeeded";
      statusEl.className = `badge ${ok ? "ok" : "danger"}`;
      statusEl.textContent = `${summary.status} (exit=${summary.returncode})`;
      cancelBtn.hidden = true;
      src.close();
      state.activeTaskSource = null;
      showToast(`任务完成: ${summary.service} ${summary.subcommand} -> ${summary.status}`, ok ? "ok" : "danger");
    } catch {
      src.close();
    }
  });
  src.addEventListener("error", () => {
    cancelBtn.hidden = true;
  });
}

/* Wire all event handlers */
function initInteractions() {
  document.getElementById("refresh-btn").addEventListener("click", async () => {
    try {
      await loadBootstrap();
      void refreshAccountsUsage({ force: true, silent: true });
      showToast("状态已刷新", "ok", 2200);
    } catch (err) {
      showToast(`刷新失败: ${err.message}`, "danger");
    }
  });

  // KPI quick-filter cards
  document.getElementById("kpi-card-running").addEventListener("click", () => {
    switchView("fleet");
    activateTab("fleet-panel-instances");
    state.instanceStatusFilter = "running";
    syncKpiStates();
    renderInstances();
  });
  document.getElementById("kpi-card-ready").addEventListener("click", () => {
    switchView("fleet");
    activateTab("fleet-panel-instances");
    state.instanceStatusFilter = "ready";
    syncKpiStates();
    renderInstances();
  });
  document.getElementById("kpi-card-services").addEventListener("click", () => {
    switchView("fleet");
    activateTab("fleet-panel-catalog");
    document.getElementById("service-search")?.focus();
  });

  // Instance filter / search / sort / view switcher / prune toolbar
  const announceReorderStatus = (colLabel) => {
    const statusEl = document.getElementById("reorder-status");
    if (!statusEl) return;
    const dirLabel = state.instanceSortDir === "descending" ? "降序" : "升序";
    statusEl.textContent = `已按${colLabel}${dirLabel}排列实例列表`;
  };

  document.getElementById("instance-search").addEventListener("input", renderInstances);
  document.getElementById("instance-account-filter").addEventListener("change", renderInstances);
  document.getElementById("instance-category-filter").addEventListener("change", renderInstances);
  document.getElementById("instance-sort").addEventListener("change", (e) => {
    const mode = e.target.value;
    if (mode === "updated") {
      state.instanceSortKey = "updated";
      state.instanceSortDir = "descending";
      announceReorderStatus("最近活跃");
    } else if (mode === "name") {
      state.instanceSortKey = "name";
      state.instanceSortDir = "ascending";
      announceReorderStatus("服务 / 实例");
    } else {
      state.instanceSortKey = "status";
      state.instanceSortDir = "ascending";
      announceReorderStatus("状态");
    }
    renderInstances();
  });

  document.querySelectorAll("[data-status-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.instanceStatusFilter = btn.dataset.statusFilter || "all";
      syncKpiStates();
      renderInstances();
    });
  });

  document.querySelectorAll("[data-fleet-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const nextView = btn.dataset.fleetView;
      if (nextView !== "cards" && nextView !== "table" && nextView !== "grouped") return;
      state.fleetView = nextView;
      try {
        localStorage.setItem(FLEET_VIEW_STORAGE_KEY, nextView);
      } catch {
        // Ignore storage restrictions.
      }
      renderInstances();
    });
  });

  document.getElementById("collapse-stopped-toggle").addEventListener("change", renderInstances);
  document.getElementById("prune-replicas-btn").addEventListener("click", openPruneDialog);
  document.getElementById("reset-instance-filters-btn").addEventListener("click", () => {
    document.getElementById("instance-search").value = "";
    document.getElementById("instance-account-filter").value = "all";
    document.getElementById("instance-category-filter").value = "all";
    state.instanceStatusFilter = "all";
    syncKpiStates();
    renderInstances();
  });

  document.querySelectorAll("#instances-table thead th[data-sort-key] .th-sort-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const th = btn.closest("th[data-sort-key]");
      if (!th) return;
      const key = th.dataset.sortKey;
      if (state.instanceSortKey === key) {
        state.instanceSortDir = state.instanceSortDir === "ascending" ? "descending" : "ascending";
      } else {
        state.instanceSortKey = key;
        state.instanceSortDir = key === "updated" ? "descending" : "ascending";
      }
      const sortSel = document.getElementById("instance-sort");
      if (sortSel && (key === "status" || key === "updated" || key === "name")) {
        sortSel.value = key;
      }
      renderInstances();
      announceReorderStatus(btn.textContent.trim());
    });
  });

  document.addEventListener("keydown", (evt) => {
    if (evt.key !== "/" || evt.ctrlKey || evt.metaKey || evt.altKey) return;
    if (document.querySelector("dialog[open]")) return;
    const target = evt.target;
    if (target instanceof Element && (target.closest("input, textarea, select") || target.isContentEditable)) {
      return;
    }
    evt.preventDefault();
    if (state.activeView !== "fleet") switchView("fleet");
    if (state.activeFleetPanel !== "fleet-panel-instances") activateTab("fleet-panel-instances");
    const searchEl = document.getElementById("instance-search");
    searchEl?.focus();
    searchEl?.select?.();
  });

  document.getElementById("service-search").addEventListener("input", renderServices);
  document.getElementById("category-filter").addEventListener("change", renderServices);

  // Clean up modal dialog state and flush deferred toasts on close
  document.querySelectorAll("dialog").forEach((dlg) => {
    dlg.addEventListener("close", () => {
      dlg.querySelectorAll(".copy-btn").forEach(resetCopyButtonState);
      if (dlg.id === "logs-dialog" && state.logsSource) {
        state.logsSource.close();
        state.logsSource = null;
      }
      if (dlg.id !== "down-dialog" && dlg.id !== "remove-dialog" && dlg.id !== "prune-dialog") {
        dlg.returnValue = "";
      }
      window.setTimeout(flushDeferredToasts, 10);
    });
  });

  document.getElementById("down-dialog").addEventListener("close", async () => {
    const dlg = document.getElementById("down-dialog");
    const confirmed = dlg.returnValue === "confirm";
    dlg.returnValue = "";
    const target = state.downTarget;
    state.downTarget = null;
    if (!confirmed || !target) return;
    try {
      const task = await apiFetch(`/api/instances/${encodeURIComponent(target.service)}/down`, {
        method: "POST",
        json: {
          instance: target.instance_arg,
          account: target.account,
          all: document.getElementById("down-all").checked,
          delete_tunnel: document.getElementById("down-delete-tunnel").checked,
        },
      });
      const nextTasks = [task, ...state.tasks.filter((x) => x.task_id !== task.task_id)];
      reconcileTasksAndInstances(state.rawInstances, nextTasks);
      refreshEffectiveFleet();
      showToast(`正在停止 ${target.session_name}...`, "ok");
    } catch (err) {
      showToast(`停止失败: ${err.message}`, "danger");
    }
  });

  document.getElementById("remove-dialog").addEventListener("close", async () => {
    const dlg = document.getElementById("remove-dialog");
    const confirmed = dlg.returnValue === "confirm";
    dlg.returnValue = "";
    const target = state.removeTarget;
    state.removeTarget = null;
    if (!confirmed || !target) return;
    try {
      const res = await apiFetch(`/api/instances/${encodeURIComponent(target.service)}/remove`, {
        method: "POST",
        json: { instance: target.instance_arg || "" },
      });
      state.recentTaskFailures.delete(target.session_name);
      showToast(`已移除实例记录 (${res.count || 1})`, "ok");
      await loadBootstrap();
    } catch (err) {
      showToast(`移除记录失败: ${err.message}`, "danger");
    }
  });

  document.getElementById("prune-dialog").addEventListener("close", async () => {
    const dlg = document.getElementById("prune-dialog");
    const confirmed = dlg.returnValue === "confirm";
    dlg.returnValue = "";
    if (!confirmed) return;
    try {
      const res = await apiFetch("/api/instances/prune", {
        method: "POST",
        json: { replicas_only: true },
      });
      const count = Number(res.count || 0);
      showToast(count > 0 ? `已清理 ${count} 个已停止临时副本` : "没有可清理的已停止副本", "ok");
      await loadBootstrap();
    } catch (err) {
      showToast(`清理失败: ${err.message}`, "danger");
    }
  });

  // Up dialog events
  document.querySelectorAll("[data-open-up]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const out = document.getElementById("up-dryrun-output");
      if (out) {
        out.hidden = true;
        out.textContent = "";
      }
      const svc = document.getElementById("up-service")?.value;
      if (svc) {
        renderUpExtraFields(svc);
      }
    });
  });
  document.getElementById("up-service").addEventListener("change", (e) => renderUpExtraFields(e.target.value));
  document.getElementById("up-tunnel-mode").addEventListener("change", (e) => {
    const mode = e.target.value;
    const wrap = document.getElementById("up-domain-wrap");
    const lbl = document.getElementById("up-domain-label");
    wrap.hidden = mode !== "domain" && mode !== "base_domain";
    lbl.textContent = mode === "base_domain" ? "单次主域名 (--base-domain)" : "固定域名（留空则使用服务默认）";
    updateUpCommandPreview();
  });
  document.getElementById("up-form").addEventListener("input", updateUpCommandPreview);
  document.getElementById("up-form").addEventListener("change", updateUpCommandPreview);

  document.getElementById("up-dryrun-btn").addEventListener("click", async () => {
    const dryrunBtn = document.getElementById("up-dryrun-btn");
    if (dryrunBtn.disabled) return;
    const svc = document.getElementById("up-service").value;
    const out = document.getElementById("up-dryrun-output");
    out.hidden = false;
    out.textContent = "正在生成计划...";
    dryrunBtn.disabled = true;
    dryrunBtn.classList.add("is-busy");
    dryrunBtn.textContent = "正在生成计划...";
    try {
      const res = await apiFetch(`/api/instances/${encodeURIComponent(svc)}/up`, {
        method: "POST",
        json: getUpPayload(true),
        timeoutMs: 30000,
      });
      document.getElementById("up-cmd-preview").textContent = res.command;
      out.textContent = res.output || "(无输出)";
    } catch (err) {
      out.textContent = `Dry-run 失败: ${err.message}`;
    } finally {
      dryrunBtn.disabled = false;
      dryrunBtn.classList.remove("is-busy");
      dryrunBtn.textContent = "预览执行计划 (--dry-run)";
    }
  });

  document.getElementById("up-form").addEventListener("submit", async (evt) => {
    evt.preventDefault();
    const submitBtn = document.getElementById("up-submit-btn");
    const dryrunBtn = document.getElementById("up-dryrun-btn");
    if (submitBtn.disabled) return;
    const svc = document.getElementById("up-service").value;
    submitBtn.disabled = true;
    dryrunBtn.disabled = true;
    submitBtn.classList.add("is-busy");
    submitBtn.textContent = "正在提交启动...";
    try {
      const task = await apiFetch(`/api/instances/${encodeURIComponent(svc)}/up`, {
        method: "POST",
        json: getUpPayload(false),
      });
      document.getElementById("up-dialog").close();
      if (state.activeView === "fleet" && state.activeFleetPanel === "fleet-panel-catalog") {
        activateTab("fleet-panel-instances");
      }
      const nextTasks = [task, ...state.tasks.filter((x) => x.task_id !== task.task_id)];
      reconcileTasksAndInstances(state.rawInstances, nextTasks);
      refreshEffectiveFleet();
      const parsed = parseLifecycleTask(task);
      showToast(`已开始启动 ${parsed?.displayLabel || svc}，正在准备环境...`, "ok");
      loadBootstrap().catch(() => {});
    } catch (err) {
      const out = document.getElementById("up-dryrun-output");
      out.hidden = false;
      out.textContent = `启动失败: ${err.message}`;
    } finally {
      submitBtn.disabled = false;
      dryrunBtn.disabled = false;
      submitBtn.classList.remove("is-busy");
      submitBtn.textContent = "后台启动 (up --detach)";
    }
  });

  // Logs modal filters & export
  document.getElementById("logs-filter").addEventListener("input", renderLogsFiltered);
  document.getElementById("logs-autoscroll").addEventListener("change", renderLogsFiltered);
  if ("CommandEvent" in window) {
    document.addEventListener(
      "command",
      (evt) => {
        if (evt.command?.startsWith("--copy") && evt.source instanceof HTMLButtonElement) {
          handleCopyCommand(evt.source, evt.target, evt.command);
        }
      },
      true,
    );
  } else {
    document.addEventListener("click", (evt) => {
      const btn = evt.target instanceof Element ? evt.target.closest('button[command^="--copy"]') : null;
      if (!btn || btn.disabled) return;
      const targetId = btn.getAttribute("commandfor");
      const target = targetId ? document.getElementById(targetId) : null;
      if (target) handleCopyCommand(btn, target, btn.getAttribute("command"));
    });
  }
  document.getElementById("logs-download-btn").addEventListener("click", () => {
    const blob = new Blob([state.logsRawText || ""], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${state.logsTarget?.session_name || "colabllm"}.log`;
    a.click();
    window.setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });

  // Connect / secrets modal
  document.getElementById("connect-reveal-btn").addEventListener("click", async () => {
    const inst = state.connectTarget;
    if (!inst) return;
    try {
      await revealConnectSecrets(inst);
    } catch (err) {
      showToast(`获取凭据失败: ${err.message}`, "danger");
    }
  });

  // Workbench CLI
  document.getElementById("wb-service").addEventListener("change", (e) => selectWorkbenchService(e.target.value));
  document.getElementById("wb-subcommand").addEventListener("change", renderWorkbenchFields);
  document.getElementById("wb-instance").addEventListener("input", scheduleWorkbenchPreview);
  document.getElementById("wb-account").addEventListener("change", scheduleWorkbenchPreview);
  document.getElementById("wb-form").addEventListener("input", scheduleWorkbenchPreview);
  document.getElementById("wb-form").addEventListener("change", scheduleWorkbenchPreview);

  document.getElementById("wb-form").addEventListener("submit", async (evt) => {
    evt.preventDefault();
    if (state.readOnly) {
      showToast("只读模式禁止运行写入/业务命令", "warn");
      return;
    }
    try {
      const task = await apiFetch("/api/tasks/run", {
        method: "POST",
        json: {
          service: document.getElementById("wb-service").value,
          subcommand: document.getElementById("wb-subcommand").value,
          instance: document.getElementById("wb-instance").value.trim(),
          account: document.getElementById("wb-account").value.trim(),
          values: readWorkbenchValues(),
        },
      });
      attachTaskStream(task.task_id, task);
      await loadBootstrap();
    } catch (err) {
      showToast(`提交失败: ${err.message}`, "danger");
    }
  });

  document.getElementById("wb-cancel-btn").addEventListener("click", async () => {
    if (!state.activeTaskId) return;
    try {
      await apiFetch(`/api/tasks/${encodeURIComponent(state.activeTaskId)}/cancel`, { method: "POST" });
      showToast("已发送停止信号", "warn");
    } catch (err) {
      showToast(`停止失败: ${err.message}`, "danger");
    }
  });

  // Tunnel & FRP
  document.getElementById("tunnel-form").addEventListener("submit", async (evt) => {
    evt.preventDefault();
    if (state.readOnly) {
      showToast("只读模式禁止修改隧道配置", "warn");
      return;
    }
    try {
      state.tunnel = await apiFetch("/api/tunnel", {
        method: "POST",
        json: { base_domain: document.getElementById("tunnel-base-domain").value.trim() },
      });
      renderTunnelAndAccounts();
      renderKPIs();
      showToast("全局主域名已保存", "ok");
    } catch (err) {
      showToast(`保存失败: ${err.message}`, "danger");
    }
  });

  document.getElementById("tunnel-clear-btn").addEventListener("click", async () => {
    if (state.readOnly) {
      showToast("只读模式禁止修改隧道配置", "warn");
      return;
    }
    try {
      state.tunnel = await apiFetch("/api/tunnel", { method: "POST", json: { clear: true } });
      renderTunnelAndAccounts();
      renderKPIs();
      showToast("已清除项目全局主域名", "ok");
    } catch (err) {
      showToast(`清除失败: ${err.message}`, "danger");
    }
  });

  document.getElementById("frp-probe-btn").addEventListener("click", async () => {
    const out = document.getElementById("frp-status-output");
    out.hidden = false;
    out.textContent = "正在探测 FRP 状态...";
    try {
      const res = await apiFetch("/api/frp-status", { timeoutMs: 40000 });
      out.textContent = JSON.stringify(res, null, 2);
    } catch (err) {
      out.textContent = `FRP 检查失败: ${err.message}`;
    }
  });

  document.getElementById("accounts-usage-refresh-btn")?.addEventListener("click", async () => {
    await refreshAccountsUsage({ force: true, silent: false });
  });
  document.getElementById("kaggle-accounts-usage-refresh-btn")?.addEventListener("click", async () => {
    await refreshAccountsUsage({ force: true, silent: false });
  });

  // Doctor
  const syncDoctorProviderMode = () => {
    const provider = document.getElementById("doctor-provider")?.value || "colab";
    const hideDrive = provider === "kaggle";
    const drvWrap = document.getElementById("doctor-drive-wrap");
    const noDrvWrap = document.getElementById("doctor-no-drive-wrap");
    if (drvWrap) drvWrap.hidden = hideDrive;
    if (noDrvWrap) noDrvWrap.hidden = hideDrive;
    refreshDoctorAccountSelector();
  };
  document.getElementById("doctor-provider")?.addEventListener("change", syncDoctorProviderMode);
  syncDoctorProviderMode();

  document.getElementById("doctor-run-btn").addEventListener("click", async () => {
    const banner = document.getElementById("doctor-banner");
    const tbody = document.getElementById("doctor-tbody");
    const provider = document.getElementById("doctor-provider")?.value || "colab";
    banner.textContent = "正在执行环境体检...";
    try {
      const q = new URLSearchParams({
        provider,
        online: document.getElementById("doctor-online").checked ? "1" : "0",
        no_drive: document.getElementById("doctor-no-drive").checked ? "1" : "0",
        account: document.getElementById("doctor-account").value.trim(),
        drive_account: document.getElementById("doctor-drive-account").value.trim(),
        transport: document.getElementById("doctor-transport").value.trim(),
      });
      const res = await apiFetch(`/api/doctor?${q.toString()}`, { timeoutMs: 70000 });
      banner.textContent = `体检完成：overall=${res.ok ? "PASS" : "FAIL"} | provider=${res.provider || provider} | transport=${res.transport || "-"} | account=${res.account || "-"}`;
      const trs = (res.checks || []).map((c) => {
        const tone = c.status === "ok" ? "ok" : c.status === "warn" ? "warn" : "danger";
        return el(
          "tr",
          null,
          el("td", null, el("span", { className: `badge ${tone}`, textContent: c.status })),
          el("td", { textContent: c.name }),
          el("td", { textContent: c.message }),
          el("td", { textContent: c.required ? "必需" : "可选" }),
        );
      });
      tbody.replaceChildren(...trs);
    } catch (err) {
      banner.textContent = `体检执行出错: ${err.message}`;
    }
  });

  const releaseBackgroundStreams = () => {
    disconnectEventsSse({ paused: document.visibilityState === "hidden" });
    state.logsSource?.close();
    state.logsSource = null;
    state.activeTaskSource?.close();
    state.activeTaskSource = null;
  };

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      disconnectEventsSse({ paused: true });
    } else if (document.visibilityState === "visible") {
      if (!state.eventsSource) {
        connectEventsSse();
        loadBootstrap().catch(() => {});
      }
    }
  });

  window.addEventListener("pageshow", () => {
    if (document.visibilityState !== "hidden" && !state.eventsSource) {
      connectEventsSse();
      loadBootstrap().catch(() => {});
    }
  });
  window.addEventListener("pagehide", releaseBackgroundStreams);
  window.addEventListener("beforeunload", releaseBackgroundStreams);
}

async function main() {
  setupFeatureFallbacks();
  initTheme();
  initNavigation();
  initInteractions();
  copilotController = initCopilotStudio({
    appState: state,
    el(tag, attrs = null, ...children) {
      const node = el(tag, attrs, ...children);
      if (attrs && tag === "textarea" && attrs.value !== undefined) {
        node.value = String(attrs.value);
      }
      if (attrs && tag === "input" && (attrs.type === "checkbox" || attrs.type === "radio") && attrs.checked !== undefined) {
        node.checked = Boolean(attrs.checked);
      }
      return node;
    },
    toast(message, tone = "info") {
      showToast(message, tone === "error" ? "danger" : tone);
    },
    copyText(text, successMsg = "已复制到剪贴板") {
      if (!navigator.clipboard?.writeText) {
        showToast("当前浏览器不支持剪贴板写入", "warn");
        return Promise.resolve();
      }
      return navigator.clipboard.writeText(String(text || "")).then(
        () => showToast(successMsg, "ok", 2200),
        () => showToast("复制失败，请手动复制", "warn"),
      );
    },
    switchView,
  });
  try {
    await loadBootstrap();
    connectEventsSse();
  } catch (err) {
    showToast(`初始化失败: ${err.message}`, "danger");
  }
}

main();
