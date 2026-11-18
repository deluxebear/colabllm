/**
 * ColabLLM Copilot Studio + Slide-out CopilotSidebar (<CopilotChat />, <CopilotThreadsDrawer />, Generative UI).
 * Uses pure-DOM construction for Trusted Types & CSP strict compliance.
 */

import { initStudios } from "./copilot_studios.js";

const STORAGE_THREADS_KEY = "colabllm.copilot.threads.v1";

const CLIENT_TOOLS_META = [
  {
    name: "list_colab_instances",
    label: "查询舰队实例列表",
    safeReadOnly: true,
    schema: {
      type: "function",
      function: {
        name: "list_colab_instances",
        description: "List all active or stopped ColabLLM runtime instances, their URLs, auth mode, and readiness.",
        parameters: { type: "object", properties: {} },
      },
    },
  },
  {
    name: "inspect_runtime_capabilities",
    label: "查询运行时 API 能力",
    safeReadOnly: true,
    schema: {
      type: "function",
      function: {
        name: "inspect_runtime_capabilities",
        description: "Inspect HTTP endpoints, OpenAI compatibility, and parameter constraints for a ColabLLM service.",
        parameters: {
          type: "object",
          properties: {
            service_name: { type: "string", description: "Service name such as qwen-flash, ollama, flux-klein, nemotron-transcribe" },
          },
          required: ["service_name"],
        },
      },
    },
  },
  {
    name: "render_ui_widget",
    label: "渲染 A2UI 卡片 / 表格 / KPI",
    safeReadOnly: true,
    schema: {
      type: "function",
      function: {
        name: "render_ui_widget",
        description: "Render an interactive A2UI widget card (kpi_grid, table, or status_card) inline in the Copilot chat.",
        parameters: {
          type: "object",
          properties: {
            widget_type: { type: "string", enum: ["kpi_grid", "table", "status_card"] },
            title: { type: "string" },
            items: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  label: { type: "string" },
                  value: { type: "string" },
                  status: { type: "string" },
                },
              },
            },
            columns: { type: "array", items: { type: "string" } },
            rows: { type: "array", items: { type: "array", items: { type: "string" } } },
            message: { type: "string" },
          },
          required: ["widget_type", "title"],
        },
      },
    },
  },
  {
    name: "generate_image_on_runtime",
    label: "跨运行时生成图像并预览",
    safeReadOnly: false,
    schema: {
      type: "function",
      function: {
        name: "generate_image_on_runtime",
        description: "Call an image runtime (flux-klein, qwen-image, or minimax-h3-image) via same-origin proxy to generate an image.",
        parameters: {
          type: "object",
          properties: {
            prompt: { type: "string" },
            session_name: { type: "string", description: "Optional instance session name; defaults to first ready image runtime." },
          },
          required: ["prompt"],
        },
      },
    },
  },
  {
    name: "synthesize_speech_on_runtime",
    label: "跨运行时合成语音并播放",
    safeReadOnly: false,
    schema: {
      type: "function",
      function: {
        name: "synthesize_speech_on_runtime",
        description: "Call a running TTS runtime (qwen3-tts, indextts, or drama3) via same-origin proxy to synthesize playable audio.",
        parameters: {
          type: "object",
          properties: {
            text: { type: "string" },
            voice: { type: "string" },
            session_name: { type: "string" },
          },
          required: ["text"],
        },
      },
    },
  },
];

function makeId(prefix = "id") {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID().slice(0, 8)}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });
}

function formatEndpointTitle(endpoint) {
  const map = {
    chat_completions: "OpenAI /v1/chat/completions",
    responses: "OpenAI /v1/responses",
    completions: "OpenAI /v1/completions",
    anthropic_messages: "Anthropic /v1/messages",
    ollama_chat: "Ollama /api/chat",
  };
  return map[endpoint] || endpoint;
}

