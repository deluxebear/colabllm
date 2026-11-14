const csrfMeta = document.querySelector('meta[name="colabllm-csrf"]');
const readOnlyMeta = document.querySelector('meta[name="colabllm-readonly"]');
const CSRF_TOKEN = csrfMeta ? csrfMeta.content : "";
const READ_ONLY = readOnlyMeta ? readOnlyMeta.content === "true" : false;

const CATEGORY_LABELS = {
  llm: "LLM 与推理",
  image: "图像生成与编辑",
  audio: "语音与音效",
  video: "视频与原生工作流",
  ocr: "文档 OCR",
  other: "其他服务",
};

const VIEW_META = {
  fleet: ["舰队与实例", "管理 Google Colab 运行时生命周期、实时日志与公网隧道"],
  workbench: ["工作台与命令", "动态解析 CLI 子命令表单、后台任务流与同源推理代理"],
  network: ["隧道与账户", "配置项目全局主域名、核验 FRP 与只读查看多账户 Drive 状态"],
  doctor: ["环境体检", "一键核验本机依赖、隧道后端与 Google Drive 授权状态"],
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
  "--transport",
  "--skip",
  "--hf-token",
  "--no-drive",
  "--new-credentials",
  "--auto-restart",
  "--restart-delay",
  "--idle-check",
  "--max-restarts",
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
  instances: [],
  accounts: null,
  tunnel: null,
  tasks: [],
  activeView: "fleet",
  activeTaskId: null,
  activeTaskSource: null,
  eventsSource: null,
  logsSource: null,
  logsRawText: "",
  logsTarget: null,
  downTarget: null,
  connectTarget: null,
  connectSecrets: null,
  chatHistory: [],
  deferredToasts: [],
  themeMq: window.matchMedia("(prefers-color-scheme: dark)"),
};

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
function repositionToasts() {
  const region = document.getElementById("toast-region");
  if (!region) return;
  const items = Array.from(region.querySelectorAll(".toast-item"));
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

function dismissToast(item) {
  if (!item || !item.isConnected) return;
  try {
    item.hidePopover?.();
  } catch {
    item.classList.remove(":popover-open");
  }
  window.setTimeout(() => {
    item.remove();
    repositionToasts();
  }, 240);
}

function renderToastNow(message, tone = "info", duration = 4500) {
  const region = document.getElementById("toast-region");
  if (!region) return;

  const closeBtn = el("button", {
    type: "button",
    className: "btn icon-btn sm",
    "aria-label": "关闭通知",
    textContent: "✕",
  });
  const item = el(
    "div",
    { className: "toast-item", popover: "manual", "data-tone": tone, role: "status" },
    el("span", { textContent: message }),
    closeBtn,
  );
  region.appendChild(item);

  try {
    if (typeof item.showPopover === "function") item.showPopover();
    else item.classList.add(":popover-open");
  } catch {
    item.classList.add(":popover-open");
  }
  repositionToasts();
  announceScreenReader(message);

  let timer = null;
  const startTimer = () => {
    if (duration <= 0) return;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => dismissToast(item), duration);
  };
  const pauseTimer = () => window.clearTimeout(timer);

  item.addEventListener("pointerenter", pauseTimer);
  item.addEventListener("pointerleave", startTimer);
  item.addEventListener("focusin", pauseTimer);
  item.addEventListener("focusout", startTimer);
  closeBtn.addEventListener("click", () => {
    pauseTimer();
    dismissToast(item);
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
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (method !== "GET" && method !== "HEAD" && CSRF_TOKEN) {
    headers.set("X-ColabLLM-CSRF", CSRF_TOKEN);
  }
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(url, {
    ...options,
    method,
    headers,
    credentials: "same-origin",
    body: options.json !== undefined ? JSON.stringify(options.json) : options.body,
  });
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
}

function activateTab(panelId) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    const active = btn.dataset.panel === panelId;
    btn.setAttribute("aria-selected", active ? "true" : "false");
    if (active) btn.setAttribute("focusgroupstart", "");
    else btn.removeAttribute("focusgroupstart");
    if (!Object.hasOwn(HTMLElement.prototype, "focusGroup")) {
      btn.tabIndex = active ? 0 : -1;
    }
  });
  document.querySelectorAll('[role="tabpanel"]').forEach((panel) => {
    if (panel.id === panelId) panel.removeAttribute("hidden");
    else panel.setAttribute("hidden", "until-found");
  });
}

