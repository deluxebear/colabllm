/**
 * Multimodal & Runtime-Native API Studios for ColabLLM Copilot Studio:
 * - Image Studio (/v1/images/*, PUP, Async 4K jobs)
 * - Audio Studio (/v1/audio/speech, voice clones, SFX generations)
 * - Transcribe Studio (SALM /v1/chat/completions + CTC timestamps, /v1/audio/transcriptions)
 * - Embeddings Studio (/v1/embeddings + Cosine Similarity Matrix)
 * - Specialized Native Consoles (Ollama, ComfyUI, Unlimited-OCR, R2-Migrate, Unsloth, OpenJutsu, LongCat)
 * - Universal Same-Origin HTTP Inspector (GET/HEAD/POST/PUT/DELETE, JSON/Multipart/SSE/NDJSON/Binary)
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

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("读取本地文件失败"));
    reader.readAsDataURL(file);
  });
}

async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
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
    proxyUrl,
    proxyFetch,
    getCsrfToken,
    getSelectedTarget,
    getSelectedProfile,
    getSelectedModel,
    trackBlobUrl,
    onSendMediaToChat,
  } = ctx;

  const studioState = {
    unslothJwt: "",
    activeTabRenderKey: "",
    inspectorMultipartRows: [{ key: "file", type: "file", value: "", file: null }],
    inspectorHistory: [],
    lastTranscribedAudioUrl: "",
  };

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

  function renderHeaderBadges(headers, elFactory) {
    const badges = [];
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
    const maxRefs = feats.max_refs || 4;
    const refTagHint = feats.ref_tag_style || "<imageN>";

    const modeGroup = el(
      "div",
      { class: "ck-presets-row", role: "radiogroup", "aria-label": "图像生成模式" },
      ...[
        ["gen", "文生图 (/v1/images/generations)"],
        ["edit", "图生图编辑 (/v1/images/edits)"],
        ...(hasAsyncJobs ? [["async4k", "4K 异步任务 (/api/ct/jobs)"]] : []),
        ...(hasChatShim ? [["chat", "Chat 图像接口 (/v1/chat/completions)"]] : []),
      ].map(([val, label], idx) =>
        el(
          "label",
          { class: "switch-row" },
          el("input", { type: "radio", name: "ck-img-mode", value: val, checked: idx === 0 || undefined }),
          el("span", {}, label)
        )
      )
    );

    const promptEl = el("textarea", {
      id: "ck-img-prompt",
      class: "ck-autosize",
      rows: "3",
      placeholder: `输入图像提示词；编辑模式可引用 ${refTagHint}`,
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
    const driveRefInput = el("input", {
      id: "ck-img-drive-refs",
      type: "text",
      autocorrect: "off",
      autocapitalize: "none",
      spellcheck: "false",
      placeholder: "可选 drive://... 参考图路径，多个用逗号分隔",
    });
    const saveDriveEl = makeSwitch("ck-img-save-drive", "保存到 Drive (save_to_drive)");
    const progressEl = el("progress", { class: "ck-progress", hidden: "" });
    const galleryEl = el("div", { id: "ck-img-gallery", class: "ck-gallery-wrap" });
    const statusLine = el("div", { class: "muted ck-cap-summary" }, "准备就绪。");

    const noticeText = feats.rejects_negative_prompt
      ? `${svcName} 约束：固定 ${feats.fixed_steps || 6} 步，支持最多 ${maxRefs} 张参考图（${refTagHint}），服务端会拒绝 negative_prompt 与 lora。`
      : `支持最多 ${maxRefs} 张参考图（${refTagHint}），同时支持 Multipart 文件与 drive:// 引用。`;

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
        }
      });
    }

    const submitBtn = el("button", { type: "submit", class: "btn primary" }, "开始生成 / 编辑");
    const formEl = el(
      "form",
      { class: "card ck-studio-form-card" },
      el("h3", {}, "图像参数与参考图"),
      el("p", { class: "muted ck-cap-summary" }, noticeText),
      modeGroup,
      makeField("ck-img-prompt", "正向提示词", promptEl),
      pupBtn ? el("div", { class: "ck-presets-row" }, pupBtn) : null,
      supportsNeg ? makeField("ck-img-neg", "负面提示词 (negative_prompt)", negEl) : null,
      el(
        "div",
        { class: "ck-two-col" },
        hasAsyncJobs ? makeField("ck-img-aspect", "画幅比例 (aspect)", aspectSelect) : makeField("ck-img-size", "输出尺寸 (size)", sizeSelect),
        hasAsyncJobs ? makeField("ck-img-tier", "清晰度档位 (tier)", tierSelect) : makeField("ck-img-n", "生成张数 (n)", countInput)
      ),
      el(
        "div",
        { class: "ck-two-col" },
        makeField("ck-img-steps", "推理步数 (steps)", stepsInput),
        supportsCfg
          ? makeField("ck-img-cfg", "True CFG Scale", cfgInput)
          : makeField("ck-img-seed", "随机种子 (seed)", seedInput)
      ),
      makeField("ck-img-refs", `上传参考图（最多 ${maxRefs} 张）`, refsInput),
      feats.drive_io ? makeField("ck-img-drive-refs", "Drive 参考图路径", driveRefInput) : null,
      feats.drive_io ? saveDriveEl : null,
      el("div", { class: "ck-composer-right" }, progressEl, submitBtn)
    );

    function appendImages(items, headerBadges) {
      if (!items.length) {
        statusLine.textContent = "接口未返回可预览图像数据，请检查同源 HTTP 探针或原始 JSON。";
        return;
      }
      for (let i = 0; i < items.length; i += 1) {
        const src = items[i];
        const card = el(
          "div",
          { class: "ck-media-card" },
          el("img", { src, alt: `生成图像 #${i + 1}` }),
          headerBadges,
          el(
            "div",
            { class: "ck-presets-row" },
            el("a", { href: src, download: `colabllm-image-${Date.now()}-${i}.png`, class: "btn small secondary" }, "下载图片"),
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
        const mode = formEl.querySelector('input[name="ck-img-mode"]:checked')?.value || "gen";
        const files = Array.from(refsInput.files || []).slice(0, maxRefs);
        const driveRefs = driveRefInput.value
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
        const saveToDrive = Boolean(document.getElementById("ck-img-save-drive")?.checked);
        const seedVal = seedInput.value.trim();

        progressEl.hidden = false;
        submitBtn.disabled = true;
        statusLine.textContent = "正在通过同源代理向 Colab GPU 发送图像请求...";

        if (mode === "async4k") {
          const uploadedIds = [];
          for (const file of files) {
            const fd = new FormData();
            fd.append("file", file);
            const up = await proxyFetch(tgt.session_name, "api/ct/uploads", { method: "POST", body: fd });
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
            await new Promise((r) => setTimeout(r, 2000));
            const poll = await proxyFetch(tgt.session_name, `api/ct/jobs/${encodeURIComponent(jobId)}`);
            finalJob = await poll.json();
            statusLine.textContent = `任务 ${jobId}: ${finalJob.status || "running"}`;
          }
          if (String(finalJob.status || "").toLowerCase() === "failed") {
            throw new Error(finalJob.error || "异步图像任务失败");
          }
          const count = Number(finalJob.image_count || finalJob.outputs?.length || 1);
          const imgs = [];
          for (let idx = 0; idx < count; idx += 1) {
            const imgResp = await proxyFetch(tgt.session_name, `api/ct/jobs/${encodeURIComponent(jobId)}/content?index=${idx}&format=webp`);
            if (imgResp.ok) {
              const blob = await imgResp.blob();
              imgs.push(trackBlobUrl(URL.createObjectURL(blob)));
            }
          }
          appendImages(imgs, el("span", { class: "badge ok" }, `Job ${jobId}`));
          statusLine.textContent = `异步 4K 任务 ${jobId} 完成。`;
        } else if (mode === "edit") {
          let resp;
          if (files.length) {
            const fd = new FormData();
            fd.append("model", defaultModel);
            fd.append("prompt", prompt);
            fd.append("response_format", "b64_json");
            fd.append("n", countInput.value || "1");
            if (supportsSteps && stepsInput.value) fd.append("steps", stepsInput.value);
            if (supportsNeg && negEl.value.trim()) fd.append("negative_prompt", negEl.value.trim());
            if (supportsCfg && cfgInput.value) fd.append("true_cfg_scale", cfgInput.value);
            if (seedVal) fd.append("seed", seedVal);
            if (saveToDrive) fd.append("save_to_drive", "true");
            for (const f of files) fd.append("image", f);
            resp = await proxyFetch(tgt.session_name, "v1/images/edits", { method: "POST", body: fd });
          } else {
            const body = {
              model: defaultModel,
              prompt,
              response_format: "b64_json",
              n: Number(countInput.value || 1),
              image: driveRefs,
            };
            if (supportsSteps && stepsInput.value) body.steps = Number(stepsInput.value);
            if (supportsNeg && negEl.value.trim()) body.negative_prompt = negEl.value.trim();
            if (supportsCfg && cfgInput.value) body.true_cfg_scale = Number(cfgInput.value);
            if (seedVal) body.seed = Number(seedVal);
            if (saveToDrive) body.save_to_drive = true;
            resp = await proxyFetch(tgt.session_name, "v1/images/edits", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            });
          }
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error?.message || data.error || resp.statusText);
          const urls = (data.data || [])
            .map((d) => (d.b64_json ? `data:image/png;base64,${d.b64_json}` : d.url))
            .filter(Boolean);
          appendImages(urls, renderHeaderBadges(resp.headers, el));
          statusLine.textContent = `已返回 ${urls.length} 张编辑结果。`;
        } else if (mode === "chat") {
          const resp = await proxyFetch(tgt.session_name, "v1/chat/completions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              model: defaultModel,
              messages: [{ role: "user", content: prompt }],
              stream: false,
            }),
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error?.message || data.error || resp.statusText);
          const content = data.choices?.[0]?.message?.content || "";
          const match = String(content).match(/data:image\/[a-zA-Z+]+;base64,[A-Za-z0-9+/=]+/);
          if (match) appendImages([match[0]], renderHeaderBadges(resp.headers, el));
          statusLine.textContent = match ? "已通过 Chat Image Shim 提取图像。" : `文本回复：${String(content).slice(0, 160)}`;
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
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error?.message || data.error || resp.statusText);
          const urls = (data.data || [])
            .map((d) => (d.b64_json ? `data:image/png;base64,${d.b64_json}` : d.url))
            .filter(Boolean);
          appendImages(urls, renderHeaderBadges(resp.headers, el));
          statusLine.textContent = `已生成 ${urls.length} 张图像。`;
        }
      } catch (err) {
        statusLine.textContent = `错误：${err.message}`;
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
        el("div", { class: "card ck-studio-output-card" }, el("h3", {}, "生成图库"), statusLine, galleryEl)
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
    const durationInput = el("input", { id: "ck-aud-duration", type: "number", min: "1", max: "30", step: "0.5", value: "8" });
    const stepsInput = el("input", { id: "ck-aud-steps", type: "number", min: "5", max: "100", value: "25" });
    const emoAlphaInput = el("input", { id: "ck-aud-emo-alpha", type: "number", min: "0", max: "1", step: "0.1", value: "0.8" });
    const emoTextInput = el("input", { id: "ck-aud-emo-text", type: "text", placeholder: "自动根据情感提示文本驱动语调（可选）" });
    const emoSliderInputs = {};
    const emoGrid = feats.emotion_vector_8d
      ? el(
          "details",
          { class: "ck-accordion" },
          el("summary", {}, "IndexTTS 8 维情绪向量 (emo_vector)"),
          el(
            "div",
            { class: "ck-acc-body ck-emo-grid" },
            ...EMO_LABELS.map(([key, label]) => {
              const inp = el("input", { id: `ck-emo-${key}`, type: "number", min: "0", max: "1", step: "0.1", value: "0" });
              emoSliderInputs[key] = inp;
              return makeField(`ck-emo-${key}`, label, inp);
            })
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
            b.addEventListener("click", () => {
              textEl.value = `${textEl.value}${textEl.value && !textEl.value.endsWith(" ") ? " " : ""}${tag} `;
              textEl.focus();
            });
            return b;
          })
        )
      : null;

    const progressEl = el("progress", { class: "ck-progress", hidden: "" });
    const statusLine = el("div", { class: "muted ck-cap-summary" }, "等待合成或生成音频。");
    const resultsList = el("div", { class: "ck-studio-output-card" });

    const submitBtn = el("button", { type: "submit", class: "btn primary" }, isSfx ? "生成音效" : "合成语音");
    const mainForm = el(
      "form",
      { class: "card ck-studio-form-card" },
      el("h3", {}, isSfx ? "TangoFlux 音效生成 (/v1/audio/generations)" : "语音合成 (/v1/audio/speech)"),
      makeField("ck-aud-input", isSfx ? "音效描述 Prompt" : "合成文本 (input)", textEl),
      tagsBar,
      isSfx
        ? el(
            "div",
            { class: "ck-two-col" },
            makeField("ck-aud-duration", "时长 (秒, 1-30)", durationInput),
            makeField("ck-aud-steps", "去噪步数 (steps)", stepsInput)
          )
        : el(
            "div",
            { class: "ck-two-col" },
            makeField("ck-aud-voice", "音色名称 (voice)", voiceInput),
            makeField("ck-aud-format", "音频格式 (response_format)", fmtSelect)
          ),
      voiceDatalist,
      feats.emotion_vector_8d
        ? el("div", { class: "ck-two-col" }, makeField("ck-aud-emo-alpha", "情绪强度 (emo_alpha)", emoAlphaInput), makeField("ck-aud-emo-text", "情绪文本引导 (emo_text)", emoTextInput))
        : null,
      emoGrid,
      el("div", { class: "ck-composer-right" }, progressEl, submitBtn)
    );

    mainForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const tgt = requireTarget();
        const text = textEl.value.trim();
        if (!text) return toast("请输入文本或音效描述", "warn");
        progressEl.hidden = false;
        submitBtn.disabled = true;
        statusLine.textContent = "正在生成音频...";

        let path = "v1/audio/speech";
        let payload;
        if (isSfx) {
          path = "v1/audio/generations";
          payload = {
            model: defaultModel,
            prompt: text,
            duration: Number(durationInput.value || 8),
            steps: Number(stepsInput.value || 25),
          };
        } else {
          payload = {
            model: defaultModel,
            input: text,
            voice: voiceInput.value.trim() || "default",
            response_format: fmtSelect.value,
          };
          if (feats.emotion_vector_8d) {
            payload.emo_alpha = Number(emoAlphaInput.value || 0.8);
            if (emoTextInput.value.trim()) payload.emo_text = emoTextInput.value.trim();
            const vec = EMO_LABELS.map(([k]) => Number(emoSliderInputs[k]?.value || 0));
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

        const ct = (resp.headers.get("Content-Type") || "").toLowerCase();
        const badges = renderHeaderBadges(resp.headers, el);
        if (ct.includes("application/json")) {
          const data = await resp.json();
          const items = data.data || [data];
          for (const item of items) {
            if (item.b64_json) {
              const src = `data:audio/wav;base64,${item.b64_json}`;
              resultsList.prepend(
                el("div", { class: "ck-media-card" }, el("audio", { controls: "", src }), badges)
              );
            }
          }
          statusLine.textContent = "音频 JSON 响应已渲染。";
        } else {
          const blob = await resp.blob();
          const url = trackBlobUrl(URL.createObjectURL(blob));
          resultsList.prepend(
            el(
              "div",
              { class: "ck-media-card" },
              el("strong", {}, text.slice(0, 60)),
              badges,
              el("audio", { controls: "", src: url }),
              el("a", { href: url, download: `colabllm-audio.${fmtSelect.value || "wav"}`, class: "btn small secondary" }, "下载音频")
            )
          );
          statusLine.textContent = `已生成 ${blob.size} 字节音频。`;
        }
      } catch (err) {
        statusLine.textContent = `失败：${err.message}`;
        toast(err.message, "error");
      } finally {
        progressEl.hidden = true;
        submitBtn.disabled = false;
      }
    });

    // Voice library management & Design Voice cards (for TTS runtimes)
    const rightCol = [el("div", { class: "card ck-studio-output-card" }, el("h3", {}, "音频播放与导出"), statusLine, resultsList)];
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
          voiceDatalist.replaceChildren(el("option", { value: "default" }), ...names.map((v) => {
            const n = typeof v === "string" ? v : v.name || v.id;
            return el("option", { value: n });
          }));
          voicesContainer.replaceChildren(
            ...(names.length
              ? names.map((v) => {
                  const n = typeof v === "string" ? v : v.name || v.id;
                  const useBtn = el("button", { type: "button", class: "btn small secondary" }, `选用: ${n}`);
                  useBtn.addEventListener("click", () => {
                    voiceInput.value = n;
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
      const cloneFile = el("input", { id: "ck-clone-file", type: "file", accept: "audio/*", required: "" });
      const cloneRefText = el("input", { id: "ck-clone-reftext", type: "text", placeholder: "参考音频对应文本（可选）" });
      const cloneConsent = makeSwitch("ck-clone-consent", "我已获得说话人声音克隆授权 (consent=true)", true);
      const cloneForm = el(
        "form",
        { class: "ck-studio-form-card" },
        makeField("ck-clone-name", "音色 ID", cloneName),
        makeField("ck-clone-file", "上传参考干声", cloneFile),
        makeField("ck-clone-reftext", "参考文本 (ref_text)", cloneRefText),
        cloneConsent,
        el("button", { type: "submit", class: "btn small primary" }, "上传并创建音色")
      );
      cloneForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
          const tgt = requireTarget();
          const name = cloneName.value.trim();
          const file = cloneFile.files?.[0];
          if (!name || !file) return toast("请填写音色 ID 并选择音频样本", "warn");
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
        }
      });

      rightCol.push(
        el(
          "details",
          { class: "ck-accordion" },
          el("summary", {}, "音色克隆库管理 (/v1/audio/voices)"),
          el("div", { class: "ck-acc-body" }, loadVoicesBtn, voicesContainer, cloneForm)
        )
      );
    }

    if (feats.voice_design) {
      const vdName = el("input", { id: "ck-vd-name", type: "text", autocorrect: "off", placeholder: "designed_voice" });
      const vdDesc = el("textarea", { id: "ck-vd-desc", class: "ck-autosize", rows: "2", placeholder: "例如：温柔知性的播客女声，咬字清晰..." });
      const vdSample = el("input", { id: "ck-vd-sample", type: "text", value: "你好，这是通过自然语言描述生成的新音色。" });
      const vdForm = el(
        "form",
        { class: "ck-acc-body" },
        makeField("ck-vd-name", "保存音色名", vdName),
        makeField("ck-vd-desc", "音色风格描述", vdDesc),
        makeField("ck-vd-sample", "试听示例句", vdSample),
        el("button", { type: "submit", class: "btn small primary" }, "生成并保存音色 (/api/ct/design-voice)")
      );
      vdForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
          const tgt = requireTarget();
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
          toast("设计音色成功", "ok");
        } catch (err) {
          toast(err.message, "error");
        }
      });
      rightCol.push(el("details", { class: "ck-accordion" }, el("summary", {}, "Qwen3-TTS 自然语言捏音色"), vdForm));
    }

    root.replaceChildren(el("div", { class: "ck-studio-grid" }, mainForm, el("div", { class: "ck-studio-output-card" }, ...rightCol)));
  }

  /* ===================== 3. TRANSCRIBE STUDIO ===================== */
  function renderTranscribeStudio() {
    const root = document.getElementById("ck-transcribe-root");
    if (!root) return;
    const profile = getSelectedProfile();
    const feats = profile?.features || {};
    const defaultModel = getSelectedModel() || profile?.default_model || getSelectedTarget()?.service || "asr";
    const isSalm = Boolean(feats.salm_chat_audio);

    const fileEl = el("input", { id: "ck-asr-file", type: "file", accept: "audio/*,video/*" });
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
    const progressEl = el("progress", { class: "ck-progress", hidden: "" });
    const transcriptOut = el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, "转写结果将显示在这里。"));
    const ctcContainer = el("div", { class: "ck-ctc-segments" });
    const presetsContainer = el("div", { class: "ck-presets-row" });

    fileEl.addEventListener("change", () => {
      const f = fileEl.files?.[0];
      if (f) {
        const u = trackBlobUrl(URL.createObjectURL(f));
        studioState.lastTranscribedAudioUrl = u;
        audioPlayer.src = u;
      }
    });

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

    const submitBtn = el("button", { type: "submit", class: "btn primary" }, isSalm ? "执行 SALM 音频转写 (/v1/chat/completions)" : "执行转写 (/v1/audio/transcriptions)");
    const formEl = el(
      "form",
      { class: "card ck-studio-form-card" },
      el("h3", {}, isSalm ? "Nemotron SALM 音文转写与问答" : "OpenAI Whisper 音频转写"),
      el(
        "p",
        { class: "muted ck-cap-summary" },
        isSalm
          ? "Nemotron Transcribe 不使用 /v1/audio/transcriptions，而是通过 /v1/chat/completions + audio_url 调用，并在响应顶层返回 ctc_timestamps。"
          : "通过 multipart/form-data 调用标准 /v1/audio/transcriptions 接口。"
      ),
      makeField("ck-asr-file", "上传本地音频", fileEl),
      makeField("ck-asr-drive", "或 Google Drive 路径", driveEl),
      makeField("ck-asr-prompt", "指令 / 提示词", promptEl),
      loadPromptsBtn,
      presetsContainer,
      isSalm ? ctcSwitch : null,
      isSalm ? thinkSwitch : null,
      el("div", { class: "ck-composer-right" }, progressEl, submitBtn)
    );

    formEl.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const tgt = requireTarget();
        const file = fileEl.files?.[0];
        const drivePath = driveEl.value.trim();
        if (!file && !drivePath) return toast("请选择本地音频或输入 drive:// 路径", "warn");

        progressEl.hidden = false;
        submitBtn.disabled = true;

        if (isSalm) {
          let audioUrl = drivePath;
          if (file) {
            audioUrl = await fileToDataUrl(file);
          }
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
          transcriptOut.firstChild.textContent = msg.content || JSON.stringify(data, null, 2);

          const rawCtc = data.ctc_timestamps || data.timestamps || [];
          const segments = Array.isArray(rawCtc) ? rawCtc : rawCtc.words || rawCtc.segments || [];
          ctcContainer.replaceChildren(
            ...(segments.length
              ? segments.map((seg) => {
                  const text = seg.word || seg.text || seg.label || "?";
                  const start = Number(seg.start ?? seg.start_time ?? 0);
                  const btn = el("button", { type: "button", class: "ck-ctc-btn" }, `${text} (${start.toFixed(2)}s)`);
                  btn.addEventListener("click", () => {
                    if (audioPlayer.src) {
                      audioPlayer.currentTime = start;
                      audioPlayer.play().catch(() => {});
                    }
                  });
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
            transcriptOut.firstChild.textContent = parsed.text || JSON.stringify(parsed, null, 2);
          } catch {
            transcriptOut.firstChild.textContent = raw;
          }
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
          el("h3", {}, "音频播放与 CTC 时间戳联动"),
          audioPlayer,
          transcriptOut,
          el("strong", {}, "时间戳切片（点击跳转到对应秒数播放）"),
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
      value: "ColabLLM 本地同源代理自动注入认证令牌\nSame-origin reverse proxy protects API tokens\n今天天气适合在公园散步",
    });
    const progressEl = el("progress", { class: "ck-progress", hidden: "" });
    const summaryEl = el("p", { class: "muted" }, "每行作为一条独立文本输入 /v1/embeddings，计算余弦相似度矩阵。");
    const matrixWrap = el("div", { class: "ck-table-wrap" });
    const rawWrap = el("details", { class: "ck-accordion" }, el("summary", {}, "原始向量 JSON"), el("div", { class: "ck-acc-body" }));

    const formEl = el(
      "form",
      { class: "card ck-studio-form-card" },
      el("h3", {}, "OpenAI /v1/embeddings 向量与相似度测试"),
      makeField("ck-emb-lines", "待编码文本（每行一条）", linesEl),
      el("div", { class: "ck-composer-right" }, progressEl, el("button", { type: "submit", class: "btn primary" }, "计算 Embeddings"))
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
        const resp = await proxyFetch(tgt.session_name, "v1/embeddings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: getSelectedModel() || defaultModel, input: inputs }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error?.message || data.error || resp.statusText);
        const vectors = (data.data || []).sort((a, b) => (a.index ?? 0) - (b.index ?? 0)).map((d) => d.embedding || []);
        const dims = vectors[0]?.length || 0;
        summaryEl.textContent = `返回 ${vectors.length} 条向量，维度 D=${dims}，Tokens=${data.usage?.total_tokens ?? "n/a"}`;

        const headRow = el("tr", {}, el("th", {}, "#"), ...inputs.map((t, i) => el("th", {}, `#${i + 1}: ${t.slice(0, 14)}`)));
        const bodyRows = inputs.map((t, r) =>
          el(
            "tr",
            {},
            el("th", {}, `#${r + 1}: ${t.slice(0, 14)}`),
            ...inputs.map((_, c) => {
              const sim = vectors[r] && vectors[c] ? cosineSimilarity(vectors[r], vectors[c]) : 0;
              return el("td", { class: "ck-matrix-cell" }, sim.toFixed(4));
            })
          )
        );
        matrixWrap.replaceChildren(el("table", { class: "ck-data-table" }, el("thead", {}, headRow), el("tbody", {}, ...bodyRows)));
        rawWrap.lastChild.replaceChildren(
          renderCodeBlock(
            el,
            "前 8 维预览",
            JSON.stringify(
              vectors.map((v, i) => ({ index: i, norm: Math.hypot(...v).toFixed(4), head8: v.slice(0, 8) })),
              null,
              2
            )
          )
        );
      } catch (err) {
        toast(err.message, "error");
      } finally {
        progressEl.hidden = true;
      }
    });

    root.replaceChildren(
      el("div", { class: "ck-studio-grid" }, formEl, el("div", { class: "card ck-studio-output-card" }, el("h3", {}, "余弦相似度矩阵"), summaryEl, matrixWrap, rawWrap))
    );
  }

  /* ===================== 5. SPECIALIZED NATIVE CONSOLE ===================== */
  function renderSpecializedStudio() {
    const root = document.getElementById("ck-specialized-root");
    if (!root) return;
    const target = getSelectedTarget();
    const profile = getSelectedProfile();
    const svc = target?.service || "";
    const outputCode = el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, "原生 API 执行结果将显示在这里。"));
    const mediaBox = el("div", { class: "ck-gallery-wrap" });

    async function callAndShow(subpath, fetchOpts = {}, note = "") {
      const tgt = requireTarget();
      if (note) outputCode.firstChild.textContent = note;
      const resp = await proxyFetch(tgt.session_name, subpath, fetchOpts);
      const ct = (resp.headers.get("Content-Type") || "").toLowerCase();
      if (!resp.ok) {
        const msg = await resp.text();
        outputCode.firstChild.textContent = `HTTP ${resp.status}\n${msg}`;
        throw new Error(msg || `HTTP ${resp.status}`);
      }
      if (ct.startsWith("image/")) {
        const u = trackBlobUrl(URL.createObjectURL(await resp.blob()));
        mediaBox.replaceChildren(el("div", { class: "ck-media-card" }, el("img", { src: u, alt: subpath })));
        outputCode.firstChild.textContent = `已加载图像资源 (${subpath})`;
        return u;
      }
      if (ct.startsWith("video/")) {
        const u = trackBlobUrl(URL.createObjectURL(await resp.blob()));
        mediaBox.replaceChildren(el("div", { class: "ck-media-card" }, el("video", { controls: "", src: u })));
        outputCode.firstChild.textContent = `已加载视频资源 (${subpath})`;
        return u;
      }
      const txt = await resp.text();
      try {
        const parsed = JSON.parse(txt);
        outputCode.firstChild.textContent = JSON.stringify(parsed, null, 2);
        return parsed;
      } catch {
        outputCode.firstChild.textContent = txt;
        return txt;
      }
    }

    const controls = [el("h3", {}, `${svc || "未选择实例"} · 原生 API 控制台`)];

    if (svc === "ollama") {
      const pullName = el("input", { id: "ck-ol-model", type: "text", autocorrect: "off", autocapitalize: "none", spellcheck: "false", placeholder: "例如 qwen3:8b 或 nomic-embed-text" });
      const pullProgress = el("progress", { class: "ck-progress", max: "100", value: "0", hidden: "" });
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
          makeBtn("已安装模型 /api/tags", () => callAndShow("api/tags")),
          makeBtn("显存驻留 /api/ps", () => callAndShow("api/ps")),
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
              outputCode.firstChild.textContent = lines.slice(-20).join("\n");
            }
          }
          toast(`模型 ${m} 拉取流程结束`, "ok");
        } catch (err) {
          toast(err.message, "error");
        } finally {
          pullProgress.hidden = true;
        }
      });
      controls.push(pullBtn);
    } else if (svc === "comfyui") {
      const wfJson = el("textarea", {
        id: "ck-cf-workflow",
        class: "ck-autosize",
        rows: "5",
        autocorrect: "off",
        autocapitalize: "none",
        spellcheck: "false",
        placeholder: '{"3": {"class_type": "KSampler", "inputs": {...}}} (ComfyUI API 格式 prompt JSON)',
      });
      const pullUrl = el("input", { id: "ck-cf-pull-url", type: "url", autocorrect: "off", placeholder: "输入工作流或模型下载 URL..." });
      const uploadFile = el("input", { id: "ck-cf-upload", type: "file", accept: "image/*" });
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
          )
        ),
        makeField("ck-cf-upload", "上传输入图到 ComfyUI (/upload/image)", uploadFile),
        makeField("ck-cf-workflow", "API Workflow JSON (/prompt)", wfJson),
        makeField("ck-cf-pull-url", "拉取远端资源 (/api/ct/pull-workflow)", pullUrl)
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
          if (res?.prompt_id) {
            toast(`已提交 ComfyUI Prompt ID: ${res.prompt_id}`, "ok");
          }
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
      controls.push(el("div", { class: "ck-presets-row" }, submitWf, uploadBtn, interruptBtn));
    } else if (svc === "unlimited-ocr") {
      const fileEl = el("input", { id: "ck-ocr-file", type: "file", accept: ".pdf,image/*" });
      const driveEl = el("input", { id: "ck-ocr-drive", type: "text", autocorrect: "off", placeholder: "或输入 drive://... 文件夹/PDF 路径" });
      const jobIdEl = el("input", { id: "ck-ocr-jobid", type: "text", autocorrect: "off", placeholder: "输入 job_id 查询进度或获取结果" });
      const uploadAndRunBtn = el("button", { type: "button", class: "btn small primary" }, "SHA-256 分片上传并识别");
      uploadAndRunBtn.addEventListener("click", async () => {
        try {
          const tgt = requireTarget();
          const file = fileEl.files?.[0];
          if (file) {
            const buf = await file.arrayBuffer();
            const chunkSize = 512 * 1024;
            const totalChunks = Math.max(1, Math.ceil(buf.byteLength / chunkSize));
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
        }
      });
      const checkBtn = el("button", { type: "button", class: "btn small secondary" }, "查询任务 (/api/jobs/{id})");
      checkBtn.addEventListener("click", () => {
        const id = jobIdEl.value.trim();
        if (!id) return toast("请输入 job_id", "warn");
        return callAndShow(`api/jobs/${encodeURIComponent(id)}`).catch((e) => toast(e.message, "error"));
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
      controls.push(
        makeField("ck-ocr-file", "本地 PDF / 扫描件（<=512 KiB 自动分片）", fileEl),
        makeField("ck-ocr-drive", "Drive 路径", driveEl),
        makeField("ck-ocr-jobid", "任务 ID", jobIdEl),
        el("div", { class: "ck-presets-row" }, uploadAndRunBtn, listBtn, checkBtn, resBtn)
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
      const jobIdEl = el("input", { id: "ck-r2-job", type: "text", autocorrect: "off", placeholder: "输入迁移任务 ID（pause/resume/cleanup/cancel）" });
      controls.push(
        el(
          "div",
          { class: "ck-presets-row" },
          ...[["健康检查", "GET", "health"], ["凭证列表", "GET", "api/credentials"], ["任务列表", "GET", "api/jobs"], ["Drive 目录", "POST", "api/drive-list", {}]].map(
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
      // Dynamic preset launcher for any other / future specialized service
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

    root.replaceChildren(
      el(
        "div",
        { class: "ck-studio-grid" },
        el("div", { class: "card ck-studio-form-card" }, ...controls),
        el("div", { class: "card ck-studio-output-card" }, el("h3", {}, "原生 API 输出"), mediaBox, outputCode)
      )
    );
  }

  /* ===================== 6. UNIVERSAL HTTP INSPECTOR ===================== */
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
      value: presets[0]?.path || "v1/models",
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
    const bodyModeRadios = el(
      "div",
      { class: "ck-presets-row", role: "radiogroup", "aria-label": "请求体类型" },
      ...[
        ["json", "JSON"],
        ["multipart", "Multipart FormData"],
        ["text", "Raw Text"],
        ["none", "无请求体"],
      ].map(([val, label], i) =>
        el("label", { class: "switch-row" }, el("input", { type: "radio", name: "ck-http-bmode", value: val, checked: i === 0 || undefined }), el("span", {}, label))
      )
    );
    const mpContainer = el("div", { class: "ck-studio-output-card", hidden: "" });

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
        const rm = el("button", { type: "button", class: "btn small danger" }, "✕");
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

    bodyModeRadios.addEventListener("change", () => {
      const m = bodyModeRadios.querySelector('input[name="ck-http-bmode"]:checked')?.value || "json";
      mpContainer.hidden = m !== "multipart";
      bodyTextarea.parentElement.hidden = m === "multipart" || m === "none";
    });

    const presetPills = el(
      "div",
      { class: "ck-presets-row" },
      ...presets.map((p) => {
        const b = el("button", { type: "button", class: "btn small secondary" }, `${p.method} ${p.label}`);
        b.addEventListener("click", () => {
          methodSel.value = p.method || "GET";
          pathInput.value = p.path || "";
          if (p.body !== undefined) {
            bodyTextarea.value = JSON.stringify(p.body, null, 2);
            const r = bodyModeRadios.querySelector('input[value="json"]');
            if (r) r.checked = true;
          }
          mpContainer.hidden = true;
          bodyTextarea.parentElement.hidden = false;
        });
        return b;
      })
    );

    const statusBadge = el("span", { class: "badge neutral" }, "未发送");
    const latencySpan = el("span", { class: "muted ck-mono" }, "");
    const mediaPreview = el("div", { class: "ck-studio-output-card" });
    const outCode = el("pre", { role: "region", tabindex: "0", class: "ck-code-pre" }, el("code", {}, "响应内容将显示在这里。"));
    const progressEl = el("progress", { class: "ck-progress", hidden: "" });

    const formEl = el(
      "form",
      { class: "card ck-studio-form-card" },
      el("h3", {}, "同源 HTTP / 流式 / Multipart 探针"),
      presetPills,
      el("div", { class: "ck-two-col" }, makeField("ck-http-method", "HTTP 方法", methodSel), makeField("ck-http-path", "远端相对路径（含 query）", pathInput)),
      bodyModeRadios,
      makeField("ck-http-body", "请求体内容", bodyTextarea),
      mpContainer,
      makeField("ck-http-auth", "可选 Authorization 覆盖 Header", customAuthInput),
      el("div", { class: "ck-composer-right" }, progressEl, el("button", { type: "submit", class: "btn primary" }, "发送同源代理请求"))
    );

    formEl.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const tgt = requireTarget();
        const method = methodSel.value;
        const subpath = pathInput.value.trim().replace(/^\/+/, "");
        const bmode = bodyModeRadios.querySelector('input[name="ck-http-bmode"]:checked')?.value || "json";
        const opts = { method, headers: {} };
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
        const t0 = performance.now();
        const resp = await proxyFetch(tgt.session_name, subpath, opts);
        const dt = Math.round(performance.now() - t0);
        const ct = (resp.headers.get("Content-Type") || "").toLowerCase();

        statusBadge.textContent = `HTTP ${resp.status}`;
        statusBadge.className = resp.ok ? "badge ok" : "badge danger";
        latencySpan.textContent = `${dt} ms · ${ct || "unknown"}`;
        mediaPreview.replaceChildren();
        const badges = renderHeaderBadges(resp.headers, el);
        if (badges) mediaPreview.appendChild(badges);

        if (method === "HEAD") {
          outCode.firstChild.textContent = Array.from(resp.headers.entries())
            .map(([k, v]) => `${k}: ${v}`)
            .join("\n");
        } else if ((ct.includes("text/event-stream") || ct.includes("application/x-ndjson")) && resp.body) {
          const reader = resp.body.getReader();
          const dec = new TextDecoder();
          let acc = "";
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            acc += dec.decode(value, { stream: true });
            outCode.firstChild.textContent = acc;
          }
        } else if (ct.startsWith("image/")) {
          const u = trackBlobUrl(URL.createObjectURL(await resp.blob()));
          mediaPreview.appendChild(el("img", { src: u, alt: subpath, class: "ck-media-thumb" }));
          outCode.firstChild.textContent = `图片已渲染 (${ct})`;
        } else if (ct.startsWith("audio/")) {
          const u = trackBlobUrl(URL.createObjectURL(await resp.blob()));
          mediaPreview.appendChild(el("audio", { controls: "", src: u }));
          outCode.firstChild.textContent = `音频已加载 (${ct})`;
        } else if (ct.startsWith("video/")) {
          const u = trackBlobUrl(URL.createObjectURL(await resp.blob()));
          mediaPreview.appendChild(el("video", { controls: "", src: u, class: "ck-media-thumb" }));
          outCode.firstChild.textContent = `视频已加载 (${ct})`;
        } else if (ct.includes("application/octet-stream")) {
          const blob = await resp.blob();
          const u = trackBlobUrl(URL.createObjectURL(blob));
          mediaPreview.appendChild(el("a", { href: u, download: "proxy-response.bin", class: "btn small secondary" }, `下载二进制响应 (${blob.size} bytes)`));
          outCode.firstChild.textContent = `二进制响应 ${blob.size} 字节。`;
        } else {
          const txt = await resp.text();
          try {
            outCode.firstChild.textContent = JSON.stringify(JSON.parse(txt), null, 2);
          } catch {
            outCode.firstChild.textContent = txt;
          }
        }
      } catch (err) {
        statusBadge.textContent = "请求异常";
        statusBadge.className = "badge danger";
        outCode.firstChild.textContent = err.message;
        toast(err.message, "error");
      } finally {
        progressEl.hidden = true;
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
          el("div", { class: "ck-presets-row" }, el("h3", {}, "代理响应输出"), statusBadge, latencySpan),
          mediaPreview,
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
  };
}