export function initCopilotStudio(deps) {
  const { appState, el, toast, copyText, switchView } = deps;

  const ckState = {
    threads: [],
    activeThreadId: "",
    selectedSession: "",
    selectedServiceFallback: "",
    showAllSurfaces: false,
    activeSurface: "chat",
    attachments: [],
    abortController: null,
    streaming: false,
    blobUrls: [],
    drawerHasEntered: false,
    studios: null,
  };

  function getCsrfToken() {
    const meta = document.querySelector('meta[name="colabllm-csrf"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function trackBlobUrl(url) {
    if (url && url.startsWith("blob:")) {
      ckState.blobUrls.push(url);
    }
    return url;
  }

  function proxyUrl(sessionName, subpath) {
    const parts = sessionName.split("-");
    // Resolve via instance record so service/instance split handles hyphenated service names accurately
    const inst = (appState.instances || []).find((i) => i.session_name === sessionName);
    const svc = inst ? inst.service : parts[0];
    const instArg = inst ? inst.instance_arg || "default" : "default";
    const cleanSub = String(subpath || "").replace(/^\/+/, "");
    return `/api/proxy/${encodeURIComponent(svc)}/${encodeURIComponent(instArg)}/${cleanSub}`;
  }

  async function proxyFetch(sessionName, subpath, opts = {}) {
    const method = (opts.method || "GET").toUpperCase();
    const headers = new Headers(opts.headers || {});
    if (method === "POST" || method === "PUT" || method === "DELETE") {
      headers.set("X-ColabLLM-CSRF", getCsrfToken());
    }
    return fetch(proxyUrl(sessionName, subpath), {
      ...opts,
      method,
      headers,
      credentials: "same-origin",
    });
  }

  function isRunningInstance(inst) {
    return Boolean(inst && inst.running && !inst.stopping);
  }

  function getRunningInstances() {
    return (appState.instances || [])
      .filter(isRunningInstance)
      .sort(
        (a, b) =>
          Number(Boolean(b.ready)) - Number(Boolean(a.ready)) ||
          Number(Boolean(b.url)) - Number(Boolean(a.url))
      );
  }

  function getSelectedTarget() {
    if (ckState.selectedSession) {
      const found = (appState.instances || []).find(
        (i) => i.session_name === ckState.selectedSession && isRunningInstance(i)
      );
      if (found) return found;
    }
    return null;
  }

  function getSelectedServiceMeta() {
    const inst = getSelectedTarget();
    const svcName = inst?.service || ckState.selectedServiceFallback || "";
    return (appState.services || []).find((s) => s.name === svcName) || null;
  }

  function getSelectedProfile() {
    return getSelectedServiceMeta()?.api_profile || null;
  }

  function getSelectedModel() {
    return (document.getElementById("ck-model")?.value || "").trim();
  }

  /* ---------- Threads Persistence ---------- */
  function loadThreads() {
    try {
      const raw = localStorage.getItem(STORAGE_THREADS_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed) && parsed.length) {
          ckState.threads = parsed;
          ckState.activeThreadId = parsed[0].id;
          return;
        }
      }
    } catch {
      // Ignore corrupt localStorage entries
    }
    createThread(false);
  }

  function saveThreads() {
    try {
      // Strip oversized inline data URLs before persisting to localStorage
      const compact = ckState.threads.slice(0, 30).map((t) => ({
        ...t,
        messages: (t.messages || []).map((m) => ({
          ...m,
          attachments: (m.attachments || []).map((a) => ({
            kind: a.kind,
            name: a.name,
            url: a.url && a.url.startsWith("data:") ? "[inline-media-omitted]" : a.url,
          })),
        })),
      }));
      localStorage.setItem(STORAGE_THREADS_KEY, JSON.stringify(compact));
    } catch {
      // QuotaExceededError fallback
    }
  }

  function getActiveThread() {
    let t = ckState.threads.find((item) => item.id === ckState.activeThreadId);
    if (!t) {
      t = createThread(true);
    }
    return t;
  }

  function createThread(renderNow = true) {
    const t = {
      id: makeId("thr"),
      title: "新对话",
      targetSession: ckState.selectedSession || "",
      model: getSelectedModel(),
      createdAt: Date.now(),
      updatedAt: Date.now(),
      messages: [],
    };
    ckState.threads.unshift(t);
    ckState.activeThreadId = t.id;
    saveThreads();
    if (renderNow) {
      renderThreadsList();
      renderAllFeeds();
    }
    return t;
  }

  /* ---------- Safe Markdown + Code Block Renderer ---------- */
  function renderInlineText(text) {
    const frag = document.createDocumentFragment();
    const parts = String(text || "").split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g);
    for (const part of parts) {
      if (!part) continue;
      if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
        frag.appendChild(el("code", { class: "ck-inline-code" }, part.slice(1, -1)));
      } else if (part.startsWith("**") && part.endsWith("**") && part.length >= 4) {
        frag.appendChild(el("strong", {}, part.slice(2, -2)));
      } else {
        frag.appendChild(document.createTextNode(part));
      }
    }
    return frag;
  }

  function renderRichContent(container, rawText) {
    const nodes = [];
    const text = String(rawText || "");
    const fenceRe = /```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g;
    let lastIndex = 0;
    let match;

    function pushParagraphs(chunk) {
      const blocks = chunk.split(/\n{2,}/);
      for (const block of blocks) {
        const trimmed = block.trim();
        if (!trimmed) continue;
        const lines = trimmed.split("\n");
        if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
          nodes.push(
            el(
              "ul",
              {},
              ...lines.map((l) => el("li", {}, renderInlineText(l.replace(/^\s*[-*]\s+/, ""))))
            )
          );
        } else {
          for (const line of lines) {
            if (/^#{1,4}\s+/.test(line)) {
              nodes.push(el("p", {}, el("strong", {}, renderInlineText(line.replace(/^#{1,4}\s+/, "")))));
            } else {
              nodes.push(el("p", {}, renderInlineText(line)));
            }
          }
        }
      }
    }

    while ((match = fenceRe.exec(text)) !== null) {
      pushParagraphs(text.slice(lastIndex, match.index));
      const lang = match[1] || "text";
      const code = match[2];
      const copyBtn = el("button", { type: "button", class: "btn small secondary" }, "复制代码");
      copyBtn.addEventListener("click", () => copyText(code, "代码已复制"));
      nodes.push(
        el(
          "div",
          { class: "ck-code-block-wrap" },
          el("div", { class: "ck-code-block-head" }, el("span", {}, lang), copyBtn),
          el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, code))
        )
      );
      lastIndex = fenceRe.lastIndex;
    }
    pushParagraphs(text.slice(lastIndex));
    container.replaceChildren(...nodes);
  }

  /* ---------- Generative UI Widget & Tool Card Renderers ---------- */
  function renderWidgetNode(widget) {
    if (!widget) return null;
    if (widget.widget_type === "kpi_grid" && Array.isArray(widget.items)) {
      return el(
        "div",
        { class: "ck-genui-card" },
        el("strong", {}, widget.title || "实时指标"),
        el(
          "div",
          { class: "ck-genui-kpi-grid" },
          ...widget.items.map((item) =>
            el(
              "div",
              { class: "ck-kpi-tile" },
              el("span", { class: "ck-kpi-label" }, item.label || "-"),
              el("span", { class: "ck-kpi-value" }, item.value || "-")
            )
          )
        )
      );
    }
    if (widget.widget_type === "table" && Array.isArray(widget.rows)) {
      const cols = Array.isArray(widget.columns) ? widget.columns : [];
      return el(
        "div",
        { class: "ck-genui-card" },
        el("strong", {}, widget.title || "数据表格"),
        el(
          "div",
          { class: "ck-table-wrap" },
          el(
            "table",
            { class: "ck-data-table" },
            cols.length ? el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", {}, c)))) : null,
            el(
              "tbody",
              {},
              ...widget.rows.map((r) => el("tr", {}, ...(Array.isArray(r) ? r : [r]).map((cell) => el("td", {}, String(cell)))))
            )
          )
        )
      );
    }
    if (widget.widget_type === "media_image" && widget.url) {
      return el(
        "div",
        { class: "ck-genui-card" },
        el("strong", {}, widget.title || "生成图像"),
        el("img", { src: widget.url, alt: widget.title || "Generated", class: "ck-media-thumb" })
      );
    }
    if (widget.widget_type === "media_audio" && widget.url) {
      return el(
        "div",
        { class: "ck-genui-card" },
        el("strong", {}, widget.title || "合成语音"),
        el("audio", { controls: "", src: widget.url })
      );
    }
    return el(
      "div",
      { class: "ck-genui-card" },
      el("strong", {}, widget.title || "状态卡片"),
      widget.message ? el("p", {}, widget.message) : null
    );
  }

  function renderToolCard(tc) {
    const statusMap = {
      pending: ["等待执行", "neutral"],
      awaiting_approval: ["需要人工确认", "warn"],
      running: ["执行中", "warn"],
      done: ["已完成", "ok"],
      rejected: ["已拒绝", "danger"],
      error: ["失败", "danger"],
    };
    const [statusText, badgeTone] = statusMap[tc.status || "done"] || ["完成", "neutral"];
    const card = el(
      "details",
      { class: "ck-tool-card", open: tc.status === "awaiting_approval" || undefined },
      el("summary", {}, el("span", {}, `工具: ${tc.function?.name || "tool"}`), el("span", { class: `badge ${badgeTone}` }, statusText)),
      el(
        "div",
        { class: "ck-acc-body" },
        el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, tc.function?.arguments || "{}")),
        tc.resultText
          ? el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, tc.resultText))
          : null,
        tc.status === "awaiting_approval" && tc._onApprove
          ? el(
              "div",
              { class: "ck-presets-row" },
              (() => {
                const approve = el("button", { type: "button", class: "btn small primary" }, "批准执行");
                approve.addEventListener("click", () => tc._onApprove(true));
                return approve;
              })(),
              (() => {
                const reject = el("button", { type: "button", class: "btn small danger" }, "拒绝");
                reject.addEventListener("click", () => tc._onApprove(false));
                return reject;
              })()
            )
          : null
      )
    );
    return card;
  }

  function renderMessageNode(msg) {
    const roleLabel = msg.role === "user" ? "用户" : msg.role === "assistant" ? "Copilot 助手" : "工具";
    const card = el("article", { class: "ck-msg", "data-role": msg.role });
    card.appendChild(
      el(
        "div",
        { class: "ck-msg-head" },
        el("strong", {}, roleLabel),
        msg.model ? el("span", { class: "ck-mono" }, msg.model) : null
      )
    );

    if (msg.attachments?.length) {
      const row = el(
        "div",
        { class: "ck-attachments" },
        ...msg.attachments.map((att) =>
          att.kind === "image" && att.url && att.url !== "[inline-media-omitted]"
            ? el("img", { src: att.url, alt: att.name, class: "ck-media-thumb" })
            : el("span", { class: "ck-attach-chip" }, `${att.kind === "audio" ? "音频" : "媒体"}: ${att.name}`)
        )
      );
      card.appendChild(row);
    }

    if (msg.reasoning) {
      const rDetails = el(
        "details",
        { class: "ck-reasoning-card", open: msg.isStreamingReasoning || undefined },
        el("summary", {}, `思考过程 (${msg.reasoning.length} 字符)`),
        el("div", { class: "ck-acc-body" }, el("p", {}, msg.reasoning))
      );
      card.appendChild(rDetails);
    }

    if (msg.toolCalls?.length) {
      for (const tc of msg.toolCalls) {
        card.appendChild(renderToolCard(tc));
      }
    }

    if (msg.uiWidgets?.length) {
      for (const w of msg.uiWidgets) {
        const wNode = renderWidgetNode(w);
        if (wNode) card.appendChild(wNode);
      }
    }

    const bodyDiv = el("div", { class: "ck-msg-body" });
    if (msg.content) {
      renderRichContent(bodyDiv, msg.content);
    } else if (msg.isStreaming && !msg.reasoning && !msg.toolCalls?.length) {
      bodyDiv.appendChild(el("p", { class: "muted" }, "正在思考与生成..."));
    }
    card.appendChild(bodyDiv);
    return card;
  }

  function renderFeedInto(feedEl, welcomeEl) {
    if (!feedEl) return;
    const thread = getActiveThread();
    const visibleMsgs = (thread.messages || []).filter((m) => m.role !== "tool");
    if (welcomeEl) {
      welcomeEl.hidden = visibleMsgs.length > 0;
    }
    feedEl.replaceChildren(...visibleMsgs.map(renderMessageNode));
    feedEl.scrollTop = feedEl.scrollHeight;
  }

  function renderAllFeeds() {
    renderFeedInto(document.getElementById("ck-chat-feed"), document.getElementById("ck-welcome-screen"));
    renderFeedInto(document.getElementById("ck-drawer-feed"), null);
  }

  function renderThreadsList() {
    const listEl = document.getElementById("ck-threads-list");
    if (!listEl) return;
    const q = (document.getElementById("ck-thread-search")?.value || "").trim().toLowerCase();
    const filtered = ckState.threads.filter((t) => {
      if (!q) return true;
      if ((t.title || "").toLowerCase().includes(q)) return true;
      return (t.messages || []).some((m) => String(m.content || "").toLowerCase().includes(q));
    });

    if (!filtered.length) {
      listEl.replaceChildren(el("li", { class: "muted" }, "无匹配会话"));
      return;
    }

    listEl.replaceChildren(
      ...filtered.map((t) => {
        const isActive = t.id === ckState.activeThreadId;
        const selectBtn = el(
          "button",
          { type: "button", class: "ck-thread-select" },
          el("span", { class: "ck-thread-title" }, t.title || "未命名会话"),
          el("span", { class: "ck-thread-meta" }, `${(t.messages || []).filter((m) => m.role !== "tool").length} 条消息`)
        );
        selectBtn.addEventListener("click", () => {
          ckState.activeThreadId = t.id;
          renderThreadsList();
          renderAllFeeds();
        });
        const delBtn = el("button", { type: "button", class: "btn icon-btn", title: "删除会话", "aria-label": "删除会话" }, "✕");
        delBtn.addEventListener("click", () => {
          ckState.threads = ckState.threads.filter((x) => x.id !== t.id);
          if (!ckState.threads.length) createThread(false);
          else if (ckState.activeThreadId === t.id) ckState.activeThreadId = ckState.threads[0].id;
          saveThreads();
          renderThreadsList();
          renderAllFeeds();
        });
        return el("li", { class: "ck-thread-item", "data-active": isActive ? "true" : "false" }, selectBtn, delBtn);
      })
    );
  }

  /* ---------- Client-Side Generative UI Tools Execution ---------- */
  async function executeClientTool(name, args) {
    if (name === "list_colab_instances") {
      return (appState.instances || []).map((i) => ({
        session_name: i.session_name,
        service: i.service,
        state: i.state_label,
        ready: i.ready,
        url: i.url || null,
        openai_compatible: Boolean(i.openai_compatible),
        primary_surface: i.primary_surface,
      }));
    }
    if (name === "inspect_runtime_capabilities") {
      const svc = (appState.services || []).find((s) => s.name === args.service_name);
      if (!svc) return { error: `Unknown service: ${args.service_name}` };
      return {
        name: svc.name,
        category: svc.category_label,
        auth: svc.auth,
        openai_compatible: svc.openai_compatible,
        api_profile: svc.api_profile,
      };
    }
    if (name === "render_ui_widget") {
      return { status: "rendered", widget: args };
    }
    if (name === "generate_image_on_runtime") {
      const running = getRunningInstances();
      const target =
        running.find((i) => i.session_name === args.session_name && i.url) ||
        running.find((i) => i.primary_surface === "image" && i.ready && i.url) ||
        getSelectedTarget();
      if (!target?.url) return { error: "未找到可用的图像生成运行时实例" };
      const resp = await proxyFetch(target.session_name, "v1/images/generations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: target.service, prompt: args.prompt, response_format: "b64_json", n: 1 }),
      });
      const data = await resp.json();
      if (!resp.ok) return { error: data.error?.message || data.error || resp.statusText };
      const first = data.data?.[0] || {};
      const url = first.b64_json ? `data:image/png;base64,${first.b64_json}` : first.url;
      return {
        status: "ok",
        session_name: target.session_name,
        widget: url ? { widget_type: "media_image", title: `${target.service} 生成结果`, url } : null,
      };
    }
    if (name === "synthesize_speech_on_runtime") {
      const running = getRunningInstances();
      const target =
        running.find((i) => i.session_name === args.session_name && i.url) ||
        running.find((i) => i.primary_surface === "audio" && i.ready && i.url) ||
        getSelectedTarget();
      if (!target?.url) return { error: "未找到可用的语音合成运行时实例" };
      const resp = await proxyFetch(target.session_name, "v1/audio/speech", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: target.service, input: args.text, voice: args.voice || "default", response_format: "wav" }),
      });
      if (!resp.ok) return { error: await resp.text() };
      const url = trackBlobUrl(URL.createObjectURL(await resp.blob()));
      return {
        status: "ok",
        session_name: target.session_name,
        widget: { widget_type: "media_audio", title: `${target.service} 合成语音`, url },
      };
    }
    return { error: `Unsupported client tool: ${name}` };
  }

  /* ---------- Chat Streaming & Multi-Protocol Dispatcher ---------- */
  function setChatBusy(busy) {
    ckState.streaming = busy;
    for (const id of ["ck-chat-progress", "ck-drawer-progress"]) {
      const p = document.getElementById(id);
      if (p) p.hidden = !busy;
    }
    for (const id of ["ck-chat-stop", "ck-drawer-stop"]) {
      const b = document.getElementById(id);
      if (b) b.hidden = !busy;
    }
    for (const id of ["ck-chat-send", "ck-drawer-send"]) {
      const b = document.getElementById(id);
      if (b) b.disabled = busy;
    }
  }

  async function buildApiMessages(thread) {
    const sysPrompt = (document.getElementById("ck-chat-system")?.value || "").trim();
    const out = [];
    if (sysPrompt) out.push({ role: "system", content: sysPrompt });
    for (const m of thread.messages) {
      if (m.role === "user") {
        const validMedia = (m.attachments || []).filter((a) => a.url && a.url !== "[inline-media-omitted]");
        if (validMedia.length) {
          const parts = [{ type: "text", text: m.content || "" }];
          for (const a of validMedia) {
            if (a.kind === "audio") parts.push({ type: "audio_url", audio_url: { url: a.url } });
            else parts.push({ type: "image_url", image_url: { url: a.url } });
          }
          out.push({ role: "user", content: parts });
        } else {
          out.push({ role: "user", content: m.content });
        }
      } else if (m.role === "assistant") {
        const msgObj = { role: "assistant", content: m.content || "" };
        if (m.toolCalls?.length) {
          msgObj.tool_calls = m.toolCalls.map((tc) => ({
            id: tc.id,
            type: "function",
            function: tc.function,
          }));
        }
        out.push(msgObj);
      } else if (m.role === "tool") {
        out.push({ role: "tool", tool_call_id: m.toolCallId, name: m.name, content: m.content });
      }
    }
    return out;
  }

  async function sendChatPrompt(rawPrompt, { fromDrawer = false } = {}) {
    if (ckState.streaming) return;
    const text = String(rawPrompt || "").trim();
    const attachments = ckState.attachments.slice();
    if (!text && !attachments.length) return;

    const target = getSelectedTarget();
    if (!target || !target.url) {
      toast("请先选择一个已有公网地址的在线实例", "warn");
      return;
    }

    const thread = getActiveThread();
    thread.targetSession = target.session_name;
    const modelId = getSelectedModel() || getSelectedProfile()?.default_model || target.service;
    thread.model = modelId;
    if (thread.title === "新对话" && text) {
      thread.title = text.slice(0, 24);
    }

    thread.messages.push({
      id: makeId("msg"),
      role: "user",
      content: text,
      attachments,
      timestamp: Date.now(),
    });
    ckState.attachments = [];
    renderAttachmentsPreview();

    const mainInput = document.getElementById("ck-chat-input");
    const drawerInput = document.getElementById("ck-drawer-input");
    if (mainInput && !fromDrawer) mainInput.value = "";
    if (drawerInput && fromDrawer) drawerInput.value = "";

    thread.updatedAt = Date.now();
    renderThreadsList();
    renderAllFeeds();

    const endpoint = document.getElementById("ck-chat-endpoint")?.value || "chat_completions";
    const stream = Boolean(document.getElementById("ck-chat-stream")?.checked ?? true);
    const thinking = Boolean(document.getElementById("ck-chat-thinking")?.checked ?? true);
    const effort = (document.getElementById("ck-chat-reasoning-effort")?.value || "").trim();
    const budgetRaw = (document.getElementById("ck-chat-reasoning-budget")?.value || "").trim();
    const temp = Number(document.getElementById("ck-chat-temp")?.value ?? 0.7);
    const maxTokens = Number(document.getElementById("ck-chat-max-tokens")?.value ?? 2048);
    const toolsEnabled = fromDrawer
      ? Boolean(document.getElementById("ck-drawer-tools")?.checked)
      : Boolean(document.getElementById("ck-tools-enable")?.checked);
    const autoExecTools = Boolean(document.getElementById("ck-tools-auto-exec")?.checked);

    ckState.abortController = new AbortController();
    setChatBusy(true);

    try {
      for (let hop = 0; hop < 5; hop += 1) {
        const apiMessages = await buildApiMessages(thread);
        const assistantMsg = {
          id: makeId("msg"),
          role: "assistant",
          model: modelId,
          content: "",
          reasoning: "",
          isStreaming: true,
          isStreamingReasoning: false,
          toolCalls: [],
          uiWidgets: [],
          timestamp: Date.now(),
        };
        thread.messages.push(assistantMsg);
        renderAllFeeds();

        let subpath = "v1/chat/completions";
        let payload;

        if (endpoint === "responses") {
          subpath = "v1/responses";
          payload = { model: modelId, input: apiMessages, stream: false };
        } else if (endpoint === "completions") {
          subpath = "v1/completions";
          payload = { model: modelId, prompt: text, max_tokens: maxTokens, temperature: temp, stream: false };
        } else if (endpoint === "anthropic_messages") {
          subpath = "v1/messages";
          payload = {
            model: modelId,
            max_tokens: maxTokens,
            messages: apiMessages.filter((m) => m.role !== "system"),
            stream: false,
          };
          const sysMsg = apiMessages.find((m) => m.role === "system");
          if (sysMsg) payload.system = sysMsg.content;
        } else if (endpoint === "ollama_chat") {
          subpath = "api/chat";
          payload = { model: modelId, messages: apiMessages, stream: true };
        } else {
          subpath = "v1/chat/completions";
          payload = {
            model: modelId,
            messages: apiMessages,
            stream,
            temperature: temp,
            max_tokens: maxTokens,
            chat_template_kwargs: { enable_thinking: thinking },
          };
          if (effort) payload.reasoning_effort = effort;
          if (budgetRaw) payload.reasoning_budget_tokens = Number(budgetRaw);
          if (toolsEnabled) payload.tools = CLIENT_TOOLS_META.map((t) => t.schema);
        }

        const resp = await proxyFetch(target.session_name, subpath, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal: ckState.abortController.signal,
        });

        if (!resp.ok) {
          const errText = await resp.text();
          throw new Error(errText || `HTTP ${resp.status}`);
        }

        const ct = (resp.headers.get("Content-Type") || "").toLowerCase();
        if (ct.includes("text/event-stream") && resp.body) {
          const reader = resp.body.getReader();
          const dec = new TextDecoder();
          let buf = "";
          const tcMap = new Map();

          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            const lines = buf.split("\n");
            buf = lines.pop() || "";
            for (const rawLine of lines) {
              const line = rawLine.trim();
              if (!line.startsWith("data:")) continue;
              const dataStr = line.slice(5).trim();
              if (dataStr === "[DONE]") break;
              try {
                const evt = JSON.parse(dataStr);
                const choice = evt.choices?.[0];
                const delta = choice?.delta || {};
                const rDelta = delta.reasoning_content || delta.reasoning || "";
                if (rDelta) {
                  assistantMsg.reasoning += rDelta;
                  assistantMsg.isStreamingReasoning = true;
                }
                if (delta.content) {
                  assistantMsg.content += delta.content;
                  assistantMsg.isStreamingReasoning = false;
                }
                if (Array.isArray(delta.tool_calls)) {
                  for (const dtc of delta.tool_calls) {
                    const idx = dtc.index ?? 0;
                    if (!tcMap.has(idx)) {
                      tcMap.set(idx, {
                        id: dtc.id || makeId("tc"),
                        type: "function",
                        function: { name: dtc.function?.name || "", arguments: dtc.function?.arguments || "" },
                        status: "pending",
                      });
                    } else {
                      const cur = tcMap.get(idx);
                      if (dtc.id) cur.id = dtc.id;
                      if (dtc.function?.name) cur.function.name += dtc.function.name;
                      if (dtc.function?.arguments) cur.function.arguments += dtc.function.arguments;
                    }
                  }
                  assistantMsg.toolCalls = Array.from(tcMap.values());
                }
              } catch {
                // Ignore partial SSE comment/keepalive chunk
              }
            }
            renderAllFeeds();
          }
        } else if (ct.includes("application/x-ndjson") && resp.body) {
          const reader = resp.body.getReader();
          const dec = new TextDecoder();
          let buf = "";
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            const lines = buf.split("\n");
            buf = lines.pop() || "";
            for (const line of lines) {
              if (!line.trim()) continue;
              try {
                const evt = JSON.parse(line);
                if (evt.message?.thinking) {
                  assistantMsg.reasoning += evt.message.thinking;
                  assistantMsg.isStreamingReasoning = true;
                }
                if (evt.message?.content) {
                  assistantMsg.content += evt.message.content;
                  assistantMsg.isStreamingReasoning = false;
                }
              } catch {}
            }
            renderAllFeeds();
          }
        } else {
          const data = await resp.json();
          if (endpoint === "responses") {
            assistantMsg.content = data.output_text || JSON.stringify(data.output || data, null, 2);
          } else if (endpoint === "completions") {
            assistantMsg.content = data.choices?.[0]?.text || JSON.stringify(data, null, 2);
          } else if (endpoint === "anthropic_messages") {
            assistantMsg.content = (data.content || []).map((c) => c.text || "").join("\n") || JSON.stringify(data, null, 2);
          } else {
            const msg = data.choices?.[0]?.message || data.message || {};
            assistantMsg.reasoning = msg.reasoning_content || msg.reasoning || "";
            assistantMsg.content = msg.content || "";
            if (Array.isArray(msg.tool_calls)) {
              assistantMsg.toolCalls = msg.tool_calls.map((tc) => ({
                ...tc,
                status: "pending",
              }));
            }
          }
        }

        assistantMsg.isStreaming = false;
        assistantMsg.isStreamingReasoning = false;
        renderAllFeeds();

        // Execute frontend Generative UI tool calls if present
        if (!assistantMsg.toolCalls?.length) {
          break;
        }

        for (const tc of assistantMsg.toolCalls) {
          const toolMeta = CLIENT_TOOLS_META.find((t) => t.name === tc.function?.name);
          const shouldAutoRun = autoExecTools && (toolMeta?.safeReadOnly ?? true);
          let approved = shouldAutoRun;
          if (!shouldAutoRun) {
            tc.status = "awaiting_approval";
            renderAllFeeds();
            approved = await new Promise((resolve) => {
              tc._onApprove = resolve;
              renderAllFeeds();
            });
          }

          if (!approved) {
            tc.status = "rejected";
            tc.resultText = JSON.stringify({ error: "User rejected tool execution" });
            thread.messages.push({
              id: makeId("msg"),
              role: "tool",
              toolCallId: tc.id,
              name: tc.function?.name,
              content: tc.resultText,
            });
            continue;
          }

          tc.status = "running";
          renderAllFeeds();
          let args = {};
          try {
            args = JSON.parse(tc.function?.arguments || "{}");
          } catch {}
          const result = await executeClientTool(tc.function?.name, args);
          tc.status = result?.error ? "error" : "done";
          if (result?.widget) {
            assistantMsg.uiWidgets.push(result.widget);
          }
          const sanitizedResult = { ...result };
          if (sanitizedResult.widget?.url?.startsWith("data:")) {
            sanitizedResult.widget = { ...sanitizedResult.widget, url: "[rendered-inline-in-ui]" };
          }
          tc.resultText = JSON.stringify(sanitizedResult, null, 2);
          thread.messages.push({
            id: makeId("msg"),
            role: "tool",
            toolCallId: tc.id,
            name: tc.function?.name,
            content: tc.resultText,
          });
        }
        saveThreads();
        renderAllFeeds();
      }
    } catch (err) {
      if (err.name === "AbortError") {
        toast("已中止生成", "warn");
      } else {
        const cur = thread.messages[thread.messages.length - 1];
        if (cur && cur.role === "assistant") {
          cur.isStreaming = false;
          cur.content = cur.content || `请求失败：${err.message}`;
        }
        toast(err.message, "error");
      }
    } finally {
      ckState.abortController = null;
      setChatBusy(false);
      saveThreads();
      renderThreadsList();
      renderAllFeeds();
    }
  }

  /* ---------- UI Sync, Dynamic Capability Tabs & Code Export ---------- */
  function activateCopilotSurface(surfaceId) {
    ckState.activeSurface = surfaceId;
    const panelId = `ck-panel-${surfaceId}`;
    const tablist = document.getElementById("ck-surface-tablist");
    const shell = tablist?.closest(".ck-tabs-shell");
    if (!shell || !tablist) return;
    let selectedBtn = null;
    for (const btn of tablist.querySelectorAll(":scope > .tab-btn")) {
      const match = btn.dataset.panel === panelId;
      btn.setAttribute("aria-selected", match ? "true" : "false");
      if (match) {
        selectedBtn = btn;
        btn.setAttribute("focusgroupstart", "");
      } else {
        btn.removeAttribute("focusgroupstart");
      }
      if (!Object.hasOwn(HTMLElement.prototype, "focusGroup")) {
        btn.tabIndex = match ? 0 : -1;
      }
    }
    if (selectedBtn && tablist.scrollWidth > tablist.clientWidth) {
      selectedBtn.scrollIntoView?.({ block: "nearest", inline: "nearest", container: "nearest" });
    }
    for (const panel of shell.querySelectorAll(':scope > [role="tabpanel"]')) {
      if (panel.id === panelId) {
        panel.removeAttribute("hidden");
      } else {
        panel.setAttribute("hidden", "until-found");
      }
    }
    updateCodeSnippet();
  }

  function updateCapabilityUI() {
    const inst = getSelectedTarget();
    const svcMeta = getSelectedServiceMeta();
    const profile = svcMeta?.api_profile;
    const statusPill = document.getElementById("ck-status-pill");
    const proxyHint = document.getElementById("ck-proxy-url-hint");
    const capSummary = document.getElementById("ck-capabilities-summary");
    const welcomeTitle = document.getElementById("ck-welcome-title");
    const drawerModelBadge = document.getElementById("ck-drawer-model-badge");

    if (inst) {
      if (statusPill) {
        statusPill.textContent = `${inst.session_name} · ${inst.state_label || "未知"}`;
        statusPill.className = `badge ${inst.ready ? "ok" : inst.url ? "warn" : "neutral"}`;
      }
      if (proxyHint) {
        proxyHint.textContent = inst.url ? `同源代理 -> ${proxyUrl(inst.session_name, "")}` : "等待公网地址 (url)";
      }
    } else if (svcMeta) {
      if (statusPill) {
        statusPill.textContent = `${svcMeta.name} (未启动实例)`;
        statusPill.className = "badge neutral";
      }
      if (proxyHint) proxyHint.textContent = "在「舰队与实例」启动后可通过本机同源代理直接调用";
    } else {
      if (statusPill) {
        statusPill.textContent = "暂无运行中实例";
        statusPill.className = "badge neutral";
      }
      if (proxyHint) {
        proxyHint.textContent = "请先在「舰队与实例」启动运行时实例";
      }
    }

    const feats = profile?.features || {};
    if (capSummary) {
      const caps = [];
      if (profile?.openai_compatible) caps.push("OpenAI /v1 兼容");
      else caps.push("原生专用 API");
      if (feats.thinking) caps.push("Reasoning 思考流");
      if (feats.tools) caps.push("Generative UI Tool Calls");
      if (feats.vision) caps.push("Vision 多模态");
      if (feats.audio_input) caps.push("Audio 音频理解");
      if (feats.async_jobs) caps.push("异步任务队列");
      if (feats.auto_discovered) caps.push("自动发现运行时");
      capSummary.textContent = `${svcMeta?.name || "未选择"} · ${caps.join(" · ")}`;
    }
    if (welcomeTitle) {
      welcomeTitle.textContent = svcMeta ? `${svcMeta.name} · Copilot Studio` : "ColabLLM Copilot Studio";
    }
    if (drawerModelBadge) {
      drawerModelBadge.textContent = getSelectedModel() || profile?.default_model || svcMeta?.name || "auto";
    }

    // Update starter suggestions inside main view and drawer
    const suggestions = profile?.suggestions || [
      "列出当前所有运行中实例并生成概览卡片",
      "用表格对比 LLM 与多模态运行时 API 能力",
    ];
    for (const cid of ["ck-suggestions", "ck-drawer-suggestions"]) {
      const box = document.getElementById(cid);
      if (!box) continue;
      box.replaceChildren(
        ...suggestions.map((text) => {
          const btn = el("button", { type: "button", class: "ck-suggestion-pill" }, text);
          btn.addEventListener("click", () => sendChatPrompt(text, { fromDrawer: cid === "ck-drawer-suggestions" }));
          return btn;
        })
      );
    }

    // Update protocol options inside Chat toolbar
    const epSelect = document.getElementById("ck-chat-endpoint");
    if (epSelect) {
      for (const opt of epSelect.options) {
        if (opt.value === "responses") opt.hidden = !feats.responses_api && !ckState.showAllSurfaces;
        if (opt.value === "ollama_chat") opt.hidden = !feats.ollama_native && !ckState.showAllSurfaces;
        if (opt.value === "completions") opt.hidden = !feats.completions_api && !ckState.showAllSurfaces;
        if (opt.value === "anthropic_messages") opt.hidden = !feats.anthropic_messages && !ckState.showAllSurfaces;
      }
      if (epSelect.selectedOptions[0]?.hidden) {
        epSelect.value = "chat_completions";
      }
    }

    // Filter capability tabs unless Show All Surfaces is enabled
    const allowed = new Set(profile?.surfaces || ["chat", "inspector"]);
    const tablist = document.getElementById("ck-surface-tablist");
    if (tablist) {
      for (const btn of tablist.querySelectorAll(":scope > .tab-btn")) {
        const surf = btn.dataset.surface;
        const visible = ckState.showAllSurfaces || allowed.has(surf);
        btn.hidden = !visible;
        if (visible) {
          btn.removeAttribute("focusgroup");
        } else {
          // Exclude hidden tabs cleanly from roving focusgroup navigation
          btn.setAttribute("focusgroup", "none");
        }
      }
      if (!ckState.showAllSurfaces && !allowed.has(ckState.activeSurface)) {
        activateCopilotSurface(profile?.primary_surface || "chat");
      }
    }

    ckState.studios?.syncTarget(inst, svcMeta);
    updateCodeSnippet();
  }

  function renderAttachmentsPreview() {
    const box = document.getElementById("ck-attachments-preview");
    if (!box) return;
    if (!ckState.attachments.length) {
      box.hidden = true;
      box.replaceChildren();
      return;
    }
    box.hidden = false;
    box.replaceChildren(
      ...ckState.attachments.map((att, idx) => {
        const rm = el("button", { type: "button", class: "btn icon-btn", "aria-label": "移除附件" }, "✕");
        rm.addEventListener("click", () => {
          ckState.attachments.splice(idx, 1);
          renderAttachmentsPreview();
        });
        return el("span", { class: "ck-attach-chip" }, `${att.kind === "audio" ? "音频" : "图片"}: ${att.name}`, rm);
      })
    );
  }

  function updateCodeSnippet() {
    const codeOut = document.getElementById("ck-code-output");
    if (!codeOut) return;
    const lang = document.getElementById("ck-code-lang")?.value || "curl-proxy";
    const inst = getSelectedTarget();
    const svc = getSelectedServiceMeta();
    const model = getSelectedModel() || svc?.api_profile?.default_model || svc?.name || "model";
    const baseProxy = inst ? `${window.location.origin}${proxyUrl(inst.session_name, "v1")}` : `${window.location.origin}/api/proxy/<service>/<instance>/v1`;
    const baseDirect = inst?.url ? `${inst.url.replace(/\/+$/, "")}/v1` : "https://<your-runtime-domain>/v1";

    if (lang === "python-openai") {
      codeOut.textContent = [
        "from openai import OpenAI",
        "",
        `client = OpenAI(base_url="${baseDirect}", api_key="<YOUR_API_TOKEN>")`,
        "resp = client.chat.completions.create(",
        `    model="${model}",`,
        '    messages=[{"role": "user", "content": "Hello ColabLLM!"}],',
        "    stream=True,",
        ")",
        "for chunk in resp:",
        "    print(chunk.choices[0].delta.content or '', end='')",
      ].join("\n");
    } else if (lang === "curl-direct") {
      codeOut.textContent = [
        `curl -N "${baseDirect}/chat/completions" \\`,
        '  -H "Authorization: Bearer <YOUR_API_TOKEN>" \\',
        '  -H "Content-Type: application/json" \\',
        `  -d '{"model":"${model}","messages":[{"role":"user","content":"Hello"}],"stream":true}'`,
      ].join("\n");
    } else {
      codeOut.textContent = [
        "# 浏览器同源安全代理（自动补全 Authorization，浏览器端不存密钥）",
        `curl -N "${baseProxy}/chat/completions" \\`,
        '  -H "Cookie: <SESSION_COOKIE>" \\',
        '  -H "X-ColabLLM-CSRF: <CSRF_TOKEN>" \\',
        '  -H "Content-Type: application/json" \\',
        `  -d '{"model":"${model}","messages":[{"role":"user","content":"Hello"}],"stream":true}'`,
      ].join("\n");
    }
  }

  /* ---------- Slide-out CopilotSidebar Drawer (Swipe-to-Dismiss) ---------- */
  function openDrawer(sessionName = "") {
    if (sessionName && getRunningInstances().some((i) => i.session_name === sessionName)) {
      selectSession(sessionName);
    }
    const drawer = document.getElementById("copilot-drawer");
    if (!drawer || drawer.open) return;
    ckState.drawerHasEntered = false;
    drawer.style.setProperty("--drawer-backdrop", "0.45");
    drawer.showModal();
    requestAnimationFrame(() => {
      drawer.scrollTo({ left: drawer.scrollWidth, behavior: "instant" });
      drawer.querySelector(".ck-drawer-panel")?.focus({ preventScroll: true });
    });
  }

  function closeDrawer() {
    const drawer = document.getElementById("copilot-drawer");
    if (!drawer || !drawer.open) return;
    drawer.scrollTo({ left: 0, behavior: "smooth" });
    setTimeout(() => {
      if (drawer.open) drawer.close();
    }, 260);
  }

  function selectSession(sessionName) {
    if (!sessionName) return;
    const nextCandidate = (appState.instances || []).find(
      (i) => i.session_name === sessionName && isRunningInstance(i)
    );
    if (!nextCandidate) return;
    const prevSvcName = getSelectedServiceMeta()?.name || "";
    ckState.selectedSession = sessionName;
    ckState.selectedServiceFallback = "";
    for (const selId of ["ck-target", "ck-drawer-target"]) {
      const sel = document.getElementById(selId);
      if (!sel) continue;
      sel.querySelector('option[value=""]')?.remove();
      if (sel.value !== sessionName) sel.value = sessionName;
    }
    const modelIn = document.getElementById("ck-model");
    if (modelIn && (!modelIn.value.trim() || prevSvcName !== nextCandidate.service)) {
      const nextSvc = (appState.services || []).find((s) => s.name === nextCandidate.service);
      modelIn.value = nextSvc?.api_profile?.default_model || nextCandidate.service || "";
    }
    updateCapabilityUI();
  }

  function syncCopilotState() {
    const prevSession = ckState.selectedSession;
    const prevSvcName =
      (appState.instances || []).find((i) => i.session_name === prevSession)?.service ||
      ckState.selectedServiceFallback ||
      "";
    const usable = getRunningInstances();

    if (ckState.selectedSession && !usable.some((i) => i.session_name === ckState.selectedSession)) {
      ckState.selectedSession = "";
    }
    if (!ckState.selectedSession && ckState.selectedServiceFallback) {
      const fallbackRunning = usable.find((i) => i.service === ckState.selectedServiceFallback);
      if (fallbackRunning) {
        ckState.selectedSession = fallbackRunning.session_name;
        ckState.selectedServiceFallback = "";
      }
    }
    if (!ckState.selectedSession && !ckState.selectedServiceFallback && usable.length) {
      ckState.selectedSession = usable[0].session_name;
    }

    for (const selId of ["ck-target", "ck-drawer-target"]) {
      const sel = document.getElementById(selId);
      if (!sel) continue;
      if (!usable.length) {
        sel.replaceChildren(el("option", { value: "" }, "暂无运行中实例"));
      } else {
        const opts = [];
        if (!ckState.selectedSession) {
          opts.push(el("option", { value: "", disabled: true, hidden: true, selected: true }, "请选择运行中实例"));
        }
        for (const i of usable) {
          opts.push(
            el(
              "option",
              { value: i.session_name },
              `${i.session_name} (${i.state_label}${i.url ? ` · ${i.url}` : " · 无URL"})`
            )
          );
        }
        sel.replaceChildren(...opts);
        sel.value = ckState.selectedSession || "";
      }
    }

    const modelIn = document.getElementById("ck-model");
    if (modelIn) {
      const nextSvcName = getSelectedServiceMeta()?.name || "";
      if (!nextSvcName) {
        if (prevSession && !ckState.selectedSession) {
          modelIn.value = "";
        }
      } else if (!modelIn.value.trim() || (prevSession !== ckState.selectedSession && prevSvcName !== nextSvcName)) {
        const prof = getSelectedProfile();
        modelIn.value = prof?.default_model || nextSvcName;
      }
    }

    const launchEmptyBtn = document.getElementById("ck-launch-empty-btn");
    if (launchEmptyBtn) {
      launchEmptyBtn.hidden = usable.length > 0;
    }

    updateCapabilityUI();
  }

  function applyRouteParams({ session = "", service = "", surface = "" } = {}) {
    const usable = getRunningInstances();
    if (session && usable.some((i) => i.session_name === session)) {
      selectSession(session);
    } else if (service) {
      const match =
        usable.find((i) => i.service === service && i.ready && i.url) ||
        usable.find((i) => i.service === service && i.url) ||
        usable.find((i) => i.service === service);
      if (match) {
        selectSession(match.session_name);
      } else if ((appState.services || []).some((s) => s.name === service)) {
        ckState.selectedSession = "";
        ckState.selectedServiceFallback = service;
        const svc = (appState.services || []).find((s) => s.name === service);
        const modelIn = document.getElementById("ck-model");
        if (modelIn && svc) {
          modelIn.value = svc.api_profile?.default_model || svc.name;
        }
        syncCopilotState();
      }
    }
    if (surface && document.getElementById(`ck-panel-${surface}`)) {
      const prof = getSelectedProfile();
      const allowed = new Set(prof?.surfaces || ["chat", "inspector"]);
      if (!ckState.showAllSurfaces && !allowed.has(surface)) {
        ckState.showAllSurfaces = true;
        const allBtn = document.getElementById("ck-all-surfaces-btn");
        if (allBtn) allBtn.setAttribute("aria-pressed", "true");
        updateCapabilityUI();
      }
      activateCopilotSurface(surface);
    }
  }

  function openInstanceInCopilot(inst, surface = "") {
    if (!inst) return;
    if (!isRunningInstance(inst)) {
      openServiceInCopilot(inst.service);
      return;
    }
    selectSession(inst.session_name);
    const prof = getSelectedProfile();
    const targetSurface = surface || prof?.primary_surface || "chat";
    activateCopilotSurface(targetSurface);
    switchView("copilot", {
      params: { session: inst.session_name, surface: targetSurface },
    });
  }

  function openServiceInCopilot(serviceName) {
    const usable = getRunningInstances();
    const match =
      usable.find((i) => i.service === serviceName && i.ready && i.url) ||
      usable.find((i) => i.service === serviceName && i.url) ||
      usable.find((i) => i.service === serviceName);
    if (match) {
      openInstanceInCopilot(match);
      return;
    }
    ckState.selectedSession = "";
    ckState.selectedServiceFallback = serviceName;
    const svc = (appState.services || []).find((s) => s.name === serviceName);
    const modelIn = document.getElementById("ck-model");
    if (modelIn && svc) {
      modelIn.value = svc.api_profile?.default_model || svc.name;
    }
    syncCopilotState();
    const targetSurface = svc?.api_profile?.primary_surface || "chat";
    activateCopilotSurface(targetSurface);
    switchView("copilot", {
      params: { service: serviceName, surface: targetSurface },
    });
  }

  /* ---------- Event Binding ---------- */
  function attachImeSubmit(textarea, form) {
    if (!textarea || !form) return;
    textarea.addEventListener("keydown", (event) => {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
  }

  function bindEvents() {
    ckState.studios = initStudios({
      el,
      toast,
      proxyUrl,
      proxyFetch,
      getCsrfToken,
      getSelectedTarget,
      getSelectedProfile,
      getSelectedModel,
      trackBlobUrl,
      onSendMediaToChat(media) {
        ckState.attachments.push(media);
        renderAttachmentsPreview();
        activateCopilotSurface("chat");
      },
    });

    // Render tool badges inside right parameter panel
    const toolsCat = document.getElementById("ck-tools-catalog");
    if (toolsCat) {
      toolsCat.replaceChildren(
        ...CLIENT_TOOLS_META.map((t) =>
          el(
            "div",
            { class: "ck-tool-badge-item" },
            el("strong", {}, t.label),
            el("span", { class: "muted ck-mono" }, `${t.name}${t.safeReadOnly ? " (只读/UI)" : " (写/推理调用)"}`)
          )
        )
      );
    }

    // Target selectors
    for (const selId of ["ck-target", "ck-drawer-target"]) {
      document.getElementById(selId)?.addEventListener("change", (e) => {
        selectSession(e.target.value);
      });
    }
    document.getElementById("ck-model")?.addEventListener("input", () => {
      updateCodeSnippet();
      const b = document.getElementById("ck-drawer-model-badge");
      if (b) b.textContent = getSelectedModel() || "auto";
    });

    // Show all capability tabs toggle
    const allBtn = document.getElementById("ck-all-surfaces-btn");
    allBtn?.addEventListener("click", () => {
      ckState.showAllSurfaces = !ckState.showAllSurfaces;
      allBtn.setAttribute("aria-pressed", ckState.showAllSurfaces ? "true" : "false");
      updateCapabilityUI();
    });

    // Capability tabs
    document.getElementById("ck-surface-tablist")?.addEventListener("click", (e) => {
      const btn = e.target.closest(".tab-btn");
      if (!btn || btn.hidden) return;
      activateCopilotSurface(btn.dataset.surface);
    });

    // Probe /v1/models & health
    document.getElementById("ck-probe-btn")?.addEventListener("click", async () => {
      try {
        const tgt = getSelectedTarget();
        if (!tgt?.url) return toast("请选择已有公网地址的实例", "warn");
        const path = getSelectedProfile()?.models_path || "v1/models";
        const resp = await proxyFetch(tgt.session_name, path);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || resp.statusText);
        const list = (data.data || data.models || []).map((m) => (typeof m === "string" ? m : m.id || m.name)).filter(Boolean);
        const dl = document.getElementById("ck-model-options");
        if (dl) dl.replaceChildren(...list.map((m) => el("option", { value: m })));
        if (list.length) {
          const modelIn = document.getElementById("ck-model");
          if (modelIn) modelIn.value = list[0];
          toast(`探测到 ${list.length} 个模型：${list.slice(0, 3).join(", ")}`, "ok");
        } else {
          toast("端点可达，但未返回模型条目", "warn");
        }
        updateCodeSnippet();
      } catch (err) {
        toast(`探测失败：${err.message}`, "error");
      }
    });

    document.getElementById("ck-health-btn")?.addEventListener("click", async () => {
      try {
        const tgt = getSelectedTarget();
        if (!tgt?.url) return toast("请选择已有公网地址的实例", "warn");
        const path = getSelectedProfile()?.health_path || "health";
        const resp = await proxyFetch(tgt.session_name, path);
        const txt = await resp.text();
        toast(`健康探测 ${path} -> HTTP ${resp.status} (${txt.slice(0, 80)})`, resp.ok ? "ok" : "warn");
      } catch (err) {
        toast(err.message, "error");
      }
    });

    // Code export popover
    document.getElementById("ck-code-lang")?.addEventListener("change", updateCodeSnippet);
    document.getElementById("ck-code-copy")?.addEventListener("click", () => {
      const code = document.getElementById("ck-code-output")?.textContent || "";
      copyText(code, "已复制代码片段");
    });

    // Pane toggles & threads CRUD
    const ws = document.querySelector(".ck-chat-workspace");
    document.getElementById("ck-toggle-threads-btn")?.addEventListener("click", (e) => {
      const hidden = ws?.classList.toggle("hide-threads");
      e.currentTarget.setAttribute("aria-expanded", hidden ? "false" : "true");
    });
    document.getElementById("ck-toggle-params-btn")?.addEventListener("click", (e) => {
      const hidden = ws?.classList.toggle("hide-params");
      e.currentTarget.setAttribute("aria-expanded", hidden ? "false" : "true");
    });
    document.getElementById("ck-new-thread-btn")?.addEventListener("click", () => createThread(true));
    document.getElementById("ck-thread-search")?.addEventListener("input", renderThreadsList);
    document.getElementById("ck-clear-thread-btn")?.addEventListener("click", () => {
      const t = getActiveThread();
      t.messages = [];
      t.title = "新对话";
      saveThreads();
      renderThreadsList();
      renderAllFeeds();
    });
    document.getElementById("ck-export-thread-btn")?.addEventListener("click", () => {
      const t = getActiveThread();
      const blob = new Blob([JSON.stringify(t, null, 2)], { type: "application/json" });
      const u = trackBlobUrl(URL.createObjectURL(blob));
      const a = el("a", { href: u, download: `${t.id}.json` });
      document.body.appendChild(a);
      a.click();
      a.remove();
    });

    // Attachments & Paste
    document.getElementById("ck-attach-file")?.addEventListener("change", async (e) => {
      for (const file of Array.from(e.target.files || [])) {
        const url = await fileToDataUrl(file);
        ckState.attachments.push({ kind: file.type.startsWith("audio/") ? "audio" : "image", name: file.name, url });
      }
      e.target.value = "";
      renderAttachmentsPreview();
    });
    document.getElementById("ck-chat-input")?.addEventListener("paste", async (e) => {
      const items = Array.from(e.clipboardData?.items || []);
      for (const item of items) {
        if (item.kind === "file" && (item.type.startsWith("image/") || item.type.startsWith("audio/"))) {
          const f = item.getAsFile();
          if (f) {
            const url = await fileToDataUrl(f);
            ckState.attachments.push({ kind: item.type.startsWith("audio/") ? "audio" : "image", name: f.name || "pasted-media", url });
            renderAttachmentsPreview();
          }
        }
      }
    });

    // Drive quick picker
    document.getElementById("ck-drive-refresh")?.addEventListener("click", async () => {
      const ul = document.getElementById("ck-drive-list");
      try {
        const tgt = getSelectedTarget();
        if (!tgt?.url) return toast("请先选择在线实例", "warn");
        const isCt = getSelectedProfile()?.features?.drive_io || tgt.service === "nemotron-transcribe";
        const resp = isCt
          ? await proxyFetch(tgt.session_name, tgt.service === "longcat-avatar" ? "drive/files" : "api/ct/drive-list")
          : await proxyFetch(tgt.session_name, "api/drive-list", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: "{}",
            });
        const data = await resp.json();
        const files = Array.isArray(data) ? data : data.files || data.items || data.entries || [];
        ul?.replaceChildren(
          ...(files.length
            ? files.slice(0, 40).map((f) => {
                const pathStr = typeof f === "string" ? f : f.drive_path || f.path || f.name;
                const b = el("button", { type: "button", class: "btn small secondary ck-drive-item-btn" }, pathStr);
                b.addEventListener("click", () => {
                  const val = String(pathStr).startsWith("drive://") ? pathStr : `drive://${String(pathStr).replace(/^\/+/, "")}`;
                  const inp = document.getElementById("ck-chat-input");
                  if (inp) inp.value = `${inp.value}${inp.value ? " " : ""}${val}`;
                  document.getElementById("ck-drive-popover")?.hidePopover?.();
                });
                return el("li", {}, b);
              })
            : [el("li", { class: "muted" }, "Drive 目录为空或不支持列举")])
        );
      } catch (err) {
        ul?.replaceChildren(el("li", { class: "muted" }, `读取失败: ${err.message}`));
      }
    });

    // Main chat & Drawer chat forms (IME-safe Enter submit + Stop)
    const chatForm = document.getElementById("ck-chat-form");
    const chatInput = document.getElementById("ck-chat-input");
    attachImeSubmit(chatInput, chatForm);
    chatForm?.addEventListener("submit", (e) => {
      e.preventDefault();
      sendChatPrompt(chatInput?.value || "", { fromDrawer: false });
    });

    const drawerForm = document.getElementById("ck-drawer-form");
    const drawerInput = document.getElementById("ck-drawer-input");
    attachImeSubmit(drawerInput, drawerForm);
    drawerForm?.addEventListener("submit", (e) => {
      e.preventDefault();
      sendChatPrompt(drawerInput?.value || "", { fromDrawer: true });
    });

    for (const stopId of ["ck-chat-stop", "ck-drawer-stop"]) {
      document.getElementById(stopId)?.addEventListener("click", () => ckState.abortController?.abort());
    }

    // Slide-out CopilotSidebar Drawer controls + IntersectionObserver swipe-to-dismiss
    const drawer = document.getElementById("copilot-drawer");
    const panel = drawer?.querySelector(".ck-drawer-panel");
    const scrim = drawer?.querySelector(".ck-drawer-scrim");
    document.getElementById("copilot-sidebar-btn")?.addEventListener("click", () => openDrawer());
    document.getElementById("ck-drawer-close-btn")?.addEventListener("click", closeDrawer);
    scrim?.addEventListener("click", closeDrawer);
    document.getElementById("ck-drawer-expand-btn")?.addEventListener("click", () => {
      closeDrawer();
      switchView("copilot");
    });

    if (drawer && panel && typeof IntersectionObserver !== "undefined") {
      const io = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (!drawer.open) continue;
            if (entry.intersectionRatio >= 0.85) {
              ckState.drawerHasEntered = true;
            } else if (ckState.drawerHasEntered && entry.intersectionRatio === 0) {
              drawer.close();
            }
          }
        },
        { root: drawer, threshold: [0, 0.85] }
      );
      io.observe(panel);
    }

    drawer?.addEventListener("scroll", () => {
      const maxScroll = Math.max(1, drawer.scrollWidth - drawer.clientWidth);
      const ratio = Math.min(1, Math.max(0, drawer.scrollLeft / maxScroll));
      drawer.style.setProperty("--drawer-backdrop", (ratio * 0.45).toFixed(3));
    });
  }

  loadThreads();
  bindEvents();
  renderThreadsList();
  renderAllFeeds();
  syncCopilotState();

  return {
    syncCopilotState,
    applyRouteParams,
    openInstanceInCopilot,
    openServiceInCopilot,
    openDrawer,
  };
}