function initNavigation() {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.panel));
  });
  document.querySelectorAll('[role="tabpanel"]').forEach((panel) => {
    panel.addEventListener("beforematch", () => {
      switchView("workbench");
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

function renderKPIs() {
  const running = state.instances.filter((i) => i.running).length;
  const ready = state.instances.filter((i) => i.running && i.ready).length;
  document.getElementById("kpi-running").textContent = String(running);
  document.getElementById("kpi-ready").textContent = String(ready);
  document.getElementById("kpi-transport").textContent = state.tunnel?.transport || "cloudflare";
  document.getElementById("kpi-services").textContent = String(state.services.length);
}

function renderInstances() {
  const grid = document.getElementById("instances-grid");
  document.getElementById("instances-count").textContent = `${state.instances.length} 个实例`;

  if (state.instances.length === 0) {
    grid.replaceChildren(
      el(
        "div",
        { className: "card" },
        el("p", { className: "muted", textContent: "尚无运行中或历史记录的 Colab 实例。点击右上角“启动新实例”即可创建。" }),
      ),
    );
    return;
  }

  const cards = state.instances.map((inst) => {
    let badgeTone = "";
    if (inst.corrupt) badgeTone = "danger";
    else if (inst.running && inst.ready) badgeTone = "ok";
    else if (inst.running) badgeTone = "warn";

    const urlHref = safeExternalUrl(inst.url);
    const titleText = inst.instance_arg ? `${inst.service} · #${inst.instance_arg}` : inst.service;

    const actions = [];
    if (inst.has_web_ui && urlHref) {
      actions.push(
        el("a", {
          className: "btn sm primary",
          href: urlHref,
          target: "_blank",
          rel: "noopener noreferrer",
          textContent: "打开 Web UI",
        }),
      );
    }
    actions.push(
      el("button", {
        type: "button",
        className: "btn sm secondary",
        textContent: "连接/凭据",
        onClick: () => openConnectDialog(inst),
      }),
      el("button", {
        type: "button",
        className: "btn sm secondary",
        textContent: "实时日志",
        onClick: () => openLogsDialog(inst),
      }),
      el("button", {
        type: "button",
        className: "btn sm secondary",
        textContent: "探活",
        onClick: () => runProbeInstance(inst),
      }),
    );
    if (inst.url) {
      actions.push(
        el("button", {
          type: "button",
          className: "btn sm secondary",
          textContent: inst.chat_compatible ? "对话" : "代理",
          onClick: () => jumpToProxy(inst),
        }),
      );
    }
    if (inst.running) {
      actions.push(
        el("button", {
          type: "button",
          className: "btn sm danger",
          disabled: state.readOnly,
          textContent: "停止",
          onClick: () => openDownDialog(inst),
        }),
      );
    } else {
      actions.push(
        el("button", {
          type: "button",
          className: "btn sm primary",
          disabled: state.readOnly,
          textContent: "重新启动",
          onClick: () => openUpDialog(inst.service, inst.instance_arg, inst.account || ""),
        }),
      );
    }

    const card = el(
      "article",
      { className: "card instance-card" },
      el(
        "div",
        { className: "card-head" },
        el(
          "div",
          { className: "card-title-group" },
          el("strong", { textContent: titleText }),
          el("span", { className: "muted", textContent: inst.stage_hint || inst.state_label }),
        ),
        el("span", { className: `badge ${badgeTone}`, textContent: inst.state_label }),
      ),
      el(
        "div",
        { className: "card-layout" },
        el(
          "div",
          null,
          el(
            "div",
            { className: "card-meta" },
            el("span", { textContent: `账户: ${inst.account || "-"}` }),
            el("span", { textContent: `Drive: ${inst.drive_account || "无"}` }),
            el("span", { textContent: `传输: ${inst.transport || "-"}` }),
            el("span", { textContent: `GPU: ${inst.session_gpu || "-"}` }),
          ),
          urlHref
            ? el("div", { className: "card-url", textContent: urlHref })
            : el("div", { className: "muted", textContent: inst.hostname ? `预设域名: ${inst.hostname}` : "暂无公网 URL" }),
        ),
        el("div", { className: "card-actions" }, ...actions),
      ),
    );
    return el("div", { className: "card-slot" }, card);
  });

  grid.replaceChildren(...cards);
}

function renderServices() {
  const q = (document.getElementById("service-search").value || "").trim().toLowerCase();
  const cat = document.getElementById("category-filter").value;
  const grid = document.getElementById("services-grid");

  const filtered = state.services.filter((svc) => {
    if (cat !== "all" && svc.category !== cat) return false;
    if (!q) return true;
    return `${svc.name} ${svc.summary} ${svc.category_label}`.toLowerCase().includes(q);
  });

  const cards = filtered.map((svc) => {
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
        el("span", { className: "badge", textContent: svc.category_label || CATEGORY_LABELS[svc.category] || svc.category }),
      ),
      el(
        "div",
        { className: "card-meta" },
        el("span", { textContent: `GPU: ${svc.gpu || "CPU"}` }),
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

function renderProxyTargets() {
  const select = document.getElementById("proxy-target");
  const prev = select.value;
  const readyOnes = state.instances.filter((i) => i.url);
  if (readyOnes.length === 0) {
    select.replaceChildren(el("option", { value: "", textContent: "暂无已分配公网 URL 的实例" }));
    return;
  }
  const opts = readyOnes.map((i) => {
    const val = JSON.stringify({ service: i.service, instance: i.instance_arg || "default" });
    const label = `${i.session_name} (${i.ready ? "就绪" : i.state_label}) - ${i.url}`;
    return el("option", { value: val, textContent: label });
  });
  select.replaceChildren(...opts);
  if (prev && Array.from(select.options).some((o) => o.value === prev)) {
    select.value = prev;
  }
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

  const acctSummary = document.getElementById("accounts-summary");
  const ac = state.accounts || {};
  acctSummary.replaceChildren(
    el("span", { className: "kv-key", textContent: "生效计算账户" }),
    el("span", { className: "kv-val", textContent: `${ac.effective_account || "未解析"} (${ac.effective_source || "-"})` }),
    el("span", { className: "kv-key", textContent: "生效共享 Drive" }),
    el("span", { className: "kv-val", textContent: `${ac.effective_drive_account || "无"} (${ac.drive_source || "-"})` }),
  );

  const tbody = document.getElementById("accounts-tbody");
  const list = ac.accounts || [];
  if (!list.length) {
    tbody.replaceChildren(el("tr", null, el("td", { colSpan: "4", className: "muted", textContent: "未检测到 colab-ext 多账户配置" })));
    return;
  }
  const trs = list.map((a) =>
    el(
      "tr",
      null,
      el("td", { textContent: `${a.name}${a.active ? " (默认)" : ""}` }),
      el("td", { textContent: a.email || "-" }),
      el("td", { textContent: a.drive_account || "-" }),
      el("td", null, el("span", { className: `badge ${a.has_drive_consent ? "ok" : "warn"}`, textContent: a.has_drive_consent ? "已授权文件" : "无本地授权" })),
    ),
  );
  tbody.replaceChildren(...trs);
}

function populateAccountSelectors() {
  const names = (state.accounts?.accounts || []).map((a) => a.name);
  fillSelectOptions(document.getElementById("up-account"), names, { emptyLabel: "自动解析" });
  fillSelectOptions(document.getElementById("up-drive-account"), names, { emptyLabel: "自动解析" });
  fillSelectOptions(document.getElementById("wb-account"), names, { emptyLabel: "默认规则解析" });
  fillSelectOptions(document.getElementById("doctor-account"), names, { emptyLabel: "自动解析" });
  fillSelectOptions(document.getElementById("doctor-drive-account"), names, { emptyLabel: "跟随账户绑定" });
}

async function loadBootstrap() {
  const data = await apiFetch("/api/bootstrap");
  state.version = data.version || "";
  state.readOnly = Boolean(data.read_only);
  state.defaultAccount = data.default_account || "";
  state.services = data.services || [];
  state.servicesMap = new Map(state.services.map((s) => [s.name, s]));
  state.instances = data.instances || [];
  state.accounts = data.accounts || null;
  state.tunnel = data.tunnel || null;
  state.tasks = data.tasks || [];

  document.getElementById("app-version").textContent = `v${state.version}`;
  document.getElementById("readonly-badge").hidden = !state.readOnly;
  document.querySelectorAll("[data-open-up]").forEach((btn) => {
    btn.disabled = state.readOnly;
  });

  fillSelectOptions(document.getElementById("up-service"), state.services, {
    valueFn: (s) => s.name,
    labelFn: (s) => `${s.name} — ${s.summary || s.category_label}`,
  });
  fillSelectOptions(document.getElementById("wb-service"), state.services, {
    valueFn: (s) => s.name,
    labelFn: (s) => `${s.name} — ${s.summary || s.category_label}`,
  });

  populateAccountSelectors();
  renderKPIs();
  renderInstances();
  renderServices();
  renderProxyTargets();
  renderTasksList();
  renderTunnelAndAccounts();

  if (state.services.length && !document.getElementById("wb-subcommand").options.length) {
    await selectWorkbenchService(state.services[0].name);
  }
}

/* SSE live updates */
function connectEventsSse() {
  if (state.eventsSource) {
    state.eventsSource.close();
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
      state.instances = payload.instances || [];
      state.tasks = payload.tasks || [];
      renderKPIs();
      renderInstances();
      renderProxyTargets();
      renderTasksList();
    } catch {
      // Ignore malformed frame.
    }
  });
  src.addEventListener("error", () => {
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
  const mode = document.getElementById("up-tunnel-mode").value;
  const domainVal = document.getElementById("up-domain-input").value.trim();
  return {
    instance: document.getElementById("up-instance").value.trim(),
    account: document.getElementById("up-account").value.trim(),
    drive_account: document.getElementById("up-drive-account").value.trim(),
    tunnel_mode: mode,
    domain: mode === "domain" ? domainVal : "",
    base_domain: mode === "base_domain" ? domainVal : "",
    gpu: document.getElementById("up-gpu").value.trim(),
    transport: document.getElementById("up-transport").value.trim(),
    skip: document.getElementById("up-skip").value.trim(),
    hf_token: document.getElementById("up-hf-token").value.trim(),
    auto_restart: document.getElementById("up-auto-restart").checked,
    no_drive: document.getElementById("up-no-drive").checked,
    new_credentials: document.getElementById("up-new-credentials").checked,
    restart_delay: document.getElementById("up-restart-delay").value.trim(),
    idle_check: document.getElementById("up-idle-check").value.trim(),
    max_restarts: document.getElementById("up-max-restarts").value.trim(),
    ready_timeout: document.getElementById("up-ready-timeout").value.trim(),
    extra,
    dry_run: Boolean(dryRun),
  };
}

function updateUpCommandPreview() {
  const svc = document.getElementById("up-service").value;
  if (!svc) return;
  const p = getUpPayload(false);
  const args = ["colabllm", svc];
  if (p.instance) args.push("-i", p.instance);
  if (p.account) args.push("--account", p.account);
  if (p.drive_account) args.push("--drive-account", p.drive_account);
  args.push("up");
  if (p.tunnel_mode === "quick") args.push("--quick-tunnel");
  else if (p.tunnel_mode === "domain") args.push(p.domain ? `--domain=${p.domain}` : "--domain");
  else if (p.tunnel_mode === "base_domain" && p.base_domain) args.push("--base-domain", p.base_domain);
  if (p.gpu) args.push("--gpu", p.gpu);
  if (p.transport) args.push("--transport", p.transport);
  if (p.skip) args.push("--skip", p.skip);
  if (p.no_drive) args.push("--no-drive");
  if (p.hf_token) args.push("--hf-token", "***");
  if (p.new_credentials) args.push("--new-credentials");
  if (p.auto_restart) args.push("--auto-restart");
  if (p.restart_delay) args.push("--restart-delay", p.restart_delay);
  if (p.idle_check) args.push("--idle-check", p.idle_check);
  if (p.max_restarts && Number(p.max_restarts) > 0) args.push("--max-restarts", p.max_restarts);
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
  let text = state.logsRawText || "(日志文件为空或尚未生成)";
  if (filter) {
    const lines = text.split(/\r?\n/).filter((line) => line.toLowerCase().includes(filter));
    text = lines.length ? lines.join("\n") : "(无匹配过滤关键词的日志行)";
  }
  viewer.textContent = text;
  if (autoScroll) {
    viewer.scrollTop = viewer.scrollHeight;
  }
}

function openLogsDialog(inst) {
  state.logsTarget = inst;
  state.logsRawText = "";
  document.getElementById("logs-dialog-title").textContent = `实时日志 · ${inst.session_name}`;
  document.getElementById("logs-filter").value = "";
  document.getElementById("logs-viewer").textContent = "正在连接日志流...";
  document.getElementById("logs-dialog").showModal();

  if (state.logsSource) {
    state.logsSource.close();
  }
  const url = `/api/instances/${encodeURIComponent(inst.service)}/logs?instance=${encodeURIComponent(inst.instance_arg || "default")}&lines=250&stream=1`;
  const src = new EventSource(url);
  state.logsSource = src;
  src.addEventListener("init", (evt) => {
    try {
      const data = JSON.parse(evt.data);
      state.logsRawText = data.content || "";
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

function openConnectDialog(inst) {
  state.connectTarget = inst;
  state.connectSecrets = null;
  document.getElementById("connect-dialog-title").textContent = `连接信息 · ${inst.session_name}`;
  renderConnectMeta(inst, null);
  document.getElementById("connect-reveal-btn").disabled = state.readOnly;
  renderConnectSnippets();
  document.getElementById("connect-dialog").showModal();
}

async function runProbeInstance(inst) {
  try {
    const q = new URLSearchParams({ instance: inst.instance_arg || "" });
    if (inst.account) q.set("account", inst.account);
    const res = await apiFetch(`/api/instances/${encodeURIComponent(inst.service)}/probe?${q.toString()}`);
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

/* Same-origin Chat & Proxy Playground */
function jumpToProxy(inst) {
  switchView("workbench");
  activateTab("panel-chat");
  const sel = document.getElementById("proxy-target");
  const val = JSON.stringify({ service: inst.service, instance: inst.instance_arg || "default" });
  if (Array.from(sel.options).some((o) => o.value === val)) {
    sel.value = val;
  }
  document.getElementById("proxy-path").value = inst.chat_compatible ? "v1/models" : "";
}

function getSelectedProxyTarget() {
  const raw = document.getElementById("proxy-target").value;
  if (!raw) throw new Error("请先选择一个已有公网 URL 的运行实例");
  return JSON.parse(raw);
}

function renderChatMessages() {
  const box = document.getElementById("chat-messages");
  if (!state.chatHistory.length) {
    box.replaceChildren(el("p", { className: "muted", textContent: "在此直接通过本机同源代理测试 OpenAI /v1/chat/completions 流式响应。" }));
    return;
  }
  const nodes = state.chatHistory.map((m) =>
    el(
      "div",
      { className: `chat-msg ${m.role}` },
      el("span", { className: "chat-msg-role", textContent: m.role === "user" ? "用户" : "模型响应" }),
      el("div", { textContent: m.text }),
    ),
  );
  box.replaceChildren(...nodes);
  box.scrollTop = box.scrollHeight;
}

async function sendStreamingChat(evt) {
  evt.preventDefault();
  if (state.readOnly) {
    showToast("只读模式禁止发送推理请求", "warn");
    return;
  }
  let target;
  try {
    target = getSelectedProxyTarget();
  } catch (err) {
    showToast(err.message, "warn");
    return;
  }
  const userText = document.getElementById("chat-input").value.trim();
  if (!userText) return;
  const sysText = document.getElementById("chat-system").value.trim();
  const model = document.getElementById("chat-model").value.trim() || "default";
  const thinking = document.getElementById("chat-thinking").checked;

  const messages = [];
  if (sysText) messages.push({ role: "system", content: sysText });
  for (const h of state.chatHistory) {
    messages.push({ role: h.role, content: h.rawContent ?? h.text });
  }
  messages.push({ role: "user", content: userText });

  state.chatHistory.push({ role: "user", text: userText, rawContent: userText });
  const assistantTurn = { role: "assistant", text: "", rawContent: "", reasoning: "" };
  state.chatHistory.push(assistantTurn);
  document.getElementById("chat-input").value = "";
  renderChatMessages();

  const sendBtn = document.getElementById("chat-send-btn");
  sendBtn.disabled = true;
  try {
    const url = `/api/proxy/${encodeURIComponent(target.service)}/${encodeURIComponent(target.instance)}/v1/chat/completions`;
    const body = {
      model,
      messages,
      stream: true,
      chat_template_kwargs: { enable_thinking: thinking },
    };
    const resp = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-ColabLLM-CSRF": CSRF_TOKEN,
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const errTxt = await resp.text();
      throw new Error(`HTTP ${resp.status}: ${errTxt}`);
    }
    const ct = resp.headers.get("Content-Type") || "";
    if (!ct.includes("text/event-stream") || !resp.body) {
      const json = await resp.json();
      const msg = json?.choices?.[0]?.message || {};
      assistantTurn.rawContent = msg.content || JSON.stringify(json, null, 2);
      assistantTurn.text = msg.reasoning_content ? `[思考]\n${msg.reasoning_content}\n\n${assistantTurn.rawContent}` : assistantTurn.rawContent;
      renderChatMessages();
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === "[DONE]") continue;
        try {
          const chunk = JSON.parse(payload);
          const delta = chunk?.choices?.[0]?.delta || {};
          if (delta.reasoning_content) assistantTurn.reasoning += delta.reasoning_content;
          if (delta.content) assistantTurn.rawContent += delta.content;
          assistantTurn.text = assistantTurn.reasoning
            ? `[思考]\n${assistantTurn.reasoning}\n\n[回答]\n${assistantTurn.rawContent}`
            : assistantTurn.rawContent;
          renderChatMessages();
        } catch {
          // Ignore non-JSON chunk.
        }
      }
    }
  } catch (err) {
    assistantTurn.text = `请求失败: ${err.message}`;
    renderChatMessages();
    showToast(err.message, "danger");
  } finally {
    sendBtn.disabled = false;
  }
}

/* Wire all event handlers */
function initInteractions() {
  document.getElementById("refresh-btn").addEventListener("click", async () => {
    try {
      await loadBootstrap();
      showToast("状态已刷新", "ok", 2200);
    } catch (err) {
      showToast(`刷新失败: ${err.message}`, "danger");
    }
  });

  document.getElementById("service-search").addEventListener("input", renderServices);
  document.getElementById("category-filter").addEventListener("change", renderServices);

  // Clean up modal dialog state and flush deferred toasts on close
  document.querySelectorAll("dialog").forEach((dlg) => {
    dlg.addEventListener("close", () => {
      if (dlg.id === "logs-dialog" && state.logsSource) {
        state.logsSource.close();
        state.logsSource = null;
      }
      if (dlg.id !== "down-dialog") {
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
      showToast(`已提交停止任务: ${task.command_preview}`, "ok");
    } catch (err) {
      showToast(`停止失败: ${err.message}`, "danger");
    }
  });

  // Up dialog events
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
    const svc = document.getElementById("up-service").value;
    const out = document.getElementById("up-dryrun-output");
    out.hidden = false;
    out.textContent = "正在生成计划...";
    try {
      const res = await apiFetch(`/api/instances/${encodeURIComponent(svc)}/up`, {
        method: "POST",
        json: getUpPayload(true),
      });
      document.getElementById("up-cmd-preview").textContent = res.command;
      out.textContent = res.output || "(无输出)";
    } catch (err) {
      out.textContent = `Dry-run 失败: ${err.message}`;
    }
  });

  document.getElementById("up-form").addEventListener("submit", async (evt) => {
    evt.preventDefault();
    const svc = document.getElementById("up-service").value;
    try {
      const task = await apiFetch(`/api/instances/${encodeURIComponent(svc)}/up`, {
        method: "POST",
        json: getUpPayload(false),
      });
      document.getElementById("up-dialog").close();
      showToast(`已在后台启动: ${task.command_preview}`, "ok");
      await loadBootstrap();
    } catch (err) {
      const out = document.getElementById("up-dryrun-output");
      out.hidden = false;
      out.textContent = `启动失败: ${err.message}`;
    }
  });

  // Logs modal filters & export
  document.getElementById("logs-filter").addEventListener("input", renderLogsFiltered);
  document.getElementById("logs-autoscroll").addEventListener("change", renderLogsFiltered);
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
      const sec = await apiFetch(`/api/instances/${encodeURIComponent(inst.service)}/secret`, {
        method: "POST",
        json: { instance: inst.instance_arg || "" },
      });
      state.connectSecrets = sec;
      renderConnectMeta(inst, sec);
      renderConnectSnippets();
    } catch (err) {
      showToast(`获取凭据失败: ${err.message}`, "danger");
    }
  });
  const copyFromSnippet = (partIdx) => {
    const txt = document.getElementById("connect-snippet").textContent || "";
    const parts = txt.split("\n\n# Python OpenAI SDK\n");
    const chosen = partIdx === 0 ? parts[0].replace(/^# curl[^\n]*\n/, "") : parts[1] || txt;
    navigator.clipboard?.writeText(chosen).then(
      () => showToast("已复制到剪贴板", "ok", 2000),
      () => showToast("浏览器拒绝剪贴板访问，请手动复制", "warn"),
    );
  };
  document.getElementById("connect-copy-curl").addEventListener("click", () => copyFromSnippet(0));
  document.getElementById("connect-copy-py").addEventListener("click", () => copyFromSnippet(1));

  // Workbench CLI
  document.getElementById("wb-service").addEventListener("change", (e) => selectWorkbenchService(e.target.value));
  document.getElementById("wb-subcommand").addEventListener("change", renderWorkbenchFields);
  document.getElementById("wb-instance").addEventListener("input", scheduleWorkbenchPreview);
  document.getElementById("wb-account").addEventListener("change", scheduleWorkbenchPreview);
  document.getElementById("wb-form").addEventListener("input", scheduleWorkbenchPreview);
  document.getElementById("wb-form").addEventListener("change", scheduleWorkbenchPreview);
  document.getElementById("wb-copy-cmd").addEventListener("click", () => {
    const cmd = document.getElementById("wb-cmd-preview").textContent;
    navigator.clipboard?.writeText(cmd).then(
      () => showToast("已复制 CLI 命令", "ok", 2000),
      () => showToast("复制失败，请手动选中复制", "warn"),
    );
  });

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

  // Chat & proxy
  document.getElementById("chat-load-models").addEventListener("click", async () => {
    try {
      const target = getSelectedProxyTarget();
      const res = await apiFetch(`/api/proxy/${encodeURIComponent(target.service)}/${encodeURIComponent(target.instance)}/v1/models`);
      const firstId = res?.data?.[0]?.id;
      if (firstId) {
        document.getElementById("chat-model").value = firstId;
        showToast(`已发现模型: ${firstId}`, "ok");
      } else {
        showToast("已连通，但未返回模型列表", "info");
      }
    } catch (err) {
      showToast(`探测模型失败: ${err.message}`, "danger");
    }
  });
  document.getElementById("chat-form").addEventListener("submit", sendStreamingChat);

  document.getElementById("proxy-http-form").addEventListener("submit", async (evt) => {
    evt.preventDefault();
    if (state.readOnly) {
      showToast("只读模式禁止发送代理请求", "warn");
      return;
    }
    const out = document.getElementById("proxy-output");
    out.textContent = "请求中...";
    try {
      const target = getSelectedProxyTarget();
      const method = document.getElementById("proxy-method").value;
      const rel = document.getElementById("proxy-path").value.trim().replace(/^\/+/, "");
      const bodyRaw = document.getElementById("proxy-body").value.trim();
      const res = await apiFetch(
        `/api/proxy/${encodeURIComponent(target.service)}/${encodeURIComponent(target.instance)}/${rel}`,
        {
          method,
          headers: method === "POST" && bodyRaw ? { "Content-Type": "application/json" } : {},
          body: method === "POST" && bodyRaw ? bodyRaw : undefined,
        },
      );
      out.textContent = typeof res === "string" ? res : JSON.stringify(res, null, 2);
    } catch (err) {
      out.textContent = `代理请求失败: ${err.message}`;
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
      const res = await apiFetch("/api/frp-status");
      out.textContent = JSON.stringify(res, null, 2);
    } catch (err) {
      out.textContent = `FRP 检查失败: ${err.message}`;
    }
  });

  // Doctor
  document.getElementById("doctor-run-btn").addEventListener("click", async () => {
    const banner = document.getElementById("doctor-banner");
    const tbody = document.getElementById("doctor-tbody");
    banner.textContent = "正在执行环境体检...";
    try {
      const q = new URLSearchParams({
        online: document.getElementById("doctor-online").checked ? "1" : "0",
        no_drive: document.getElementById("doctor-no-drive").checked ? "1" : "0",
        account: document.getElementById("doctor-account").value.trim(),
        drive_account: document.getElementById("doctor-drive-account").value.trim(),
        transport: document.getElementById("doctor-transport").value.trim(),
      });
      const res = await apiFetch(`/api/doctor?${q.toString()}`);
      banner.textContent = `体检完成：overall=${res.ok ? "PASS" : "FAIL"} | transport=${res.transport || "-"} | account=${res.account || "-"}`;
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

  window.addEventListener("beforeunload", () => {
    state.eventsSource?.close();
    state.logsSource?.close();
    state.activeTaskSource?.close();
  });
}

async function main() {
  setupFeatureFallbacks();
  initTheme();
  initNavigation();
  initInteractions();
  renderChatMessages();
  try {
    await loadBootstrap();
    connectEventsSse();
  } catch (err) {
    showToast(`初始化失败: ${err.message}`, "danger");
  }
}

main();
