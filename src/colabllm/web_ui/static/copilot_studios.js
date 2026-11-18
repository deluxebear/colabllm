/**
 * Multimodal & Runtime-Native API Studios for ColabLLM Copilot Studio:
 * - Image Studio (/v1/images/*, PUP, Async 4K jobs, Iterative Editing)
 * - Audio Studio (/v1/audio/speech, batch dialogue, voice clones, SFX generations)
 * - Transcribe Studio (SALM /v1/chat/completions + live-synced CTC timestamps, /v1/audio/transcriptions, SRT/TXT export)
 * - Embeddings Studio (/v1/embeddings + RAG Top-K Ranking + Heatmap Cosine Similarity Matrix)
 * - Specialized Native Consoles (Kaggle web-probe, Ollama, ComfyUI, Unlimited-OCR, R2-Migrate, Unsloth, OpenJutsu, LongCat)
 * - Universal Same-Origin HTTP Inspector (GET/HEAD/POST/PUT/DELETE, JSON/Multipart/SSE/NDJSON/Binary, Headers, History)
 */

const EMO_LABELS = [
  ["happy", "喜悦 (happy)"],
  ["angry", "愤怒 (angry)"],
  ["sad", "悲伤 (sad)"],
  ["afraid", "恐惧 (afraid)"],
  ["disgusted", "厌恶 (disgusted)"],
  ["melancholic", "忧郁 (melancholic)"],
  ["surprised", "惊讶 (surprised)"],
  ["calm", "平静 (calm)"],
];

const EMO_PRESETS = [
  { label: "喜悦活力", alpha: 0.9, vec: [0.85, 0, 0, 0, 0, 0, 0.25, 0] },
  { label: "沉稳播报", alpha: 0.7, vec: [0, 0, 0, 0, 0, 0, 0, 0.9] },
  { label: "深沉忧郁", alpha: 0.85, vec: [0, 0, 0.55, 0, 0, 0.8, 0, 0] },
  { label: "惊讶戏剧", alpha: 0.9, vec: [0.2, 0.25, 0, 0, 0, 0, 0.9, 0] },
  { label: "清零重置", alpha: 0.8, vec: [0, 0, 0, 0, 0, 0, 0, 0] },
];

const ASR_PROMPT_PRESETS = [
  { label: "逐字精准转写", prompt: "Transcribe the audio accurately word for word with punctuation." },
  { label: "会议纪要与行动项", prompt: "Listen to the audio and summarize key decisions and action items in bullet points." },
  { label: "中英双语翻译", prompt: "Transcribe the audio and translate it accurately into Simplified Chinese and English." },
  { label: "核心观点提炼", prompt: "Summarize the speaker's main thesis and supporting evidence concisely." },
];

const EMB_PRESETS = [
  {
    label: "中英跨语种对齐",
    text: "ColabLLM 本地同源代理自动注入认证令牌\nSame-origin reverse proxy protects API tokens\n今天天气适合在公园散步",
  },
  {
    label: "RAG 检索排序（首行为 Query）",
    text: "如何在 Kaggle 无 Google Drive 环境下验证同源 HTTP 代理连通性？\nKaggle web-probe 提供纯标准库 HTTP /health 与状态网页用于端到端隧道探测。\nFlux Klein 支持最多 10 张参考图融合与 PUP 提示词扩写。\nIndexTTS 支持 8 维情绪向量与参考干声克隆。",
  },
  {
    label: "代码与文档语义匹配",
    text: "Extract word-level timestamps from speech audio using CTC\nNemotron SALM /v1/chat/completions with capture_ctc_timestamps=True\nComfyUI /prompt submits node workflow JSON for diffusion execution",
  },
];

function cosineSimilarity(a, b) {
  let dot = 0;
  let normA = 0;
  let normB = 0;
  const len = Math.min(a.length, b.length);
  for (let i = 0; i < len; i += 1) {
    dot += a[i] * b[i];
    normA += a[i] * a[i];
    normB += b[i] * b[i];
  }
  const denom = Math.sqrt(normA) * Math.sqrt(normB);
  return denom > 0 ? dot / denom : 0;
}

function arrayBufferToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("读取本地文件失败"));
    reader.readAsDataURL(file);
  });
}

function insertTokenAtCursor(textarea, token) {
  if (!textarea) return;
  const start = Number.isInteger(textarea.selectionStart) ? textarea.selectionStart : textarea.value.length;
  const end = Number.isInteger(textarea.selectionEnd) ? textarea.selectionEnd : textarea.value.length;
  const before = textarea.value.slice(0, start);
  const after = textarea.value.slice(end);
  const padBefore = before && !/\s$/.test(before) ? " " : "";
  const padAfter = after && !/^\s/.test(after) ? " " : " ";
  const inserted = `${padBefore}${token}${padAfter}`;
  textarea.value = `${before}${inserted}${after}`;
  const cursor = start + inserted.length;
  textarea.setSelectionRange?.(cursor, cursor);
  textarea.focus();
}

function formatSrtTime(secRaw) {
  const sec = Math.max(0, Number(secRaw) || 0);
  const hrs = Math.floor(sec / 3600);
  const mins = Math.floor((sec % 3600) / 60);
  const secs = Math.floor(sec % 60);
  const ms = Math.round((sec - Math.floor(sec)) * 1000);
  const p2 = (n) => String(n).padStart(2, "0");
  const p3 = (n) => String(n).padStart(3, "0");
  return `${p2(hrs)}:${p2(mins)}:${p2(secs)},${p3(ms)}`;
}

function buildSrtText(segments) {
  if (!Array.isArray(segments) || !segments.length) return "";
  return segments
    .map((seg, i) => {
      const text = String(seg.word || seg.text || seg.label || "").trim() || "?";
      const start = Number(seg.start ?? seg.start_time ?? 0);
      const end = Number(seg.end ?? seg.end_time ?? start + 0.6);
      return `${i + 1}\n${formatSrtTime(start)} --> ${formatSrtTime(Math.max(start + 0.08, end))}\n${text}\n`;
    })
    .join("\n");
}

function downloadTextFile(elFactory, trackBlobUrl, text, filename, mime = "text/plain;charset=utf-8") {
  const blob = new Blob([text], { type: mime });
  const u = trackBlobUrl(URL.createObjectURL(blob));
  const a = elFactory("a", { href: u, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function renderCodeBlock(el, title, text) {
  return el(
    "div",
    { class: "ck-code-block-wrap" },
    title ? el("div", { class: "ck-code-block-head" }, el("span", {}, title)) : null,
    el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, text || ""))
  );
}

export function initStudios(ctx) {
  const {
    el,
    toast,
    copyText,
    makeCopyButton: hostMakeCopyButton,
    syncRangeVisual: hostSyncRangeVisual,
    proxyUrl,
    proxyFetch,
    getSelectedTarget,
    getSelectedProfile,
    getSelectedModel,
    trackBlobUrl,
    onSendMediaToChat,
    onSendAudioToTranscribe,
    onInspectEndpoint,
  } = ctx;

  const studioState = {
    unslothJwt: "",
    activeTabRenderKey: "",
    imageRefFiles: [],
    imageAbortController: null,
    bridgedAsrFile: null,
    cloneSampleFile: null,
    inspectorMultipartRows: [{ key: "file", type: "file", value: "", file: null }],
    inspectorHistory: [],
    inspectorAbortController: null,
    lastTranscribedAudioUrl: "",
  };

  function makeCopyButton(getText, labelText = "复制", ariaLabel = "") {
    if (typeof hostMakeCopyButton === "function") {
      return hostMakeCopyButton(getText, labelText, ariaLabel);
    }
    const btn = el("button", { type: "button", class: "btn small secondary" }, labelText);
    btn.addEventListener("click", () => {
      const txt = typeof getText === "function" ? getText() : getText;
      copyText?.(String(txt || ""), "已复制");
    });
    return btn;
  }

  function syncSlider(sliderEl, outEl, formatFn = (v) => v) {
    if (!sliderEl) return;
    if (typeof hostSyncRangeVisual === "function") {
      hostSyncRangeVisual(sliderEl, outEl);
    } else {
      sliderEl.style?.setProperty?.("--attr-min", sliderEl.min || "0");
      sliderEl.style?.setProperty?.("--attr-max", sliderEl.max || "1");
      sliderEl.style?.setProperty?.("--control-value", sliderEl.value || "0");
      sliderEl.setAttribute("aria-valuetext", String(sliderEl.value || "0"));
    }
    if (outEl) outEl.textContent = String(formatFn(sliderEl.value));
  }

  function makeSliderField(id, labelText, { min, max, step, value, formatFn = (v) => v }) {
    const out = el("output", { for: id, class: "ck-mono" }, String(formatFn(value)));
    const range = el("input", {
      id,
      type: "range",
      class: "ck-range",
      min: String(min),
      max: String(max),
      step: String(step),
      value: String(value),
    });
    syncSlider(range, out, formatFn);
    range.addEventListener("input", () => syncSlider(range, out, formatFn));
    const wrap = el(
      "div",
      { class: "ck-field" },
      el("div", { class: "ck-slider-head" }, el("label", { for: id }, labelText), out),
      range
    );
    return { wrap, input: range, output: out, sync: () => syncSlider(range, out, formatFn) };
  }

  function makeModeGroup(name, ariaLabel, options, initialValue, onChange) {
    const group = el("div", { class: "ck-mode-group", role: "radiogroup", "aria-label": ariaLabel });
    const pills = [];
    for (const [val, label] of options) {
      const checked = val === initialValue;
      const radio = el("input", { type: "radio", name, value: val, checked: checked || undefined });
      const pill = el("label", { class: "ck-mode-pill", "data-checked": checked ? "true" : "false" }, radio, el("span", {}, label));
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        for (const p of pills) p.pill.setAttribute("data-checked", p.radio.checked ? "true" : "false");
        onChange?.(val);
      });
      pills.push({ pill, radio, val });
      group.appendChild(pill);
    }
    return {
      group,
      getValue() {
        return pills.find((p) => p.radio.checked)?.val || initialValue;
      },
      setValue(nextVal, trigger = true) {
        const match = pills.find((p) => p.val === nextVal);
        if (!match) return;
        for (const p of pills) {
          p.radio.checked = p === match;
          p.pill.setAttribute("data-checked", p.radio.checked ? "true" : "false");
        }
        if (trigger) onChange?.(nextVal);
      },
    };
  }

  function requireTarget() {
    const target = getSelectedTarget();
    if (!target) {
      throw new Error("请先在顶部选择一个目标实例");
    }
    if (!target.url) {
      throw new Error(`实例 ${target.session_name} 尚未分配公网 URL，请先等待启动完成`);
    }
    return target;
  }

  function isKaggleTarget() {
    const target = getSelectedTarget();
    const profile = getSelectedProfile();
    return target?.provider === "kaggle" || profile?.features?.provider === "kaggle";
  }

  function makeField(id, labelText, control) {
    return el("div", { class: "ck-field" }, el("label", { for: id }, labelText), control);
  }

  function makeSwitch(id, labelText, checked = false) {
    return el(
      "label",
      { class: "switch-row" },
      el("span", {}, labelText),
      el("input", { id, type: "checkbox", switch: "", checked: checked || undefined })
    );
  }

  function renderHeaderBadges(headers, elFactory, dtMs = null) {
    const badges = [];
    if (typeof dtMs === "number") {
      badges.push(elFactory("span", { class: "badge neutral ck-mono" }, `${(dtMs / 1000).toFixed(2)}s`));
    }
    for (const h of ["X-Audio-Sample-Rate", "X-Saved-Drive-Path", "X-Audio-Duration", "X-Job-Id"]) {
      const val = headers.get(h);
      if (val) {
        badges.push(elFactory("span", { class: "badge ok" }, `${h}: ${val}`));
      }
    }
    return badges.length ? elFactory("div", { class: "ck-presets-row" }, ...badges) : null;
  }

  /* ===================== 1. IMAGE STUDIO ===================== */
  function renderImageStudio() {
    const root = document.getElementById("ck-image-root");
    if (!root) return;
    const target = getSelectedTarget();
    const profile = getSelectedProfile();
    const feats = profile?.features || {};
    const svcName = target?.service || "image";
    const defaultModel = getSelectedModel() || profile?.default_model || svcName;
    const hasAsyncJobs = Boolean(feats.async_jobs);
    const hasChatShim = Boolean(feats.chat_image_shim);
    const supportsSteps = !feats.fixed_steps;
    const supportsNeg = Boolean(feats.supports_negative_prompt) && !feats.rejects_negative_prompt;
    const supportsCfg = Boolean(feats.supports_cfg);
    const allowDrive = Boolean(feats.drive_io) && !isKaggleTarget();
    const maxRefs = feats.max_refs || 4;
    const refTagStyle = feats.ref_tag_style || "<imageN>";
    const makeRefTag = (idx) => (refTagStyle === "<Picture N>" ? `<Picture ${idx + 1}>` : `<image${idx + 1}>`);

    const promptEl = el("textarea", {
      id: "ck-img-prompt",
      class: "ck-autosize",
      rows: "3",
      placeholder: `输入图像提示词；编辑/多图模式可点击缩略图下方按钮插入 ${refTagStyle}`,
    });
    const negEl = el("input", {
      id: "ck-img-neg",
      type: "text",
      placeholder: "负面提示词（可选）",
      disabled: !supportsNeg || undefined,
    });
    const sizeSelect = el(
      "select",
      { id: "ck-img-size" },
      el("option", { value: "1024x1024" }, "1024x1024 (1:1)"),
      el("option", { value: "1280x720" }, "1280x720 (16:9)"),
      el("option", { value: "720x1280" }, "720x1280 (9:16)"),
      el("option", { value: "1024x1536" }, "1024x1536 (2:3)"),
      el("option", { value: "1536x1024" }, "1536x1024 (3:2)")
    );
    const aspectSelect = el(
      "select",
      { id: "ck-img-aspect" },
      ...(feats.aspect_presets || ["auto", "1:1", "16:9", "4:3", "3:2", "9:16"]).map((a) =>
        el("option", { value: a }, a)
      )
    );
    const tierSelect = el(
      "select",
      { id: "ck-img-tier" },
      ...(feats.tiers || ["4k", "2k", "1k"]).map((t) => el("option", { value: t }, t.toUpperCase()))
    );
    const stepsInput = el("input", {
      id: "ck-img-steps",
      type: "number",
      min: "1",
      max: "100",
      value: String(feats.default_steps || 20),
      disabled: !supportsSteps || undefined,
    });
    const cfgInput = el("input", {
      id: "ck-img-cfg",
      type: "number",
      min: "0",
      max: "20",
      step: "0.5",
      value: "4.0",
      disabled: !supportsCfg || undefined,
    });
    const countInput = el("input", { id: "ck-img-n", type: "number", min: "1", max: "4", value: "1" });
    const seedInput = el("input", { id: "ck-img-seed", type: "number", placeholder: "随机" });
    const refsInput = el("input", { id: "ck-img-refs", type: "file", accept: "image/*", multiple: "" });
    const refStrip = el("div", { class: "ck-ref-strip", hidden: "" });
    const driveRefInput = el("input", {
      id: "ck-img-drive-refs",
      type: "text",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "可选 drive://... 参考图路径，多个用逗号分隔",
    });
    const saveDriveEl = makeSwitch("ck-img-save-drive", "保存到 Drive (save_to_drive)");
    const progressEl = el("progress", { class: "ck-progress", "aria-label": "图像生成进度", hidden: "" });
    const galleryEl = el("div", { id: "ck-img-gallery", class: "ck-gallery-wrap" });
    const statusLine = el("div", { class: "muted ck-cap-summary" }, "准备就绪。");
    const cancelPollBtn = el("button", { type: "button", class: "btn small danger", hidden: "" }, "取消轮询");
    const queueBtn = hasAsyncJobs
      ? el("button", { type: "button", class: "btn small secondary" }, "查看异步队列 (/api/ct/jobs)")
      : null;

    cancelPollBtn.addEventListener("click", () => studioState.imageAbortController?.abort());
    if (queueBtn) {
      queueBtn.addEventListener("click", async () => {
        try {
          const tgt = requireTarget();
          const resp = await proxyFetch(tgt.session_name, "api/ct/jobs");
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error || resp.statusText);
          const list = Array.isArray(data) ? data : data.jobs || data.data || [];
          statusLine.textContent = list.length
            ? `队列共 ${list.length} 个任务：${list.slice(0, 4).map((j) => `${j.job_id || j.id}(${j.status})`).join(" · ")}`
            : "当前异步队列为空。";
        } catch (err) {
          toast(err.message, "error");
        }
      });
    }

    function renderRefStrip() {
      if (!studioState.imageRefFiles.length) {
        refStrip.hidden = true;
        refStrip.replaceChildren();
        return;
      }
      refStrip.hidden = false;
      refStrip.replaceChildren(
        ...studioState.imageRefFiles.map((item, idx) => {
          const tag = makeRefTag(idx);
          const insertBtn = el("button", { type: "button", class: "btn small secondary", title: `在提示词光标处插入 ${tag}` }, tag);
          insertBtn.addEventListener("click", () => insertTokenAtCursor(promptEl, tag));
          const rmBtn = el("button", { type: "button", class: "btn icon-btn", "aria-label": `移除参考图 ${idx + 1}` }, "✕");
          rmBtn.addEventListener("click", () => {
            studioState.imageRefFiles.splice(idx, 1);
            renderRefStrip();
          });
          return el(
            "div",
            { class: "ck-ref-card" },
            el("img", { src: item.url, alt: item.name, class: "ck-ref-thumb" }),
            el("div", { class: "ck-ref-actions" }, insertBtn, rmBtn)
          );
        })
      );
    }

    refsInput.addEventListener("change", () => {
      const picked = Array.from(refsInput.files || []);
      for (const f of picked) {
        if (studioState.imageRefFiles.length >= maxRefs) {
          toast(`最多支持 ${maxRefs} 张参考图`, "warn");
          break;
        }
        studioState.imageRefFiles.push({
          file: f,
          name: f.name,
          url: trackBlobUrl(URL.createObjectURL(f)),
        });
      }
      refsInput.value = "";
      renderRefStrip();
    });

    const negField = supportsNeg ? makeField("ck-img-neg", "负面提示词 (negative_prompt)", negEl) : null;
    const sizeOrAspectField = hasAsyncJobs
      ? makeField("ck-img-aspect", "画幅比例 (aspect)", aspectSelect)
      : makeField("ck-img-size", "输出尺寸 (size)", sizeSelect);
    const tierOrCountField = hasAsyncJobs
      ? makeField("ck-img-tier", "清晰度档位 (tier)", tierSelect)
      : makeField("ck-img-n", "生成张数 (n)", countInput);
    const stepsField = makeField("ck-img-steps", "推理步数 (steps)", stepsInput);
    const cfgOrSeedField = supportsCfg
      ? makeField("ck-img-cfg", "True CFG Scale", cfgInput)
      : makeField("ck-img-seed", "随机种子 (seed)", seedInput);
    const refsField = makeField("ck-img-refs", `添加参考图（已选，最多 ${maxRefs} 张）`, refsInput);
    const driveRefField = allowDrive ? makeField("ck-img-drive-refs", "Drive 参考图路径", driveRefInput) : null;

    const submitBtn = el("button", { type: "submit", class: "btn primary" }, "开始生成");

    const modeCtrl = makeModeGroup(
      "ck-img-mode",
      "图像生成模式",
      [
        ["gen", "文生图 (/v1/images/generations)"],
        ["edit", "图生图编辑 (/v1/images/edits)"],
        ...(hasAsyncJobs ? [["async4k", "4K 异步任务 (/api/ct/jobs)"]] : []),
        ...(hasChatShim ? [["chat", "Chat 图像接口 (/v1/chat/completions)"]] : []),
      ],
      "gen",
      syncModeFields
    );

    function syncModeFields(mode) {
      const isEditOrAsync = mode === "edit" || mode === "async4k";
      const isChat = mode === "chat";
      if (negField) negField.hidden = isChat || !supportsNeg;
      sizeOrAspectField.hidden = isChat || (mode === "edit" && !hasAsyncJobs);
      tierOrCountField.hidden = isChat;
      stepsField.hidden = isChat || (mode === "async4k" && !supportsSteps);
      cfgOrSeedField.hidden = isChat;
      refsField.hidden = !isEditOrAsync;
      refStrip.hidden = !isEditOrAsync || studioState.imageRefFiles.length === 0;
      if (driveRefField) driveRefField.hidden = !isEditOrAsync;
      if (queueBtn) queueBtn.hidden = mode !== "async4k";
      submitBtn.textContent =
        mode === "edit" ? "执行参考图编辑" : mode === "async4k" ? "提交 4K 异步任务" : mode === "chat" ? "通过 Chat 接口生成" : "开始文生图";
    }
    syncModeFields(modeCtrl.getValue());
    renderRefStrip();

    const noticeText = feats.rejects_negative_prompt
      ? `${svcName} 约束：固定 ${feats.fixed_steps || 6} 步，支持最多 ${maxRefs} 张参考图（${refTagStyle}），服务端会拒绝 negative_prompt 与 lora。`
      : `支持最多 ${maxRefs} 张参考图（${refTagStyle}），同时支持本地多图缩略图编排与结果一键续编辑。`;

    const pupBtn = feats.pup_upsample
      ? el("button", { type: "button", class: "btn small secondary" }, "PUP 扩写提示词")
      : null;
    if (pupBtn) {
      pupBtn.addEventListener("click", async () => {
        try {
          const tgt = requireTarget();
          const prompt = promptEl.value.trim();
          if (!prompt) return toast("请先输入简短提示词", "warn");
          progressEl.hidden = false;
          pupBtn.disabled = true;
          const resp = await proxyFetch(tgt.session_name, "api/ct/upsample", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt }),
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error || resp.statusText);
          if (data.upsampled_prompt || data.prompt) {
            promptEl.value = data.upsampled_prompt || data.prompt;
            toast("已完成 PUP 提示词扩写", "ok");
          }
        } catch (err) {
          toast(err.message, "error");
        } finally {
          progressEl.hidden = true;
          pupBtn.disabled = false;
        }
      });
    }

    const formEl = el(
      "form",
      { class: "card ck-studio-form-card" },
      el("h3", {}, "图像参数与参考图编排"),
      el("p", { class: "muted ck-cap-summary" }, noticeText),
      modeCtrl.group,
      makeField("ck-img-prompt", "正向提示词", promptEl),
      pupBtn || queueBtn ? el("div", { class: "ck-presets-row" }, pupBtn, queueBtn) : null,
      negField,
      el("div", { class: "ck-two-col" }, sizeOrAspectField, tierOrCountField),
      el("div", { class: "ck-two-col" }, stepsField, cfgOrSeedField),
      refsField,
      refStrip,
      driveRefField,
      allowDrive ? saveDriveEl : null,
      el("div", { class: "ck-composer-right" }, progressEl, cancelPollBtn, submitBtn)
    );

    function appendImages(items, headerBadges, promptSnapshot = "") {
      if (!items.length) {
        statusLine.textContent = "接口未返回可预览图像数据，请检查同源 HTTP 探针或原始 JSON。";
        return;
      }
      for (let i = 0; i < items.length; i += 1) {
        const src = items[i];
        const useAsRefBtn = el("button", { type: "button", class: "btn small secondary" }, "设为参考图继续编辑");
        useAsRefBtn.addEventListener("click", async () => {
          try {
            if (studioState.imageRefFiles.length >= maxRefs) {
              return toast(`参考图已达上限 (${maxRefs} 张)`, "warn");
            }
            const res = await fetch(src);
            const blob = await res.blob();
            const file = new File([blob], `ref-${Date.now()}.png`, { type: blob.type || "image/png" });
            studioState.imageRefFiles.push({
              file,
              name: file.name,
              url: trackBlobUrl(URL.createObjectURL(blob)),
            });
            modeCtrl.setValue(hasAsyncJobs && modeCtrl.getValue() === "async4k" ? "async4k" : "edit", true);
            renderRefStrip();
            promptEl.focus();
            toast(`已加入参考图队列 (${makeRefTag(studioState.imageRefFiles.length - 1)})`, "ok");
          } catch (err) {
            toast(`载入参考图失败：${err.message}`, "error");
          }
        });

        const card = el(
          "div",
          { class: "ck-media-card" },
          el("img", { src, alt: promptSnapshot ? promptSnapshot.slice(0, 48) : `生成图像 #${i + 1}` }),
          headerBadges?.cloneNode?.(true) || null,
          el(
            "div",
            { class: "ck-presets-row" },
            el("a", { href: src, download: `colabllm-image-${Date.now()}-${i}.png`, class: "btn small secondary" }, "下载图片"),
            useAsRefBtn,
            onSendMediaToChat
              ? (() => {
                  const b = el("button", { type: "button", class: "btn small secondary" }, "发往 Chat 分析");
                  b.addEventListener("click", () => onSendMediaToChat({ kind: "image", name: `generated-${i + 1}.png`, url: src }));
                  return b;
                })()
              : null
          )
        );
        galleryEl.prepend(card);
      }
    }

    formEl.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const tgt = requireTarget();
        const prompt = promptEl.value.trim();
        if (!prompt) return toast("请输入提示词", "warn");
        const mode = modeCtrl.getValue();
        const files = studioState.imageRefFiles.slice(0, maxRefs).map((r) => r.file);
        const driveRefs = driveRefInput.value
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
        const saveToDrive = allowDrive && Boolean(document.getElementById("ck-img-save-drive")?.checked);
        const seedVal = seedInput.value.trim();

        studioState.imageAbortController = new AbortController();
        const signal = studioState.imageAbortController.signal;
        const t0 = performance.now();
        progressEl.hidden = false;
        submitBtn.disabled = true;
        cancelPollBtn.hidden = mode !== "async4k";
        statusLine.textContent = "正在通过同源代理发送图像请求...";

        if (mode === "async4k") {
          const uploadedIds = [];
          for (const file of files) {
            const fd = new FormData();
            fd.append("file", file);
            const up = await proxyFetch(tgt.session_name, "api/ct/uploads", { method: "POST", body: fd, signal });
            const upData = await up.json();
            if (!up.ok) throw new Error(upData.error || "参考图上传失败");
            uploadedIds.push(upData.file_id || upData.id);
          }
          const jobPayload = {
            prompt,
            aspect: aspectSelect.value,
            tier: tierSelect.value,
            save_to_drive: saveToDrive,
          };
          if (uploadedIds.length) jobPayload.ref_file_ids = uploadedIds;
          if (driveRefs.length) jobPayload.image_urls = driveRefs;
          if (seedVal) jobPayload.seed = Number(seedVal);

          const jobResp = await proxyFetch(tgt.session_name, "api/ct/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(jobPayload),
            signal,
          });
          const job = await jobResp.json();
          if (!jobResp.ok) throw new Error(job.error || "提交异步任务失败");
          const jobId = job.job_id || job.id;
          statusLine.textContent = `异步任务 ${jobId} 已提交，正在轮询生成进度...`;

          let finalJob = job;
          for (let attempt = 0; attempt < 90; attempt += 1) {
            if (["completed", "succeeded", "failed", "cancelled"].includes(String(finalJob.status || "").toLowerCase())) {
              break;
            }
            await new Promise((resolve, reject) => {
              const t = setTimeout(resolve, 2000);
              signal.addEventListener("abort", () => {
                clearTimeout(t);
                reject(new DOMException("Aborted", "AbortError"));
              }, { once: true });
            });
            const poll = await proxyFetch(tgt.session_name, `api/ct/jobs/${encodeURIComponent(jobId)}`, { signal });
            finalJob = await poll.json();
            const elapsedSec = Math.round((performance.now() - t0) / 1000);
            statusLine.textContent = `任务 ${jobId}: ${finalJob.status || "running"} (${elapsedSec}s)`;
          }
          if (String(finalJob.status || "").toLowerCase() === "failed") {
            throw new Error(finalJob.error || "异步图像任务失败");
          }
          const count = Number(finalJob.image_count || finalJob.outputs?.length || 1);
          const imgs = [];
          for (let idx = 0; idx < count; idx += 1) {
            const imgResp = await proxyFetch(tgt.session_name, `api/ct/jobs/${encodeURIComponent(jobId)}/content?index=${idx}&format=webp`, { signal });
            if (imgResp.ok) {
              const blob = await imgResp.blob();
              imgs.push(trackBlobUrl(URL.createObjectURL(blob)));
            }
          }
          const dt = Math.round(performance.now() - t0);
          appendImages(imgs, el("div", { class: "ck-presets-row" }, el("span", { class: "badge ok" }, `4K Job ${jobId}`), el("span", { class: "badge neutral ck-mono" }, `${(dt / 1000).toFixed(1)}s`)), prompt);
          statusLine.textContent = `异步 4K 任务 ${jobId} 完成 (${(dt / 1000).toFixed(1)}s)。`;
        } else if (mode === "edit") {
          if (!files.length && !driveRefs.length) {
            return toast("编辑模式请先上传参考图或填写 Drive 路径", "warn");
          }
          let resp;
          if (files.length) {
            const fd = new FormData();
            fd.append("model", defaultModel);
            fd.append("prompt", prompt);
            fd.append("response_format", "b64_json");
            fd.append("n", countInput.value || "1");
            if (hasAsyncJobs) {
              fd.append("aspect", aspectSelect.value);
              fd.append("tier", tierSelect.value);
            }
            if (supportsSteps && stepsInput.value) fd.append("steps", stepsInput.value);
            if (supportsNeg && negEl.value.trim()) fd.append("negative_prompt", negEl.value.trim());
            if (supportsCfg && cfgInput.value) fd.append("true_cfg_scale", cfgInput.value);
            if (seedVal) fd.append("seed", seedVal);
            if (saveToDrive) fd.append("save_to_drive", "true");
            for (const f of files) fd.append("image", f);
            resp = await proxyFetch(tgt.session_name, "v1/images/edits", { method: "POST", body: fd, signal });
          } else {
            const body = {
              model: defaultModel,
              prompt,
              response_format: "b64_json",
              n: Number(countInput.value || 1),
              image: driveRefs,
            };
            if (hasAsyncJobs) {
              body.aspect = aspectSelect.value;
              body.tier = tierSelect.value;
            }
            if (supportsSteps && stepsInput.value) body.steps = Number(stepsInput.value);
            if (supportsNeg && negEl.value.trim()) body.negative_prompt = negEl.value.trim();
            if (supportsCfg && cfgInput.value) body.true_cfg_scale = Number(cfgInput.value);
            if (seedVal) body.seed = Number(seedVal);
            if (saveToDrive) body.save_to_drive = true;
            resp = await proxyFetch(tgt.session_name, "v1/images/edits", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
              signal,
            });
          }
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error?.message || data.error || resp.statusText);
          const dt = Math.round(performance.now() - t0);
          const urls = (data.data || [])
            .map((d) => (d.b64_json ? `data:image/png;base64,${d.b64_json}` : d.url))
            .filter(Boolean);
          appendImages(urls, renderHeaderBadges(resp.headers, el, dt), prompt);
          statusLine.textContent = `已返回 ${urls.length} 张编辑结果 (${(dt / 1000).toFixed(2)}s)。`;
        } else if (mode === "chat") {
          const resp = await proxyFetch(tgt.session_name, "v1/chat/completions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              model: defaultModel,
              messages: [{ role: "user", content: prompt }],
              stream: false,
            }),
            signal,
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error?.message || data.error || resp.statusText);
          const dt = Math.round(performance.now() - t0);
          const content = data.choices?.[0]?.message?.content || "";
          const match = String(content).match(/data:image\/[a-zA-Z+]+;base64,[A-Za-z0-9+/=]+/);
          if (match) appendImages([match[0]], renderHeaderBadges(resp.headers, el, dt), prompt);
          statusLine.textContent = match ? `已通过 Chat Image Shim 提取图像 (${(dt / 1000).toFixed(2)}s)。` : `文本回复：${String(content).slice(0, 160)}`;
        } else {
          const body = {
            model: defaultModel,
            prompt,
            size: sizeSelect.value,
            n: Number(countInput.value || 1),
            response_format: "b64_json",
          };
          if (hasAsyncJobs) {
            body.aspect = aspectSelect.value;
            body.tier = tierSelect.value;
          }
          if (supportsSteps && stepsInput.value) body.steps = Number(stepsInput.value);
          if (supportsNeg && negEl.value.trim()) body.negative_prompt = negEl.value.trim();
          if (supportsCfg && cfgInput.value) body.true_cfg_scale = Number(cfgInput.value);
          if (seedVal) body.seed = Number(seedVal);
          if (saveToDrive) body.save_to_drive = true;

          const resp = await proxyFetch(tgt.session_name, "v1/images/generations", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
            signal,
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error?.message || data.error || resp.statusText);
          const dt = Math.round(performance.now() - t0);
          const urls = (data.data || [])
            .map((d) => (d.b64_json ? `data:image/png;base64,${d.b64_json}` : d.url))
            .filter(Boolean);
          appendImages(urls, renderHeaderBadges(resp.headers, el, dt), prompt);
          statusLine.textContent = `已生成 ${urls.length} 张图像 (${(dt / 1000).toFixed(2)}s)。`;
        }
      } catch (err) {
        if (err.name === "AbortError") {
          statusLine.textContent = "已取消异步轮询。";
          toast("已取消轮询", "warn");
        } else {
          statusLine.textContent = `错误：${err.message}`;
          toast(err.message, "error");
        }
      } finally {
        studioState.imageAbortController = null;
        progressEl.hidden = true;
        cancelPollBtn.hidden = true;
        submitBtn.disabled = false;
      }
    });

    const clearGalleryBtn = el("button", { type: "button", class: "btn small secondary" }, "清空图库");
    clearGalleryBtn.addEventListener("click", () => {
      galleryEl.replaceChildren();
      statusLine.textContent = "图库已清空。";
    });

    root.replaceChildren(
      el(
        "div",
        { class: "ck-studio-grid" },
        formEl,
        el(
          "div",
          { class: "card ck-studio-output-card" },
          el("div", { class: "ck-output-header" }, el("h3", {}, "生成图库"), clearGalleryBtn),
          statusLine,
          galleryEl
        )
      )
    );
  }

  /* ===================== 2. AUDIO STUDIO ===================== */
  function renderAudioStudio() {
    const root = document.getElementById("ck-audio-root");
    if (!root) return;
    const profile = getSelectedProfile();
    const feats = profile?.features || {};
    const defaultModel = getSelectedModel() || profile?.default_model || getSelectedTarget()?.service || "tts";
    const isSfx = Boolean(feats.sfx) && !feats.tts;
    const hasBatchSpeech = Boolean(feats.batch_speech);

    const textEl = el("textarea", {
      id: "ck-aud-input",
      class: "ck-autosize",
      rows: "3",
      placeholder: isSfx
        ? "描述要生成的音效环境、动作与质感（支持 1-30 秒立体声 44.1kHz）"
        : "输入要合成为语音的文本内容...",
    });
    const voiceInput = el("input", {
      id: "ck-aud-voice",
      type: "text",
      list: "ck-aud-voice-datalist",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      value: "default",
    });
    const voiceDatalist = el("datalist", { id: "ck-aud-voice-datalist" }, el("option", { value: "default" }));
    const fmtSelect = el(
      "select",
      { id: "ck-aud-format" },
      el("option", { value: "wav" }, "wav"),
      el("option", { value: "mp3" }, "mp3"),
      el("option", { value: "flac" }, "flac"),
      el("option", { value: "opus" }, "opus")
    );
    const speedSlider = !isSfx
      ? makeSliderField("ck-aud-speed", "语速 (speed)", {
          min: 0.5,
          max: 2.0,
          step: 0.1,
          value: 1.0,
          formatFn: (v) => `${Number(v).toFixed(1)}x`,
        })
      : null;

    const durationSlider = isSfx
      ? makeSliderField("ck-aud-duration", "音效时长 (duration)", {
          min: 1,
          max: 30,
          step: 0.5,
          value: 8,
          formatFn: (v) => `${Number(v).toFixed(1)}s`,
        })
      : null;
    const stepsInput = el("input", { id: "ck-aud-steps", type: "number", min: "5", max: "100", value: "25" });
    const seedInput = el("input", { id: "ck-aud-seed", type: "number", placeholder: "随机" });

    const emoAlphaSlider = feats.emotion_vector_8d
      ? makeSliderField("ck-aud-emo-alpha", "情绪强度 (emo_alpha)", {
          min: 0,
          max: 1,
          step: 0.05,
          value: 0.8,
          formatFn: (v) => Number(v).toFixed(2),
        })
      : null;
    const emoTextInput = el("input", { id: "ck-aud-emo-text", type: "text", placeholder: "自动根据情感提示文本驱动语调（可选）" });
    const emoSliderCtrls = {};
    const emoPresetsBar = feats.emotion_vector_8d
      ? el(
          "div",
          { class: "ck-presets-row" },
          ...EMO_PRESETS.map((preset) => {
            const b = el("button", { type: "button", class: "btn small secondary" }, preset.label);
            b.addEventListener("click", () => {
              if (emoAlphaSlider) {
                emoAlphaSlider.input.value = String(preset.alpha);
                emoAlphaSlider.sync();
              }
              EMO_LABELS.forEach(([key], idx) => {
                const ctrl = emoSliderCtrls[key];
                if (ctrl) {
                  ctrl.input.value = String(preset.vec[idx] ?? 0);
                  ctrl.sync();
                }
              });
            });
            return b;
          })
        )
      : null;

    const emoGrid = feats.emotion_vector_8d
      ? el(
          "details",
          { class: "ck-accordion" },
          el("summary", {}, "IndexTTS 8 维情绪向量 (emo_vector)"),
          el(
            "div",
            { class: "ck-acc-body" },
            emoPresetsBar,
            el(
              "div",
              { class: "ck-emo-grid" },
              ...EMO_LABELS.map(([key, label]) => {
                const ctrl = makeSliderField(`ck-emo-${key}`, label, {
                  min: 0,
                  max: 1,
                  step: 0.05,
                  value: 0,
                  formatFn: (v) => Number(v).toFixed(2),
                });
                emoSliderCtrls[key] = ctrl;
                return ctrl.wrap;
              })
            )
          )
        )
      : null;

    const tagsBar = Array.isArray(feats.inline_tags)
      ? el(
          "div",
          { class: "ck-presets-row" },
          el("span", { class: "muted" }, "快捷插入戏剧标签："),
          ...feats.inline_tags.map((tag) => {
            const b = el("button", { type: "button", class: "btn small secondary" }, tag);
            b.addEventListener("click", () => insertTokenAtCursor(textEl, tag));
            return b;
          })
        )
      : null;

    const speechModeCtrl = hasBatchSpeech
      ? makeModeGroup(
          "ck-aud-mode",
          "Drama3 合成模式",
          [
            ["speech", "单条语音 (/v1/audio/speech)"],
            ["batch", "多段对白批量 (/v1/audio/speech/batch)"],
          ],
          "speech",
          (m) => {
            textEl.placeholder =
              m === "batch"
                ? "每行一段对白；可用 voice: 台词 指定角色，例如：\n[whispers] 听见了吗？\ndefault: [laughs] 我们开始吧！"
                : "输入要合成为语音的文本内容...";
          }
        )
      : null;

    const progressEl = el("progress", { class: "ck-progress", "aria-label": "音频生成进度", hidden: "" });
    const statusLine = el("div", { class: "muted ck-cap-summary" }, "等待合成或生成音频。");
    const resultsList = el("div", { class: "ck-studio-output-card" });

    let cloneSampleBadge = null;
    let cloneDetailsEl = null;

    const submitBtn = el("button", { type: "submit", class: "btn primary" }, isSfx ? "生成音效" : "合成语音");
    const mainForm = el(
      "form",
      { class: "card ck-studio-form-card" },
      el("h3", {}, isSfx ? "TangoFlux 音效生成 (/v1/audio/generations)" : "语音合成 (/v1/audio/speech)"),
      speechModeCtrl ? speechModeCtrl.group : null,
      makeField("ck-aud-input", isSfx ? "音效描述 Prompt" : "合成文本 (input)", textEl),
      tagsBar,
      isSfx
        ? el(
            "div",
            { class: "ck-two-col" },
            durationSlider.wrap,
            el("div", { class: "ck-two-col" }, makeField("ck-aud-steps", "步数 (steps)", stepsInput), makeField("ck-aud-seed", "种子 (seed)", seedInput))
          )
        : el(
            "div",
            { class: "ck-two-col" },
            makeField("ck-aud-voice", "音色名称 (voice)", voiceInput),
            makeField("ck-aud-format", "音频格式 (response_format)", fmtSelect)
          ),
      voiceDatalist,
      speedSlider ? speedSlider.wrap : null,
      feats.emotion_vector_8d
        ? el("div", { class: "ck-two-col" }, emoAlphaSlider.wrap, makeField("ck-aud-emo-text", "情绪文本引导 (emo_text)", emoTextInput))
        : null,
      emoGrid,
      el("div", { class: "ck-composer-right" }, progressEl, submitBtn)
    );

    function appendAudioResultCard({ title, src, blob = null, badges = null, ext = "wav" }) {
      const asrBtn =
        typeof onSendAudioToTranscribe === "function"
          ? el("button", { type: "button", class: "btn small secondary" }, "送入转写核验")
          : null;
      if (asrBtn) {
        asrBtn.addEventListener("click", () =>
          onSendAudioToTranscribe({ url: src, blob, name: `synthesized-${Date.now()}.${ext}` })
        );
      }
      const cloneBtn =
        feats.voices_crud && blob
          ? el("button", { type: "button", class: "btn small secondary" }, "设为克隆参考干声")
          : null;
      if (cloneBtn) {
        cloneBtn.addEventListener("click", () => {
          studioState.cloneSampleFile = new File([blob], `clone-ref-${Date.now()}.${ext}`, {
            type: blob.type || "audio/wav",
          });
          if (cloneSampleBadge) {
            cloneSampleBadge.hidden = false;
            cloneSampleBadge.textContent = `已载入参考干声：${studioState.cloneSampleFile.name}`;
          }
          if (cloneDetailsEl) cloneDetailsEl.open = true;
          toast("已将生成音频设为克隆参考样本", "ok");
        });
      }

      resultsList.prepend(
        el(
          "div",
          { class: "ck-media-card" },
          el("strong", {}, title),
          badges?.cloneNode?.(true) || null,
          el("audio", { controls: "", src }),
          el(
            "div",
            { class: "ck-presets-row" },
            el("a", { href: src, download: `colabllm-audio-${Date.now()}.${ext}`, class: "btn small secondary" }, "下载音频"),
            asrBtn,
            cloneBtn
          )
        )
      );
    }

    mainForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const tgt = requireTarget();
        const text = textEl.value.trim();
        if (!text) return toast("请输入文本或音效描述", "warn");
        const mode = speechModeCtrl?.getValue() || "speech";
        progressEl.hidden = false;
        submitBtn.disabled = true;
        statusLine.textContent = "正在生成音频...";
        const t0 = performance.now();

        let path = "v1/audio/speech";
        let payload;
        if (isSfx) {
          path = "v1/audio/generations";
          payload = {
            model: defaultModel,
            prompt: text,
            duration: Number(durationSlider?.input.value || 8),
            steps: Number(stepsInput.value || 25),
          };
          if (seedInput.value.trim()) payload.seed = Number(seedInput.value.trim());
        } else if (mode === "batch") {
          path = "v1/audio/speech/batch";
          const fallbackVoice = voiceInput.value.trim() || "default";
          const items = text
            .split("\n")
            .map((l) => l.trim())
            .filter(Boolean)
            .map((line) => {
              const m = line.match(/^([A-Za-z0-9_.-]{1,32}):\s+(.+)$/);
              return m ? { voice: m[1], input: m[2] } : { voice: fallbackVoice, input: line };
            });
          payload = { model: defaultModel, items, response_format: fmtSelect.value };
        } else {
          payload = {
            model: defaultModel,
            input: text,
            voice: voiceInput.value.trim() || "default",
            response_format: fmtSelect.value,
          };
          const speedVal = Number(speedSlider?.input.value || 1.0);
          if (Math.abs(speedVal - 1.0) > 0.01) payload.speed = speedVal;
          if (feats.emotion_vector_8d) {
            payload.emo_alpha = Number(emoAlphaSlider?.input.value || 0.8);
            if (emoTextInput.value.trim()) payload.emo_text = emoTextInput.value.trim();
            const vec = EMO_LABELS.map(([k]) => Number(emoSliderCtrls[k]?.input.value || 0));
            if (vec.some((v) => v > 0)) payload.emo_vector = vec;
          }
        }

        const resp = await proxyFetch(tgt.session_name, path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!resp.ok) {
          const errText = await resp.text();
          throw new Error(errText || resp.statusText);
        }
        const dt = Math.round(performance.now() - t0);
        const ct = (resp.headers.get("Content-Type") || "").toLowerCase();
        const badges = renderHeaderBadges(resp.headers, el, dt);

        if (ct.includes("application/json")) {
          const data = await resp.json();
          const items = data.data || data.results || [data];
          let rendered = 0;
          for (const item of items) {
            const b64 = item.b64_json || item.audio_base64;
            if (b64) {
              const src = `data:audio/wav;base64,${b64}`;
              rendered += 1;
              appendAudioResultCard({ title: item.input || `分段 #${rendered}`, src, badges, ext: fmtSelect.value || "wav" });
            }
          }
          statusLine.textContent = `已返回 ${rendered} 段音频 JSON (${(dt / 1000).toFixed(2)}s)。`;
        } else {
          const blob = await resp.blob();
          const url = trackBlobUrl(URL.createObjectURL(blob));
          appendAudioResultCard({
            title: text.slice(0, 60),
            src: url,
            blob,
            badges,
            ext: fmtSelect.value || "wav",
          });
          statusLine.textContent = `已生成 ${blob.size} 字节音频 (${(dt / 1000).toFixed(2)}s)。`;
        }
      } catch (err) {
        statusLine.textContent = `失败：${err.message}`;
        toast(err.message, "error");
      } finally {
        progressEl.hidden = true;
        submitBtn.disabled = false;
      }
    });

    const clearOutBtn = el("button", { type: "button", class: "btn small secondary" }, "清空结果");
    clearOutBtn.addEventListener("click", () => {
      resultsList.replaceChildren();
      statusLine.textContent = "等待合成或生成音频。";
    });
    const rightCol = [
      el(
        "div",
        { class: "card ck-studio-output-card" },
        el("div", { class: "ck-output-header" }, el("h3", {}, "音频播放与导出"), clearOutBtn),
        statusLine,
        resultsList
      ),
    ];

    if (feats.voices_crud) {
      const voicesContainer = el("div", { class: "ck-presets-row" }, el("span", { class: "muted" }, "点击刷新读取已保存音色库"));
      const loadVoicesBtn = el("button", { type: "button", class: "btn small secondary" }, "刷新音色列表 (/v1/audio/voices)");
      loadVoicesBtn.addEventListener("click", async () => {
        try {
          const tgt = requireTarget();
          const resp = await proxyFetch(tgt.session_name, "v1/audio/voices");
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error || resp.statusText);
          const names = Array.isArray(data) ? data : data.voices || data.data || [];
          voiceDatalist.replaceChildren(
            el("option", { value: "default" }),
            ...names.map((v) => el("option", { value: typeof v === "string" ? v : v.name || v.id }))
          );
          voicesContainer.replaceChildren(
            ...(names.length
              ? names.map((v) => {
                  const n = typeof v === "string" ? v : v.name || v.id;
                  const useBtn = el("button", { type: "button", class: "btn small secondary" }, `选用: ${n}`);
                  useBtn.addEventListener("click", () => {
                    voiceInput.value = n;
                    toast(`已选用音色 ${n}`, "ok");
                  });
                  const delBtn = el("button", { type: "button", class: "btn small danger" }, "删除");
                  delBtn.addEventListener("click", async () => {
                    const dr = await proxyFetch(tgt.session_name, `v1/audio/voices/${encodeURIComponent(n)}`, { method: "DELETE" });
                    if (dr.ok) {
                      toast(`已删除音色 ${n}`, "ok");
                      loadVoicesBtn.click();
                    }
                  });
                  return el("div", { class: "ck-attach-chip" }, useBtn, delBtn);
                })
              : [el("span", { class: "muted" }, "暂无自定义音色")])
          );
        } catch (err) {
          toast(err.message, "error");
        }
      });

      const cloneName = el("input", { id: "ck-clone-name", type: "text", autocorrect: "off", autocapitalize: "none", spellcheck: "false", placeholder: "my_voice" });
      const cloneFile = el("input", { id: "ck-clone-file", type: "file", accept: "audio/*" });
      cloneSampleBadge = el("span", { class: "badge ok", hidden: !studioState.cloneSampleFile || undefined }, studioState.cloneSampleFile ? `已载入参考干声：${studioState.cloneSampleFile.name}` : "");
      cloneFile.addEventListener("change", () => {
        if (cloneFile.files?.[0]) {
          studioState.cloneSampleFile = cloneFile.files[0];
          cloneSampleBadge.hidden = false;
          cloneSampleBadge.textContent = `已选择文件：${cloneFile.files[0].name}`;
        }
      });
      const cloneRefText = el("input", { id: "ck-clone-reftext", type: "text", placeholder: "参考音频对应文本（可选）" });
      const cloneConsent = makeSwitch("ck-clone-consent", "我已获得说话人声音克隆授权 (consent=true)", true);
      const cloneSubmitBtn = el("button", { type: "submit", class: "btn small primary" }, "上传并创建音色");
      const cloneForm = el(
        "form",
        { class: "ck-studio-form-card" },
        makeField("ck-clone-name", "音色 ID", cloneName),
        makeField("ck-clone-file", "上传参考干声", cloneFile),
        cloneSampleBadge,
        makeField("ck-clone-reftext", "参考文本 (ref_text)", cloneRefText),
        cloneConsent,
        cloneSubmitBtn
      );
      cloneForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
          const tgt = requireTarget();
          const name = cloneName.value.trim();
          const file = cloneFile.files?.[0] || studioState.cloneSampleFile;
          if (!name || !file) return toast("请填写音色 ID 并选择参考音频样本", "warn");
          cloneSubmitBtn.disabled = true;
          const fd = new FormData();
          fd.append("name", name);
          fd.append("consent", String(Boolean(document.getElementById("ck-clone-consent")?.checked)));
          fd.append("audio_sample", file);
          if (cloneRefText.value.trim()) fd.append("ref_text", cloneRefText.value.trim());
          const resp = await proxyFetch(tgt.session_name, "v1/audio/voices", { method: "POST", body: fd });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error || resp.statusText);
          voiceInput.value = name;
          toast(`音色 ${name} 创建成功`, "ok");
          loadVoicesBtn.click();
        } catch (err) {
          toast(err.message, "error");
        } finally {
          cloneSubmitBtn.disabled = false;
        }
      });

      cloneDetailsEl = el(
        "details",
        { class: "ck-accordion" },
        el("summary", {}, "音色克隆库管理 (/v1/audio/voices)"),
        el("div", { class: "ck-acc-body" }, loadVoicesBtn, voicesContainer, cloneForm)
      );
      rightCol.push(cloneDetailsEl);
    }

    if (feats.voice_design) {
      const vdName = el("input", { id: "ck-vd-name", type: "text", autocorrect: "off", placeholder: "designed_voice" });
      const vdDesc = el("textarea", { id: "ck-vd-desc", class: "ck-autosize", rows: "2", placeholder: "例如：温柔知性的播客女声，咬字清晰..." });
      const vdSample = el("input", { id: "ck-vd-sample", type: "text", value: "你好，这是通过自然语言描述生成的新音色。" });
      const vdSubmitBtn = el("button", { type: "submit", class: "btn small primary" }, "生成并保存音色 (/api/ct/design-voice)");
      const vdForm = el(
        "form",
        { class: "ck-acc-body" },
        makeField("ck-vd-name", "保存音色名", vdName),
        makeField("ck-vd-desc", "音色风格描述", vdDesc),
        makeField("ck-vd-sample", "试听示例句", vdSample),
        vdSubmitBtn
      );
      vdForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
          const tgt = requireTarget();
          vdSubmitBtn.disabled = true;
          const resp = await proxyFetch(tgt.session_name, "api/ct/design-voice", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: vdName.value.trim() || "designed_voice",
              consent: true,
              voice_description: vdDesc.value.trim(),
              sample_text: vdSample.value.trim(),
            }),
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error || resp.statusText);
          voiceInput.value = vdName.value.trim() || "designed_voice";
          toast("设计音色成功，已选中该音色", "ok");
        } catch (err) {
          toast(err.message, "error");
        } finally {
          vdSubmitBtn.disabled = false;
        }
      });
      rightCol.push(el("details", { class: "ck-accordion" }, el("summary", {}, "Qwen3-TTS 自然语言捏音色"), vdForm));
    }

    root.replaceChildren(el("div", { class: "ck-studio-grid" }, mainForm, el("div", { class: "ck-studio-output-card" }, ...rightCol)));
  }

  /* ===================== 3. TRANSCRIBE STUDIO ===================== */
  let asrApplyBridgeFn = null;

  function renderTranscribeStudio() {
    const root = document.getElementById("ck-transcribe-root");
    if (!root) return;
    const profile = getSelectedProfile();
    const feats = profile?.features || {};
    const defaultModel = getSelectedModel() || profile?.default_model || getSelectedTarget()?.service || "asr";
    const isSalm = Boolean(feats.salm_chat_audio);
    const allowDrive = (Boolean(feats.drive_transcribe) || isSalm) && !isKaggleTarget();

    const fileEl = el("input", { id: "ck-asr-file", type: "file", accept: "audio/*,video/*" });
    const bridgedBadge = el("span", { class: "badge ok", hidden: true }, "");
    const driveEl = el("input", {
      id: "ck-asr-drive",
      type: "text",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "或输入 drive://audio/sample.wav",
    });
    const promptEl = el("textarea", {
      id: "ck-asr-prompt",
      class: "ck-autosize",
      rows: "2",
      value: "Transcribe the audio accurately.",
    });
    const ctcSwitch = makeSwitch("ck-asr-ctc", "捕获词级/句级 CTC 时间戳 (capture_ctc_timestamps)", true);
    const thinkSwitch = makeSwitch("ck-asr-think", "启用思考分析 (enable_thinking)", false);
    const audioPlayer = el("audio", { controls: "" });
    const progressEl = el("progress", { class: "ck-progress", "aria-label": "转写处理进度", hidden: "" });
    const transcriptOut = el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, "转写结果将显示在这里。"));
    const ctcContainer = el("div", { class: "ck-ctc-segments" });
    const presetsContainer = el("div", { class: "ck-presets-row" });

    let lastTranscriptText = "";
    let lastCtcSegments = [];
    const ctcButtonsMeta = [];

    const exportSrtBtn = el("button", { type: "button", class: "btn small secondary", hidden: "" }, "导出 SRT 字幕");
    exportSrtBtn.addEventListener("click", () => {
      const srt = buildSrtText(lastCtcSegments);
      if (!srt) return toast("当前结果没有时间戳切片", "warn");
      downloadTextFile(el, trackBlobUrl, srt, `colabllm-transcript-${Date.now()}.srt`);
    });
    const exportTxtBtn = el("button", { type: "button", class: "btn small secondary" }, "导出 TXT");
    exportTxtBtn.addEventListener("click", () => {
      if (!lastTranscriptText) return toast("暂无转写文本", "warn");
      downloadTextFile(el, trackBlobUrl, lastTranscriptText, `colabllm-transcript-${Date.now()}.txt`);
    });
    const sendAsrToChatBtn = onSendMediaToChat
      ? el("button", { type: "button", class: "btn small secondary" }, "将结果发往 Chat 追问")
      : null;
    if (sendAsrToChatBtn) {
      sendAsrToChatBtn.addEventListener("click", () => {
        if (!lastTranscriptText) return toast("暂无转写文本", "warn");
        const chatIn = document.getElementById("ck-chat-input");
        if (chatIn) {
          chatIn.value = `以下是音频转写结果，请帮我提炼要点：\n\n${lastTranscriptText}`;
        }
        onSendMediaToChat({ kind: "audio", name: studioState.bridgedAsrFile?.name || "transcribed-audio", url: studioState.lastTranscribedAudioUrl || "" });
      });
    }

    audioPlayer.addEventListener("timeupdate", () => {
      const now = Number(audioPlayer.currentTime || 0);
      for (const item of ctcButtonsMeta) {
        const active = now >= item.start && now <= item.end;
        item.btn.setAttribute("data-active", active ? "true" : "false");
      }
    });

    asrApplyBridgeFn = (media) => {
      if (!media) return;
      const file =
        media.file ||
        (media.blob ? new File([media.blob], media.name || "audio.wav", { type: media.blob.type || "audio/wav" }) : null);
      studioState.bridgedAsrFile = file ? { file, url: media.url, name: file.name } : null;
      if (media.url) {
        studioState.lastTranscribedAudioUrl = media.url;
        audioPlayer.src = media.url;
      }
      if (file) {
        bridgedBadge.hidden = false;
        bridgedBadge.textContent = `已桥接合成音频：${file.name}`;
      }
    };
    if (studioState.bridgedAsrFile) {
      asrApplyBridgeFn(studioState.bridgedAsrFile);
    }

    fileEl.addEventListener("change", () => {
      const f = fileEl.files?.[0];
      if (f) {
        studioState.bridgedAsrFile = null;
        bridgedBadge.hidden = true;
        const u = trackBlobUrl(URL.createObjectURL(f));
        studioState.lastTranscribedAudioUrl = u;
        audioPlayer.src = u;
      }
    });

    const localPresetsRow = el(
      "div",
      { class: "ck-presets-row" },
      ...ASR_PROMPT_PRESETS.map((p) => {
        const b = el("button", { type: "button", class: "btn small secondary" }, p.label);
        b.addEventListener("click", () => {
          promptEl.value = p.prompt;
        });
        return b;
      })
    );

    const loadPromptsBtn = feats.prompt_presets
      ? el("button", { type: "button", class: "btn small secondary" }, "拉取内置任务模板 (/api/ct/prompts)")
      : null;
    if (loadPromptsBtn) {
      loadPromptsBtn.addEventListener("click", async () => {
        try {
          const tgt = requireTarget();
          const resp = await proxyFetch(tgt.session_name, "api/ct/prompts");
          const data = await resp.json();
          const list = Array.isArray(data) ? data : data.prompts || [];
          presetsContainer.replaceChildren(
            ...list.map((p) => {
              const label = typeof p === "string" ? p : p.name || p.title || "Template";
              const text = typeof p === "string" ? p : p.prompt || p.text || label;
              const b = el("button", { type: "button", class: "btn small secondary" }, label);
              b.addEventListener("click", () => {
                promptEl.value = text;
              });
              return b;
            })
          );
        } catch (err) {
          toast(err.message, "error");
        }
      });
    }

    const driveBatchBtn =
      feats.drive_transcribe && allowDrive
        ? el("button", { type: "button", class: "btn small secondary" }, "Drive 长音频批量 (/api/ct/drive-transcribe)")
        : null;
    if (driveBatchBtn) {
      driveBatchBtn.addEventListener("click", async () => {
        try {
          const tgt = requireTarget();
          const drivePath = driveEl.value.trim();
          if (!drivePath) return toast("请输入 drive:// 路径", "warn");
          progressEl.hidden = false;
          driveBatchBtn.disabled = true;
          const resp = await proxyFetch(tgt.session_name, "api/ct/drive-transcribe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              drive_path: drivePath,
              capture_ctc_timestamps: Boolean(document.getElementById("ck-asr-ctc")?.checked),
            }),
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error || resp.statusText);
          lastTranscriptText = data.text || JSON.stringify(data, null, 2);
          transcriptOut.firstChild.textContent = lastTranscriptText;
          toast("已提交/完成 Drive 音频转写", "ok");
        } catch (err) {
          toast(err.message, "error");
        } finally {
          progressEl.hidden = true;
          driveBatchBtn.disabled = false;
        }
      });
    }

    const submitBtn = el(
      "button",
      { type: "submit", class: "btn primary" },
      isSalm ? "执行 SALM 音频转写 (/v1/chat/completions)" : "执行转写 (/v1/audio/transcriptions)"
    );
    const formEl = el(
      "form",
      { class: "card ck-studio-form-card" },
      el("h3", {}, isSalm ? "Nemotron SALM 音文转写与问答" : "OpenAI Whisper 音频转写"),
      el(
        "p",
        { class: "muted ck-cap-summary" },
        isSalm
          ? "Nemotron Transcribe 通过 /v1/chat/completions + audio_url 调用，并在响应顶层返回 ctc_timestamps。"
          : "通过 multipart/form-data 调用标准 /v1/audio/transcriptions 接口。"
      ),
      makeField("ck-asr-file", "上传本地音频", fileEl),
      bridgedBadge,
      allowDrive ? makeField("ck-asr-drive", "或 Google Drive 路径", driveEl) : null,
      makeField("ck-asr-prompt", "指令 / 提示词", promptEl),
      localPresetsRow,
      loadPromptsBtn || driveBatchBtn ? el("div", { class: "ck-presets-row" }, loadPromptsBtn, driveBatchBtn) : null,
      presetsContainer,
      isSalm ? ctcSwitch : null,
      isSalm ? thinkSwitch : null,
      el("div", { class: "ck-composer-right" }, progressEl, submitBtn)
    );

    formEl.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const tgt = requireTarget();
        const file = fileEl.files?.[0] || studioState.bridgedAsrFile?.file || null;
        const drivePath = allowDrive ? driveEl.value.trim() : "";
        if (!file && !drivePath) return toast("请选择本地音频或输入 drive:// 路径", "warn");

        progressEl.hidden = false;
        submitBtn.disabled = true;
        ctcButtonsMeta.length = 0;

        if (isSalm) {
          let audioUrl = drivePath;
          if (file) audioUrl = await fileToDataUrl(file);
          const resp = await proxyFetch(tgt.session_name, "v1/chat/completions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              model: defaultModel,
              messages: [
                {
                  role: "user",
                  content: [
                    { type: "text", text: promptEl.value.trim() || "Transcribe the audio accurately." },
                    { type: "audio_url", audio_url: { url: audioUrl } },
                  ],
                },
              ],
              mm_processor_kwargs: {
                capture_ctc_timestamps: Boolean(document.getElementById("ck-asr-ctc")?.checked),
              },
              chat_template_kwargs: {
                enable_thinking: Boolean(document.getElementById("ck-asr-think")?.checked),
              },
              stream: false,
            }),
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error?.message || data.error || resp.statusText);
          const msg = data.choices?.[0]?.message || {};
          lastTranscriptText = msg.content || JSON.stringify(data, null, 2);
          transcriptOut.firstChild.textContent = lastTranscriptText;

          const rawCtc = data.ctc_timestamps || data.timestamps || [];
          const segments = Array.isArray(rawCtc) ? rawCtc : rawCtc.words || rawCtc.segments || [];
          lastCtcSegments = segments;
          exportSrtBtn.hidden = !segments.length;
          ctcContainer.replaceChildren(
            ...(segments.length
              ? segments.map((seg) => {
                  const text = seg.word || seg.text || seg.label || "?";
                  const start = Number(seg.start ?? seg.start_time ?? 0);
                  const end = Number(seg.end ?? seg.end_time ?? start + 0.5);
                  const btn = el("button", { type: "button", class: "ck-ctc-btn" }, `${text} (${start.toFixed(2)}s)`);
                  btn.addEventListener("click", () => {
                    if (audioPlayer.src) {
                      audioPlayer.currentTime = start;
                      audioPlayer.play().catch(() => {});
                    }
                  });
                  ctcButtonsMeta.push({ btn, start, end });
                  return btn;
                })
              : [el("span", { class: "muted" }, "本次响应未包含可点击时间戳切片。")])
          );
        } else {
          if (!file) return toast("标准 /v1/audio/transcriptions 需要上传本地音频文件", "warn");
          const fd = new FormData();
          fd.append("file", file);
          fd.append("model", defaultModel);
          if (promptEl.value.trim()) fd.append("prompt", promptEl.value.trim());
          const resp = await proxyFetch(tgt.session_name, "v1/audio/transcriptions", { method: "POST", body: fd });
          const raw = await resp.text();
          if (!resp.ok) throw new Error(raw || resp.statusText);
          try {
            const parsed = JSON.parse(raw);
            lastTranscriptText = parsed.text || JSON.stringify(parsed, null, 2);
            lastCtcSegments = Array.isArray(parsed.segments) ? parsed.segments : [];
          } catch {
            lastTranscriptText = raw;
            lastCtcSegments = [];
          }
          transcriptOut.firstChild.textContent = lastTranscriptText;
          exportSrtBtn.hidden = !lastCtcSegments.length;
          ctcContainer.replaceChildren();
        }
      } catch (err) {
        toast(err.message, "error");
      } finally {
        progressEl.hidden = true;
        submitBtn.disabled = false;
      }
    });

    root.replaceChildren(
      el(
        "div",
        { class: "ck-studio-grid" },
        formEl,
        el(
          "div",
          { class: "card ck-studio-output-card" },
          el(
            "div",
            { class: "ck-output-header" },
            el("h3", {}, "音频播放与 CTC 时间戳联动"),
            el("div", { class: "ck-presets-row" }, makeCopyButton(() => lastTranscriptText, "复制文本"), exportTxtBtn, exportSrtBtn, sendAsrToChatBtn)
          ),
          audioPlayer,
          transcriptOut,
          el("strong", {}, "时间戳切片（点击跳转到对应秒数，播放时自动高亮当前片段）"),
          ctcContainer
        )
      )
    );
  }

  /* ===================== 4. EMBEDDINGS STUDIO ===================== */
  function renderEmbeddingsStudio() {
    const root = document.getElementById("ck-embeddings-root");
    if (!root) return;
    const defaultModel = getSelectedModel() || getSelectedProfile()?.default_model || "";

    const linesEl = el("textarea", {
      id: "ck-emb-lines",
      class: "ck-autosize",
      rows: "5",
      value: EMB_PRESETS[0].text,
    });
    const dimsEl = el("input", { id: "ck-emb-dims", type: "number", min: "1", max: "8192", placeholder: "留空使用默认维度" });
    const progressEl = el("progress", { class: "ck-progress", "aria-label": "向量计算进度", hidden: "" });
    const summaryEl = el("p", { class: "muted" }, "每行作为一条独立文本输入 /v1/embeddings，计算余弦相似度矩阵与 Query Top-K 排序。");
    const rankWrap = el("div", { class: "ck-rank-list" });
    const matrixWrap = el("div", { class: "ck-table-wrap" });
    const rawWrap = el("details", { class: "ck-accordion" }, el("summary", {}, "原始向量摘要 JSON"), el("div", { class: "ck-acc-body" }));

    let lastVectorsPayload = null;
    const exportJsonBtn = el("button", { type: "button", class: "btn small secondary" }, "导出完整 JSON");
    exportJsonBtn.addEventListener("click", () => {
      if (!lastVectorsPayload) return toast("请先计算 Embeddings", "warn");
      downloadTextFile(el, trackBlobUrl, JSON.stringify(lastVectorsPayload, null, 2), `colabllm-embeddings-${Date.now()}.json`, "application/json");
    });

    const presetRow = el(
      "div",
      { class: "ck-presets-row" },
      ...EMB_PRESETS.map((p) => {
        const b = el("button", { type: "button", class: "btn small secondary" }, p.label);
        b.addEventListener("click", () => {
          linesEl.value = p.text;
        });
        return b;
      })
    );

    const submitBtn = el("button", { type: "submit", class: "btn primary" }, "计算 Embeddings");
    const formEl = el(
      "form",
      { class: "card ck-studio-form-card" },
      el("h3", {}, "OpenAI /v1/embeddings 向量与相似度测试"),
      presetRow,
      makeField("ck-emb-lines", "待编码文本（每行一条；Top-K 检索以第 1 行为 Query）", linesEl),
      makeField("ck-emb-dims", "可选降维 (dimensions)", dimsEl),
      el("div", { class: "ck-composer-right" }, progressEl, submitBtn)
    );

    formEl.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const tgt = requireTarget();
        const inputs = linesEl.value
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean);
        if (!inputs.length) return toast("至少输入一行文本", "warn");

        progressEl.hidden = false;
        submitBtn.disabled = true;
        const body = { model: getSelectedModel() || defaultModel, input: inputs };
        if (dimsEl.value.trim()) body.dimensions = Number(dimsEl.value.trim());

        const t0 = performance.now();
        const resp = await proxyFetch(tgt.session_name, "v1/embeddings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await resp.json();
        const dt = Math.round(performance.now() - t0);
        if (!resp.ok) throw new Error(data.error?.message || data.error || resp.statusText);
        lastVectorsPayload = data;

        const vectors = (data.data || []).sort((a, b) => (a.index ?? 0) - (b.index ?? 0)).map((d) => d.embedding || []);
        const dims = vectors[0]?.length || 0;
        const norms = vectors.map((v) => Math.hypot(...v).toFixed(4));
        summaryEl.textContent = `返回 ${vectors.length} 条向量，维度 D=${dims}，L2 范数=${norms[0] || "-"}，耗时 ${dt}ms，Tokens=${data.usage?.total_tokens ?? "n/a"}`;

        if (inputs.length >= 2 && vectors[0]?.length) {
          const ranked = inputs
            .slice(1)
            .map((txt, idx) => ({
              idx: idx + 2,
              text: txt,
              sim: cosineSimilarity(vectors[0], vectors[idx + 1] || []),
            }))
            .sort((a, b) => b.sim - a.sim);
          rankWrap.replaceChildren(
            el("strong", {}, `RAG 首行 Query 相关性 Top-${ranked.length}：`),
            ...ranked.map((item, rankIdx) =>
              el(
                "div",
                { class: "ck-rank-row" },
                el("span", {}, `#${item.idx}: ${item.text.slice(0, 64)}`),
                el("span", { class: rankIdx === 0 ? "badge ok ck-mono" : "badge neutral ck-mono" }, item.sim.toFixed(4))
              )
            )
          );
        } else {
          rankWrap.replaceChildren();
        }

        const headRow = el("tr", {}, el("th", {}, "#"), ...inputs.map((t, i) => el("th", {}, `#${i + 1}: ${t.slice(0, 14)}`)));
        const bodyRows = inputs.map((t, r) =>
          el(
            "tr",
            {},
            el("th", { scope: "row" }, `#${r + 1}: ${t.slice(0, 14)}`),
            ...inputs.map((_, c) => {
              const sim = vectors[r] && vectors[c] ? cosineSimilarity(vectors[r], vectors[c]) : 0;
              const td = el("td", { class: "ck-matrix-cell" }, sim.toFixed(4));
              td.style.setProperty("--sim-alpha", `${Math.round(Math.max(0, Math.min(1, sim)) * 28)}%`);
              return td;
            })
          )
        );
        matrixWrap.replaceChildren(el("table", { class: "ck-data-table" }, el("thead", {}, headRow), el("tbody", {}, ...bodyRows)));
        rawWrap.lastChild.replaceChildren(
          renderCodeBlock(
            el,
            "前 8 维预览",
            JSON.stringify(
              vectors.map((v, i) => ({ index: i, norm: norms[i], head8: v.slice(0, 8) })),
              null,
              2
            )
          )
        );
      } catch (err) {
        toast(err.message, "error");
      } finally {
        progressEl.hidden = true;
        submitBtn.disabled = false;
      }
    });

    root.replaceChildren(
      el(
        "div",
        { class: "ck-studio-grid" },
        formEl,
        el(
          "div",
          { class: "card ck-studio-output-card" },
          el(
            "div",
            { class: "ck-output-header" },
            el("h3", {}, "余弦相似度矩阵"),
            el("div", { class: "ck-presets-row" }, makeCopyButton(() => JSON.stringify(lastVectorsPayload || {}, null, 2), "复制 JSON"), exportJsonBtn)
          ),
          summaryEl,
          rankWrap,
          matrixWrap,
          rawWrap
        )
      )
    );
  }

  /* ===================== 5. SPECIALIZED NATIVE CONSOLE ===================== */
  function renderSpecializedStudio() {
    const root = document.getElementById("ck-specialized-root");
    if (!root) return;
    const target = getSelectedTarget();
    const profile = getSelectedProfile();
    const svc = target?.service || profile?.default_model || "";
    const statusBadge = el("span", { class: "badge neutral" }, "待命");
    const latencyBadge = el("span", { class: "muted ck-mono" }, "");
    const outputCode = el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, "原生 API 执行结果将显示在这里。"));
    const mediaBox = el("div", { class: "ck-specialized-rich" });

    let lastCallSpec = profile?.endpoint_presets?.[0] || { method: "GET", path: profile?.health_path || "health" };
    let lastRawOutput = "";

    async function callAndShow(subpath, fetchOpts = {}, note = "") {
      const tgt = requireTarget();
      const method = (fetchOpts.method || "GET").toUpperCase();
      lastCallSpec = { method, path: subpath, body: fetchOpts.body };
      if (note) outputCode.firstChild.textContent = note;
      const t0 = performance.now();
      const resp = await proxyFetch(tgt.session_name, subpath, fetchOpts);
      const dt = Math.round(performance.now() - t0);
      const ct = (resp.headers.get("Content-Type") || "").toLowerCase();

      statusBadge.textContent = `HTTP ${resp.status}`;
      statusBadge.className = resp.ok ? "badge ok" : "badge danger";
      latencyBadge.textContent = `${dt} ms · ${ct.split(";")[0] || "-"}`;

      if (!resp.ok) {
        const msg = await resp.text();
        lastRawOutput = `HTTP ${resp.status}\n${msg}`;
        outputCode.firstChild.textContent = lastRawOutput;
        throw new Error(msg || `HTTP ${resp.status}`);
      }
      if (method === "HEAD") {
        lastRawOutput = Array.from(resp.headers.entries())
          .map(([k, v]) => `${k}: ${v}`)
          .join("\n") || `HTTP ${resp.status} HEAD OK (${dt} ms)`;
        outputCode.firstChild.textContent = lastRawOutput;
        return { status: resp.status, headers: Object.fromEntries(resp.headers.entries()), dt };
      }
      if (ct.startsWith("image/")) {
        const u = trackBlobUrl(URL.createObjectURL(await resp.blob()));
        mediaBox.replaceChildren(el("div", { class: "ck-media-card" }, el("img", { src: u, alt: subpath })));
        lastRawOutput = `已加载图像资源 (${subpath})`;
        outputCode.firstChild.textContent = lastRawOutput;
        return u;
      }
      if (ct.startsWith("video/")) {
        const u = trackBlobUrl(URL.createObjectURL(await resp.blob()));
        mediaBox.replaceChildren(el("div", { class: "ck-media-card" }, el("video", { controls: "", src: u })));
        lastRawOutput = `已加载视频资源 (${subpath})`;
        outputCode.firstChild.textContent = lastRawOutput;
        return u;
      }
      const txt = await resp.text();
      try {
        const parsed = JSON.parse(txt);
        lastRawOutput = JSON.stringify(parsed, null, 2);
        outputCode.firstChild.textContent = lastRawOutput;
        return parsed;
      } catch {
        lastRawOutput = txt;
        outputCode.firstChild.textContent = txt;
        return txt;
      }
    }

    const controls = [el("h3", {}, `${svc || "未选择实例"} · 原生 API 控制台`)];

    if (svc === "kaggle-web-probe") {
      controls.push(el("p", { class: "muted ck-cap-summary" }, "Kaggle 纯标准库 HTTP 探针：用于验证 Caddy Basic Auth、FRP/Cloudflare 隧道连通性、运行时磁盘与 /kaggle/working/.ct-cache 制品清单。"));
      const runProbeBtn = el("button", { type: "button", class: "btn small primary" }, "运行端到端健康与缓存探针 (GET /health)");
      const headProbeBtn = el("button", { type: "button", class: "btn small secondary" }, "轻量探活 (HEAD /health)");
      const htmlPageBtn = el("button", { type: "button", class: "btn small secondary" }, "抓取状态网页源码 (GET /)");
      const boundaryBtn = el("button", { type: "button", class: "btn small secondary" }, "验证非 OpenAI 404 边界 (/v1/models)");

      runProbeBtn.addEventListener("click", async () => {
        try {
          const data = await callAndShow("health");
          if (data && typeof data === "object") {
            const arts = Array.isArray(data.cached_artifacts) ? data.cached_artifacts : [];
            const kpis = el(
              "div",
              { class: "ck-probe-kpis" },
              el("div", { class: "ck-kpi-tile" }, el("span", { class: "ck-kpi-label" }, "服务状态"), el("span", { class: "ck-kpi-value" }, String(data.status || "ok").toUpperCase())),
              el("div", { class: "ck-kpi-tile" }, el("span", { class: "ck-kpi-label" }, "运行时长"), el("span", { class: "ck-kpi-value" }, `${data.uptime_seconds ?? 0}s`)),
              el("div", { class: "ck-kpi-tile" }, el("span", { class: "ck-kpi-label" }, "可用磁盘"), el("span", { class: "ck-kpi-value" }, `${data.free_mb ?? 0} MB`)),
              el("div", { class: "ck-kpi-tile" }, el("span", { class: "ck-kpi-label" }, "已缓存制品"), el("span", { class: "ck-kpi-value" }, String(arts.length)))
            );
            const table = el(
              "div",
              { class: "ck-table-wrap" },
              el(
                "table",
                { class: "ck-data-table" },
                el("thead", {}, el("tr", {}, el("th", {}, "制品名称"), el("th", {}, "版本"), el("th", {}, "SHA-256"), el("th", {}, "校验时间"))),
                el(
                  "tbody",
                  {},
                  ...(arts.length
                    ? arts.map((a) => el("tr", {}, el("td", {}, a.artifact || "-"), el("td", {}, a.version || "-"), el("td", { class: "ck-mono" }, a.sha256 || "-"), el("td", {}, a.verified_at || "-")))
                    : [el("tr", {}, el("td", { colspan: "4", class: "muted" }, `缓存目录 ${data.cache_dir || "-"} 暂无制品清单`))])
                )
              )
            );
            mediaBox.replaceChildren(kpis, table);
          }
        } catch (err) {
          toast(err.message, "error");
        }
      });
      headProbeBtn.addEventListener("click", () => callAndShow("health", { method: "HEAD" }).catch((e) => toast(e.message, "error")));
      htmlPageBtn.addEventListener("click", () => callAndShow("").catch((e) => toast(e.message, "error")));
      boundaryBtn.addEventListener("click", () =>
        callAndShow("v1/models").catch(() => {
          toast("已确认 /v1/models 按预期返回 404 (not_supported)", "ok");
        })
      );
      controls.push(el("div", { class: "ck-presets-row" }, runProbeBtn, headProbeBtn, htmlPageBtn, boundaryBtn));
    } else if (svc === "ollama") {
      const pullName = el("input", { id: "ck-ol-model", type: "text", autocorrect: "off", autocapitalize: "none", spellcheck: "false", placeholder: "例如 qwen3:8b 或 nomic-embed-text" });
      const pullProgress = el("progress", { class: "ck-progress", "aria-label": "Ollama 模型拉取进度", max: "100", value: "0", hidden: "" });
      const renderModelPills = (list, label) => {
        if (!Array.isArray(list) || !list.length) return;
        mediaBox.replaceChildren(
          el("strong", {}, label),
          el(
            "div",
            { class: "ck-presets-row" },
            ...list.map((m) => {
              const name = m.name || m.model || String(m);
              const b = el("button", { type: "button", class: "btn small secondary" }, `选用: ${name}`);
              b.addEventListener("click", () => {
                pullName.value = name;
                const ckModel = document.getElementById("ck-model");
                if (ckModel) ckModel.value = name;
                toast(`已填入模型 ${name}`, "ok");
              });
              return b;
            })
          )
        );
      };
      const makeBtn = (label, fn) => {
        const b = el("button", { type: "button", class: "btn small secondary" }, label);
        b.addEventListener("click", () => fn().catch((err) => toast(err.message, "error")));
        return b;
      };
      controls.push(
        el(
          "div",
          { class: "ck-presets-row" },
          makeBtn("版本 /api/version", () => callAndShow("api/version")),
          makeBtn("已安装模型 /api/tags", async () => {
            const d = await callAndShow("api/tags");
            renderModelPills(d?.models, "点击选择已安装模型：");
          }),
          makeBtn("显存驻留 /api/ps", async () => {
            const d = await callAndShow("api/ps");
            renderModelPills(d?.models, "当前驻留显存模型：");
          }),
          makeBtn("查看详情 /api/show", () => {
            const m = pullName.value.trim() || getSelectedModel();
            if (!m) throw new Error("请填写模型名");
            return callAndShow("api/show", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: m }) });
          }),
          makeBtn("删除模型 /api/delete", () => {
            const m = pullName.value.trim();
            if (!m) throw new Error("请填写要删除的模型名");
            return callAndShow("api/delete", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: m }) });
          })
        ),
        makeField("ck-ol-model", "模型名称 (用于 pull/show/delete)", pullName),
        pullProgress
      );
      const pullBtn = el("button", { type: "button", class: "btn small primary" }, "流式拉取模型 (/api/pull NDJSON)");
      pullBtn.addEventListener("click", async () => {
        try {
          const tgt = requireTarget();
          const m = pullName.value.trim();
          if (!m) return toast("请输入模型名称", "warn");
          pullProgress.hidden = false;
          pullBtn.disabled = true;
          pullProgress.removeAttribute("value");
          const resp = await proxyFetch(tgt.session_name, "api/pull", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model: m, stream: true }),
          });
          if (!resp.ok || !resp.body) throw new Error(await resp.text());
          const reader = resp.body.getReader();
          const dec = new TextDecoder();
          let buf = "";
          const lines = [];
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            const parts = buf.split("\n");
            buf = parts.pop() || "";
            for (const line of parts) {
              if (!line.trim()) continue;
              lines.push(line);
              try {
                const evt = JSON.parse(line);
                if (evt.total > 0 && typeof evt.completed === "number") {
                  pullProgress.max = evt.total;
                  pullProgress.value = evt.completed;
                }
              } catch {}
              lastRawOutput = lines.slice(-20).join("\n");
              outputCode.firstChild.textContent = lastRawOutput;
            }
          }
          toast(`模型 ${m} 拉取流程结束`, "ok");
        } catch (err) {
          toast(err.message, "error");
        } finally {
          pullProgress.hidden = true;
          pullBtn.disabled = false;
        }
      });
      controls.push(pullBtn);
    } else if (svc === "comfyui") {
      const wfJson = el("textarea", {
        id: "ck-cf-workflow",
        class: "ck-autosize ck-mono",
        rows: "5",
        autocorrect: "off",
        autocapitalize: "none",
        spellcheck: "false",
        placeholder: '{"3": {"class_type": "KSampler", "inputs": {...}}} (ComfyUI API 格式 prompt JSON)',
      });
      const pullUrl = el("input", { id: "ck-cf-pull-url", type: "url", autocorrect: "off", placeholder: "输入工作流或模型下载 URL..." });
      const uploadFile = el("input", { id: "ck-cf-upload", type: "file", accept: "image/*" });
      const sampleWfBtn = el("button", { type: "button", class: "btn small secondary" }, "填入示例 Prompt 骨架");
      sampleWfBtn.addEventListener("click", () => {
        wfJson.value = JSON.stringify(
          {
            "3": { class_type: "KSampler", inputs: { seed: 42, steps: 20, cfg: 7, sampler_name: "euler", scheduler: "normal", denoise: 1 } },
          },
          null,
          2
        );
      });
      const pullWfBtn = el("button", { type: "button", class: "btn small secondary" }, "拉取远端资源 (/api/ct/pull-workflow)");
      pullWfBtn.addEventListener("click", () => {
        const u = pullUrl.value.trim();
        if (!u) return toast("请输入工作流或资源 URL", "warn");
        return callAndShow("api/ct/pull-workflow", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: u }),
        }).catch((e) => toast(e.message, "error"));
      });
      controls.push(
        el(
          "div",
          { class: "ck-presets-row" },
          ...[["GPU/系统 /system_stats", "system_stats"], ["队列 /queue", "queue"], ["SSD 缓存 /api/ct/ssd-status", "api/ct/ssd-status"], ["节点清单 /object_info", "object_info"]].map(
            ([label, p]) => {
              const b = el("button", { type: "button", class: "btn small secondary" }, label);
              b.addEventListener("click", () => callAndShow(p).catch((e) => toast(e.message, "error")));
              return b;
            }
          ),
          sampleWfBtn
        ),
        makeField("ck-cf-upload", "上传输入图到 ComfyUI (/upload/image)", uploadFile),
        makeField("ck-cf-workflow", "API Workflow JSON (/prompt)", wfJson),
        makeField("ck-cf-pull-url", "远端资源 URL (/api/ct/pull-workflow)", pullUrl)
      );
      const submitWf = el("button", { type: "button", class: "btn small primary" }, "提交 Workflow 到 /prompt");
      submitWf.addEventListener("click", async () => {
        try {
          const parsed = JSON.parse(wfJson.value.trim() || "{}");
          const res = await callAndShow("prompt", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt: parsed }),
          });
          if (res?.prompt_id) toast(`已提交 ComfyUI Prompt ID: ${res.prompt_id}`, "ok");
        } catch (err) {
          toast(err.message, "error");
        }
      });
      const uploadBtn = el("button", { type: "button", class: "btn small secondary" }, "执行上传图片");
      uploadBtn.addEventListener("click", async () => {
        try {
          const f = uploadFile.files?.[0];
          if (!f) return toast("请选择图片", "warn");
          const fd = new FormData();
          fd.append("image", f);
          await callAndShow("upload/image", { method: "POST", body: fd });
        } catch (err) {
          toast(err.message, "error");
        }
      });
      const interruptBtn = el("button", { type: "button", class: "btn small danger" }, "中断运行 (/interrupt)");
      interruptBtn.addEventListener("click", () => callAndShow("interrupt", { method: "POST", body: "{}" }).catch((e) => toast(e.message, "error")));
      controls.push(el("div", { class: "ck-presets-row" }, submitWf, uploadBtn, pullWfBtn, interruptBtn));
    } else if (svc === "unlimited-ocr") {
      const fileEl = el("input", { id: "ck-ocr-file", type: "file", accept: ".pdf,image/*" });
      const driveEl = el("input", { id: "ck-ocr-drive", type: "text", autocorrect: "off", placeholder: "或输入 drive://... 文件夹/PDF 路径" });
      const jobIdEl = el("input", { id: "ck-ocr-jobid", type: "text", autocorrect: "off", placeholder: "输入 job_id 查询进度或获取结果" });
      const upProgress = el("progress", { class: "ck-progress", "aria-label": "OCR 分片上传进度", max: "1", value: "0", hidden: "" });
      const uploadAndRunBtn = el("button", { type: "button", class: "btn small primary" }, "SHA-256 分片上传并识别");
      uploadAndRunBtn.addEventListener("click", async () => {
        try {
          requireTarget();
          const file = fileEl.files?.[0];
          uploadAndRunBtn.disabled = true;
          if (file) {
            const buf = await file.arrayBuffer();
            const chunkSize = 512 * 1024;
            const totalChunks = Math.max(1, Math.ceil(buf.byteLength / chunkSize));
            upProgress.max = String(totalChunks);
            upProgress.value = "0";
            upProgress.hidden = false;
            const uploadId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
            for (let idx = 0; idx < totalChunks; idx += 1) {
              const slice = buf.slice(idx * chunkSize, (idx + 1) * chunkSize);
              const b64 = arrayBufferToBase64(slice);
              const sha = await sha256Hex(slice);
              await callAndShow(
                "api/upload",
                {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    upload_id: uploadId,
                    filename: file.name,
                    chunk_index: idx,
                    total_chunks: totalChunks,
                    data: b64,
                    sha256: sha,
                  }),
                },
                `正在上传分片 ${idx + 1}/${totalChunks}...`
              );
              upProgress.value = String(idx + 1);
            }
            const job = await callAndShow("api/jobs", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ upload_id: uploadId, filename: file.name }),
            });
            if (job?.job_id || job?.id) jobIdEl.value = job.job_id || job.id;
          } else if (driveEl.value.trim()) {
            const job = await callAndShow("api/jobs", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ source: driveEl.value.trim() }),
            });
            if (job?.job_id || job?.id) jobIdEl.value = job.job_id || job.id;
          } else {
            return toast("请选择 PDF/图片 或填写 Drive 路径", "warn");
          }
        } catch (err) {
          toast(err.message, "error");
        } finally {
          upProgress.hidden = true;
          uploadAndRunBtn.disabled = false;
        }
      });
      const checkBtn = el("button", { type: "button", class: "btn small secondary" }, "查询任务 (/api/jobs/{id})");
      checkBtn.addEventListener("click", () => {
        const id = jobIdEl.value.trim();
        if (!id) return toast("请输入 job_id", "warn");
        return callAndShow(`api/jobs/${encodeURIComponent(id)}`).catch((e) => toast(e.message, "error"));
      });
      const autoPollBtn = el("button", { type: "button", class: "btn small secondary" }, "轮询直至完成并取回结果");
      autoPollBtn.addEventListener("click", async () => {
        const id = jobIdEl.value.trim();
        if (!id) return toast("请输入 job_id", "warn");
        autoPollBtn.disabled = true;
        try {
          for (let i = 0; i < 60; i += 1) {
            const st = await callAndShow(`api/jobs/${encodeURIComponent(id)}`, {}, `第 ${i + 1} 次轮询 OCR 任务 ${id}...`);
            const s = String(st?.status || "").toLowerCase();
            if (s === "completed" || s === "succeeded") {
              await callAndShow("api/results", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ job_id: id }),
              });
              toast(`OCR 任务 ${id} 已完成并加载结果`, "ok");
              return;
            }
            if (s === "failed" || s === "cancelled") {
              throw new Error(st?.error || `OCR 任务状态为 ${s}`);
            }
            await new Promise((r) => setTimeout(r, 2000));
          }
          toast("轮询超时，请稍后手动查询", "warn");
        } catch (err) {
          toast(err.message, "error");
        } finally {
          autoPollBtn.disabled = false;
        }
      });
      const resBtn = el("button", { type: "button", class: "btn small secondary" }, "获取识别结果 (/api/results)");
      resBtn.addEventListener("click", () =>
        callAndShow("api/results", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ job_id: jobIdEl.value.trim() }),
        }).catch((e) => toast(e.message, "error"))
      );
      const listBtn = el("button", { type: "button", class: "btn small secondary" }, "Drive 目录 (/api/drive-list)");
      listBtn.addEventListener("click", () =>
        callAndShow("api/drive-list", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: driveEl.value.trim() }) }).catch((e) => toast(e.message, "error"))
      );
      const dlMdBtn = el("button", { type: "button", class: "btn small secondary" }, "导出 Markdown (.md)");
      dlMdBtn.addEventListener("click", () => {
        if (!lastRawOutput) return toast("暂无 OCR 结果", "warn");
        downloadTextFile(el, trackBlobUrl, lastRawOutput, `ocr-result-${Date.now()}.md`, "text/markdown;charset=utf-8");
      });
      controls.push(
        makeField("ck-ocr-file", "本地 PDF / 扫描件（<=512 KiB 自动分片）", fileEl),
        upProgress,
        makeField("ck-ocr-drive", "Drive 路径", driveEl),
        makeField("ck-ocr-jobid", "任务 ID", jobIdEl),
        el("div", { class: "ck-presets-row" }, uploadAndRunBtn, listBtn, checkBtn, autoPollBtn, resBtn, dlMdBtn)
      );
    } else if (svc === "unsloth-studio") {
      const loginStatus = el("p", { class: "muted ck-cap-summary" }, studioState.unslothJwt ? "已缓存会话 JWT（自动通过 X-ColabLLM-Proxy-Authorization 注入）" : "未登录 Unsloth JWT");
      const autoLoginBtn = el("button", { type: "button", class: "btn small primary" }, "同源自动注入凭据登录 (/api/auth/login)");
      autoLoginBtn.addEventListener("click", async () => {
        try {
          const data = await callAndShow("api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
          const token = data?.access_token || data?.token || data?.jwt;
          if (token) {
            studioState.unslothJwt = token;
            loginStatus.textContent = `已获取 JWT (${String(token).slice(0, 18)}...)，后续请求将自动携带该凭证`;
            toast("Unsloth Studio JWT 登录成功", "ok");
          }
        } catch (err) {
          toast(err.message, "error");
        }
      });
      const authStatusBtn = el("button", { type: "button", class: "btn small secondary" }, "认证状态 (/api/auth/status)");
      authStatusBtn.addEventListener("click", () =>
        callAndShow("api/auth/status", {
          headers: studioState.unslothJwt ? { "X-ColabLLM-Proxy-Authorization": `Bearer ${studioState.unslothJwt}` } : {},
        }).catch((e) => toast(e.message, "error"))
      );
      const healthBtn = el("button", { type: "button", class: "btn small secondary" }, "健康检查 (/api/health)");
      healthBtn.addEventListener("click", () => callAndShow("api/health").catch((e) => toast(e.message, "error")));
      controls.push(loginStatus, el("div", { class: "ck-presets-row" }, autoLoginBtn, authStatusBtn, healthBtn));
    } else if (svc === "r2-migrate") {
      const jobIdEl = el("input", { id: "ck-r2-job", type: "text", autocorrect: "off", placeholder: "输入或从下方列表点击填充任务 ID" });
      const jobsBtn = el("button", { type: "button", class: "btn small primary" }, "加载迁移任务 (/api/jobs)");
      jobsBtn.addEventListener("click", async () => {
        try {
          const data = await callAndShow("api/jobs");
          const jobs = Array.isArray(data) ? data : data?.jobs || [];
          if (jobs.length) {
            mediaBox.replaceChildren(
              el("strong", {}, `迁移任务概览（共 ${jobs.length} 个，点击快速填入 ID）：`),
              ...jobs.slice(0, 12).map((j) => {
                const jid = j.id || j.job_id;
                const fillBtn = el("button", { type: "button", class: "btn small secondary" }, "选中 ID");
                fillBtn.addEventListener("click", () => {
                  jobIdEl.value = jid;
                  toast(`已选中迁移任务 ${jid}`, "ok");
                });
                return el(
                  "div",
                  { class: "ck-rank-row" },
                  el("span", { class: "ck-mono" }, `${jid} · ${j.status || "-"}`),
                  fillBtn
                );
              })
            );
          }
        } catch (err) {
          toast(err.message, "error");
        }
      });
      controls.push(
        el(
          "div",
          { class: "ck-presets-row" },
          jobsBtn,
          ...[["健康检查", "GET", "health"], ["凭证列表", "GET", "api/credentials"], ["Drive 目录", "POST", "api/drive-list", {}]].map(
            ([label, m, p, body]) => {
              const b = el("button", { type: "button", class: "btn small secondary" }, label);
              b.addEventListener("click", () =>
                callAndShow(p, {
                  method: m,
                  headers: body ? { "Content-Type": "application/json" } : {},
                  body: body ? JSON.stringify(body) : undefined,
                }).catch((e) => toast(e.message, "error"))
              );
              return b;
            }
          )
        ),
        makeField("ck-r2-job", "目标迁移任务 ID", jobIdEl),
        el(
          "div",
          { class: "ck-presets-row" },
          ...["pause", "resume", "cleanup", "cancel"].map((act) => {
            const b = el("button", { type: "button", class: act === "cancel" ? "btn small danger" : "btn small secondary" }, act);
            b.addEventListener("click", () => {
              const id = jobIdEl.value.trim();
              if (!id) return toast("请输入 job_id", "warn");
              return callAndShow(`api/jobs/${encodeURIComponent(id)}/${act}`, { method: "POST", body: "{}" }).catch((e) => toast(e.message, "error"));
            });
            return b;
          })
        )
      );
    } else if (svc === "openjutsu" || svc === "longcat-video" || svc === "longcat-avatar") {
      const promptIn = el("textarea", { id: "ck-vid-prompt", class: "ck-autosize", rows: "3", placeholder: "输入视频/数字人生成提示词..." });
      const runBtn = el("button", { type: "button", class: "btn small primary" }, "提交视频生成任务");
      const jobIn = el("input", { id: "ck-vid-job", type: "text", autocorrect: "off", placeholder: "任务 / event_id" });
      const pollBtn = el("button", { type: "button", class: "btn small secondary" }, "查询状态 / 拉取结果视频");
      runBtn.addEventListener("click", async () => {
        try {
          if (svc === "openjutsu") {
            const fd = new FormData();
            fd.append("prompt", promptIn.value.trim());
            const res = await callAndShow("api/jobs", { method: "POST", body: fd });
            if (res?.id || res?.job_id) jobIn.value = res.id || res.job_id;
          } else if (svc === "longcat-avatar") {
            const res = await callAndShow("jobs", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ prompt: promptIn.value.trim() }),
            });
            if (res?.job_id || res?.id) jobIn.value = res.job_id || res.id;
          } else {
            const res = await callAndShow("gradio_api/call/generate", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ data: [promptIn.value.trim()] }),
            });
            if (res?.event_id) jobIn.value = res.event_id;
          }
        } catch (err) {
          toast(err.message, "error");
        }
      });
      pollBtn.addEventListener("click", async () => {
        try {
          const id = jobIn.value.trim();
          if (!id) return toast("请填写任务/Event ID", "warn");
          if (svc === "openjutsu") {
            await callAndShow(`api/jobs/${encodeURIComponent(id)}`);
          } else if (svc === "longcat-avatar") {
            const st = await callAndShow(`jobs/${encodeURIComponent(id)}`);
            if (st?.status === "completed") {
              const tgt = requireTarget();
              mediaBox.replaceChildren(
                el("div", { class: "ck-media-card" }, el("video", { controls: "", src: proxyUrl(tgt.session_name, `jobs/${encodeURIComponent(id)}/video`) }))
              );
            }
          } else {
            await callAndShow(`gradio_api/call/generate/${encodeURIComponent(id)}`);
          }
        } catch (err) {
          toast(err.message, "error");
        }
      });
      controls.push(
        el(
          "div",
          { class: "ck-presets-row" },
          ...((profile?.endpoint_presets || []).map((preset) => {
            const b = el("button", { type: "button", class: "btn small secondary" }, preset.label);
            b.addEventListener("click", () =>
              callAndShow(preset.path, {
                method: preset.method || "GET",
                headers: preset.body ? { "Content-Type": "application/json" } : {},
                body: preset.body ? JSON.stringify(preset.body) : undefined,
              }).catch((e) => toast(e.message, "error"))
            );
            return b;
          }))
        ),
        makeField("ck-vid-prompt", "提示词 Prompt", promptIn),
        makeField("ck-vid-job", "任务 / Event ID", jobIn),
        el("div", { class: "ck-presets-row" }, runBtn, pollBtn)
      );
    } else {
      const presets = profile?.endpoint_presets || [];
      controls.push(
        el("p", { class: "muted" }, "该运行时已自动识别以下端点预设，点击即可直接通过同源代理执行或转至「同源 HTTP 探针」精细编辑："),
        el(
          "div",
          { class: "ck-presets-row" },
          ...(presets.length
            ? presets.map((p) => {
                const b = el("button", { type: "button", class: "btn small secondary" }, `${p.method} /${p.path} (${p.label})`);
                b.addEventListener("click", () =>
                  callAndShow(p.path, {
                    method: p.method || "GET",
                    headers: p.body ? { "Content-Type": "application/json" } : {},
                    body: p.body ? JSON.stringify(p.body) : undefined,
                  }).catch((e) => toast(e.message, "error"))
                );
                return b;
              })
            : [el("span", { class: "muted" }, "暂无预设端点，请使用「同源 HTTP 探针」面板自由调试。")])
        )
      );
    }

    const inspectBtn = onInspectEndpoint
      ? el("button", { type: "button", class: "btn small secondary" }, "转至 HTTP 探针精调")
      : null;
    if (inspectBtn) {
      inspectBtn.addEventListener("click", () => onInspectEndpoint(lastCallSpec));
    }

    root.replaceChildren(
      el(
        "div",
        { class: "ck-studio-grid" },
        el("div", { class: "card ck-studio-form-card" }, ...controls),
        el(
          "div",
          { class: "card ck-studio-output-card" },
          el(
            "div",
            { class: "ck-output-header" },
            el("div", { class: "ck-presets-row" }, el("h3", {}, "原生 API 输出"), statusBadge, latencyBadge),
            el("div", { class: "ck-presets-row" }, makeCopyButton(() => lastRawOutput, "复制输出"), inspectBtn)
          ),
          mediaBox,
          outputCode
        )
      )
    );
  }

  /* ===================== 6. UNIVERSAL HTTP INSPECTOR ===================== */
  let inspectorPrefillFn = null;

  function renderInspectorStudio() {
    const root = document.getElementById("ck-inspector-root");
    if (!root) return;
    const profile = getSelectedProfile();
    const presets = profile?.endpoint_presets || [
      { label: "模型列表", method: "GET", path: "v1/models" },
      { label: "健康检查", method: "GET", path: "health" },
    ];

    const methodSel = el(
      "select",
      { id: "ck-http-method" },
      ...["GET", "HEAD", "POST", "PUT", "DELETE"].map((m) => el("option", { value: m }, m))
    );
    const pathInput = el("input", {
      id: "ck-http-path",
      type: "text",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      value: presets[0]?.path ?? "v1/models",
    });
    const customAuthInput = el("input", {
      id: "ck-http-auth",
      type: "text",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      value: studioState.unslothJwt ? `Bearer ${studioState.unslothJwt}` : "",
      placeholder: "留空则由本机后端自动注入 state.json 凭据；填值则覆盖 Authorization",
    });
    const bodyTextarea = el("textarea", {
      id: "ck-http-body",
      class: "ck-autosize ck-mono",
      rows: "5",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: '{"model": "..."}',
    });
    const mpContainer = el("div", { class: "ck-studio-output-card", hidden: "" });
    const bodyFieldWrap = makeField("ck-http-body", "请求体内容", bodyTextarea);

    const bmodeCtrl = makeModeGroup(
      "ck-http-bmode",
      "请求体类型",
      [
        ["json", "JSON"],
        ["multipart", "Multipart FormData"],
        ["text", "Raw Text"],
        ["none", "无请求体"],
      ],
      "json",
      syncInspectorBodyUi
    );

    function syncInspectorBodyUi() {
      const m = methodSel.value;
      const hasBodyMethod = m === "POST" || m === "PUT";
      const bm = bmodeCtrl.getValue();
      bmodeCtrl.group.hidden = !hasBodyMethod;
      mpContainer.hidden = !hasBodyMethod || bm !== "multipart";
      bodyFieldWrap.hidden = !hasBodyMethod || bm === "multipart" || bm === "none";
    }

    function rebuildMultipartRows() {
      const rows = studioState.inspectorMultipartRows.map((row, idx) => {
        const kIn = el("input", { type: "text", value: row.key, autocorrect: "off", placeholder: "字段名" });
        kIn.addEventListener("input", () => {
          row.key = kIn.value;
        });
        const tSel = el("select", {}, el("option", { value: "file" }, "文件"), el("option", { value: "text" }, "文本"));
        tSel.value = row.type;
        const valSlot = el("div");
        function syncValInput() {
          if (row.type === "file") {
            const fIn = el("input", { type: "file" });
            fIn.addEventListener("change", () => {
              row.file = fIn.files?.[0] || null;
            });
            valSlot.replaceChildren(fIn);
          } else {
            const tx = el("input", { type: "text", value: row.value || "", placeholder: "文本值" });
            tx.addEventListener("input", () => {
              row.value = tx.value;
            });
            valSlot.replaceChildren(tx);
          }
        }
        tSel.addEventListener("change", () => {
          row.type = tSel.value;
          syncValInput();
        });
        syncValInput();
        const rm = el("button", { type: "button", class: "btn small danger", "aria-label": "删除字段" }, "✕");
        rm.addEventListener("click", () => {
          studioState.inspectorMultipartRows.splice(idx, 1);
          rebuildMultipartRows();
        });
        return el("div", { class: "ck-presets-row" }, kIn, tSel, valSlot, rm);
      });
      const addRow = el("button", { type: "button", class: "btn small secondary" }, "+ 添加字段");
      addRow.addEventListener("click", () => {
        studioState.inspectorMultipartRows.push({ key: "field", type: "text", value: "", file: null });
        rebuildMultipartRows();
      });
      mpContainer.replaceChildren(...rows, addRow);
    }
    rebuildMultipartRows();
    methodSel.addEventListener("change", syncInspectorBodyUi);
    syncInspectorBodyUi();

    inspectorPrefillFn = (preset = {}) => {
      methodSel.value = String(preset.method || "GET").toUpperCase();
      pathInput.value = String(preset.path ?? "").replace(/^\/+/, "");
      if (preset.body !== undefined && preset.body !== null) {
        bodyTextarea.value = typeof preset.body === "string" ? preset.body : JSON.stringify(preset.body, null, 2);
        bmodeCtrl.setValue(preset.bodyMode || "json", false);
      } else if (methodSel.value === "GET" || methodSel.value === "HEAD") {
        bmodeCtrl.setValue("none", false);
      }
      syncInspectorBodyUi();
      pathInput.focus();
    };

    const presetPills = el(
      "div",
      { class: "ck-presets-row" },
      ...presets.map((p) => {
        const b = el("button", { type: "button", class: "btn small secondary" }, `${p.method} ${p.label}`);
        b.addEventListener("click", () => inspectorPrefillFn(p));
        return b;
      })
    );

    const statusBadge = el("span", { class: "badge neutral" }, "未发送");
    const latencySpan = el("span", { class: "muted ck-mono" }, "");
    const mediaPreview = el("div", { class: "ck-studio-output-card" });
    const headersPre = el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, "暂无响应头"));
    const headersAcc = el("details", { class: "ck-accordion", hidden: "" }, el("summary", {}, "响应头 (Response Headers)"), el("div", { class: "ck-acc-body" }, headersPre));
    const outCode = el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, "响应内容将显示在这里。"));
    const progressEl = el("progress", { class: "ck-progress", "aria-label": "HTTP 探测请求进度", hidden: "" });
    const abortBtn = el("button", { type: "button", class: "btn small danger", hidden: "" }, "中止请求");
    abortBtn.addEventListener("click", () => studioState.inspectorAbortController?.abort());
    const submitBtn = el("button", { type: "submit", class: "btn primary" }, "发送同源代理请求");

    const historyRow = el("div", { class: "ck-history-list" });
    function renderHistory() {
      historyRow.replaceChildren(
        ...studioState.inspectorHistory.map((h) => {
          const pill = el("button", { type: "button", class: "btn small secondary ck-mono" }, `${h.method} /${h.path} · ${h.status} · ${h.dt}ms`);
          pill.addEventListener("click", () => inspectorPrefillFn({ method: h.method, path: h.path, body: h.bodyText, bodyMode: h.bodyMode }));
          return pill;
        })
      );
    }
    renderHistory();

    const formatJsonBtn = el("button", { type: "button", class: "btn small secondary" }, "格式化 JSON");
    formatJsonBtn.addEventListener("click", () => {
      try {
        bodyTextarea.value = JSON.stringify(JSON.parse(bodyTextarea.value || "{}"), null, 2);
      } catch (err) {
        toast(`JSON 语法错误：${err.message}`, "warn");
      }
    });

    const copyCurlBtn = makeCopyButton(() => {
      const tgt = getSelectedTarget();
      const method = methodSel.value;
      const subpath = pathInput.value.trim().replace(/^\/+/, "");
      const url = tgt ? `${window.location.origin}${proxyUrl(tgt.session_name, subpath)}` : `${window.location.origin}/api/proxy/<service>/default/${subpath}`;
      const isWrite = method === "POST" || method === "PUT" || method === "DELETE";
      const lines = [`curl ${method === "HEAD" ? "-I" : `-X ${method}`} "${url}" \\`, '  -H "Cookie: <SESSION_COOKIE>"' + (isWrite ? " \\" : "")];
      if (isWrite) lines.push('  -H "X-ColabLLM-CSRF: <CSRF_TOKEN>"' + (bodyTextarea.value.trim() ? " \\" : ""));
      if ((method === "POST" || method === "PUT") && bmodeCtrl.getValue() === "json" && bodyTextarea.value.trim()) {
        lines.push('  -H "Content-Type: application/json" \\');
        lines.push(`  -d '${bodyTextarea.value.replace(/'/g, "'\"'\"'")}'`);
      }
      return lines.join("\n");
    }, "复制 cURL");

    let lastRespText = "";

    const formEl = el(
      "form",
      { class: "card ck-studio-form-card" },
      el("h3", {}, "同源 HTTP / 流式 / Multipart 探针"),
      presetPills,
      el("div", { class: "ck-two-col" }, makeField("ck-http-method", "HTTP 方法", methodSel), makeField("ck-http-path", "远端相对路径（含 query）", pathInput)),
      bmodeCtrl.group,
      bodyFieldWrap,
      mpContainer,
      makeField("ck-http-auth", "可选 Authorization 覆盖 Header", customAuthInput),
      el("div", { class: "ck-composer-right" }, formatJsonBtn, copyCurlBtn, progressEl, abortBtn, submitBtn)
    );

    formEl.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const tgt = requireTarget();
        const method = methodSel.value;
        const subpath = pathInput.value.trim().replace(/^\/+/, "");
        const bmode = bmodeCtrl.getValue();
        studioState.inspectorAbortController = new AbortController();
        const opts = { method, headers: {}, signal: studioState.inspectorAbortController.signal };
        if (customAuthInput.value.trim()) {
          opts.headers["X-ColabLLM-Proxy-Authorization"] = customAuthInput.value.trim();
        }
        if (method === "POST" || method === "PUT") {
          if (bmode === "multipart") {
            const fd = new FormData();
            for (const row of studioState.inspectorMultipartRows) {
              if (!row.key) continue;
              if (row.type === "file" && row.file) fd.append(row.key, row.file);
              else if (row.type === "text") fd.append(row.key, row.value || "");
            }
            opts.body = fd;
          } else if (bmode === "json" && bodyTextarea.value.trim()) {
            JSON.parse(bodyTextarea.value);
            opts.headers["Content-Type"] = "application/json";
            opts.body = bodyTextarea.value;
          } else if (bmode === "text" && bodyTextarea.value) {
            opts.headers["Content-Type"] = "text/plain; charset=utf-8";
            opts.body = bodyTextarea.value;
          }
        }

        progressEl.hidden = false;
        abortBtn.hidden = false;
        submitBtn.disabled = true;
        const t0 = performance.now();
        const resp = await proxyFetch(tgt.session_name, subpath, opts);
        const dt = Math.round(performance.now() - t0);
        const ct = (resp.headers.get("Content-Type") || "").toLowerCase();

        statusBadge.textContent = `HTTP ${resp.status}`;
        statusBadge.className = resp.ok ? "badge ok" : "badge danger";
        latencySpan.textContent = `${dt} ms · ${ct || "unknown"}`;
        headersAcc.hidden = false;
        headersPre.firstChild.textContent = Array.from(resp.headers.entries())
          .map(([k, v]) => `${k}: ${v}`)
          .join("\n") || "(无暴露响应头)";

        mediaPreview.replaceChildren();
        const badges = renderHeaderBadges(resp.headers, el, dt);
        if (badges) mediaPreview.appendChild(badges);

        studioState.inspectorHistory.unshift({
          method,
          path: subpath,
          bodyMode: bmode,
          bodyText: bodyTextarea.value,
          status: resp.status,
          dt,
        });
        studioState.inspectorHistory = studioState.inspectorHistory.slice(0, 8);
        renderHistory();

        if (method === "HEAD") {
          lastRespText = headersPre.firstChild.textContent;
          outCode.firstChild.textContent = lastRespText;
        } else if ((ct.includes("text/event-stream") || ct.includes("application/x-ndjson")) && resp.body) {
          const reader = resp.body.getReader();
          const dec = new TextDecoder();
          let acc = "";
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            acc += dec.decode(value, { stream: true });
            lastRespText = acc;
            outCode.firstChild.textContent = acc;
          }
        } else if (ct.startsWith("image/")) {
          const u = trackBlobUrl(URL.createObjectURL(await resp.blob()));
          mediaPreview.appendChild(el("img", { src: u, alt: subpath, class: "ck-media-thumb" }));
          lastRespText = `图片已渲染 (${ct})`;
          outCode.firstChild.textContent = lastRespText;
        } else if (ct.startsWith("audio/")) {
          const u = trackBlobUrl(URL.createObjectURL(await resp.blob()));
          mediaPreview.appendChild(el("audio", { controls: "", src: u }));
          lastRespText = `音频已加载 (${ct})`;
          outCode.firstChild.textContent = lastRespText;
        } else if (ct.startsWith("video/")) {
          const u = trackBlobUrl(URL.createObjectURL(await resp.blob()));
          mediaPreview.appendChild(el("video", { controls: "", src: u, class: "ck-media-thumb" }));
          lastRespText = `视频已加载 (${ct})`;
          outCode.firstChild.textContent = lastRespText;
        } else if (ct.includes("application/octet-stream")) {
          const blob = await resp.blob();
          const u = trackBlobUrl(URL.createObjectURL(blob));
          mediaPreview.appendChild(el("a", { href: u, download: "proxy-response.bin", class: "btn small secondary" }, `下载二进制响应 (${blob.size} bytes)`));
          lastRespText = `二进制响应 ${blob.size} 字节。`;
          outCode.firstChild.textContent = lastRespText;
        } else {
          const txt = await resp.text();
          try {
            lastRespText = JSON.stringify(JSON.parse(txt), null, 2);
          } catch {
            lastRespText = txt;
          }
          outCode.firstChild.textContent = lastRespText;
        }
      } catch (err) {
        if (err.name === "AbortError") {
          statusBadge.textContent = "已中止";
          statusBadge.className = "badge warn";
          toast("已中止请求", "warn");
        } else {
          statusBadge.textContent = "请求异常";
          statusBadge.className = "badge danger";
          lastRespText = err.message;
          outCode.firstChild.textContent = err.message;
          toast(err.message, "error");
        }
      } finally {
        studioState.inspectorAbortController = null;
        progressEl.hidden = true;
        abortBtn.hidden = true;
        submitBtn.disabled = false;
      }
    });

    root.replaceChildren(
      el(
        "div",
        { class: "ck-studio-grid" },
        formEl,
        el(
          "div",
          { class: "card ck-studio-output-card" },
          el(
            "div",
            { class: "ck-output-header" },
            el("div", { class: "ck-presets-row" }, el("h3", {}, "代理响应输出"), statusBadge, latencySpan),
            makeCopyButton(() => lastRespText, "复制响应")
          ),
          historyRow,
          mediaPreview,
          headersAcc,
          outCode
        )
      )
    );
  }

  return {
    syncTarget(target, svcMeta) {
      const key = `${target?.session_name || "none"}::${svcMeta?.name || "none"}`;
      if (studioState.activeTabRenderKey === key) return;
      studioState.activeTabRenderKey = key;
      renderImageStudio();
      renderAudioStudio();
      renderTranscribeStudio();
      renderEmbeddingsStudio();
      renderSpecializedStudio();
      renderInspectorStudio();
    },
    forceRerenderAll() {
      studioState.activeTabRenderKey = "";
      this.syncTarget(getSelectedTarget(), getSelectedProfile() ? getSelectedTarget() : null);
    },
    loadAudioForTranscribe(media) {
      if (media) {
        const file =
          media.file ||
          (media.blob ? new File([media.blob], media.name || "audio.wav", { type: media.blob.type || "audio/wav" }) : null);
        studioState.bridgedAsrFile = file ? { file, url: media.url, name: file.name } : media;
        asrApplyBridgeFn?.(studioState.bridgedAsrFile);
      }
    },
    prefillInspector(preset) {
      inspectorPrefillFn?.(preset);
    },
  };
}
