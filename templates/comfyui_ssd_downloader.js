(function() {
  if (window.__ctSsdDownloaderLoaded) return;
  window.__ctSsdDownloaderLoaded = true;

  const MODEL_DIRS = [
    ["diffusion_models", "diffusion_models (UNET / 扩散模型)"],
    ["loras", "loras (LoRA 模型)"],
    ["vae", "vae (VAE 模型)"],
    ["text_encoders", "text_encoders (CLIP / T5 / Qwen 文本编码器)"],
    ["checkpoints", "checkpoints (Checkpoints 大模型)"],
    ["clip_vision", "clip_vision (CLIP Vision 视觉编码器)"],
    ["controlnet", "controlnet (ControlNet 模型)"],
    ["upscale_models", "upscale_models (放大模型)"],
    ["audio_encoders", "audio_encoders (音频编码器)"],
    ["model_patches", "model_patches (Model Patches)"],
    ["embeddings", "embeddings (Embeddings)"],
    ["__custom__", "✏️ 自定义目录..."]
  ];

  const css = `
    #ct-ssd-btn {
      display: inline-flex; align-items: center; gap: 6px;
      background: linear-gradient(135deg, #2563eb 0%, #0d9488 100%);
      color: #fff; border: 1px solid rgba(255,255,255,0.2);
      border-radius: 6px; padding: 5px 11px; font-size: 12px; font-weight: 600;
      cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.3);
      margin: 0 6px; white-space: nowrap; z-index: 9999;
    }
    #ct-ssd-btn:hover { filter: brightness(1.12); }
    #ct-ssd-btn.ct-floating { position: fixed; top: 8px; right: 240px; }
    #ct-ssd-modal-backdrop {
      position: fixed; inset: 0; background: rgba(0,0,0,0.65);
      display: none; align-items: center; justify-content: center; z-index: 100000;
      font-family: system-ui, -apple-system, sans-serif;
    }
    #ct-ssd-modal {
      background: #18181b; color: #f4f4f5; width: 720px; max-width: 95vw;
      max-height: 90vh; border-radius: 10px; border: 1px solid #3f3f46;
      box-shadow: 0 20px 50px rgba(0,0,0,0.6); display: flex; flex-direction: column;
      overflow: hidden;
    }
    .ct-ssd-hdr {
      display: flex; align-items: center; justify-content: space-between;
      padding: 13px 18px; background: #27272a; border-bottom: 1px solid #3f3f46;
    }
    .ct-ssd-hdr h3 { margin: 0; font-size: 15px; display: flex; align-items: center; gap: 8px; }
    .ct-ssd-badge {
      background: #065f46; color: #a7f3d0; padding: 2px 8px; border-radius: 999px;
      font-size: 11px; font-weight: 600;
    }
    .ct-ssd-body { padding: 14px 18px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }
    .ct-ssd-section {
      background: #202024; border: 1px solid #323238; border-radius: 8px;
      padding: 10px 12px; display: flex; flex-direction: column; gap: 8px;
    }
    .ct-ssd-sec-title { font-size: 12px; font-weight: 600; color: #a1a1aa; }
    .ct-ssd-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .ct-ssd-input, .ct-ssd-select {
      background: #09090b; color: #f4f4f5; border: 1px solid #3f3f46;
      border-radius: 6px; padding: 7px 10px; font-size: 12.5px;
    }
    .ct-ssd-input { flex: 1; min-width: 180px; }
    .ct-ssd-action {
      background: #2563eb; color: #fff; border: none; border-radius: 6px;
      padding: 7px 13px; font-size: 12.5px; font-weight: 600; cursor: pointer;
      white-space: nowrap;
    }
    .ct-ssd-action.canvas-btn {
      background: linear-gradient(135deg, #059669 0%, #0284c7 100%);
      width: 100%; padding: 9px 14px; font-size: 13px;
    }
    .ct-ssd-action.model-btn { background: #0d9488; }
    .ct-ssd-action:disabled { opacity: 0.5; cursor: not-allowed; }
    .ct-ssd-progress-wrap {
      background: #27272a; border-radius: 6px; height: 8px; overflow: hidden; display: none;
    }
    .ct-ssd-progress-bar {
      background: linear-gradient(90deg, #10b981, #3b82f6); height: 100%; width: 0%;
      transition: width 0.3s ease;
    }
    #ct-ssd-logs {
      background: #09090b; color: #d4d4d8; border: 1px solid #27272a; border-radius: 6px;
      padding: 9px 10px; font-family: ui-monospace, monospace; font-size: 11.5px;
      height: 145px; overflow-y: auto; white-space: pre-wrap; line-height: 1.45;
    }
    .ct-ssd-models {
      background: #121215; border: 1px solid #27272a; border-radius: 6px;
      padding: 7px 10px; max-height: 115px; overflow-y: auto; font-size: 11.5px;
    }
    .ct-ssd-model-item {
      display: flex; justify-content: space-between; padding: 3px 0;
      border-bottom: 1px solid rgba(255,255,255,0.05); color: #a1a1aa;
    }
    .ct-ssd-close {
      background: transparent; color: #a1a1aa; border: none; font-size: 18px; cursor: pointer;
    }
  `;
  const styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  const dirOptionsHtml = MODEL_DIRS.map(([v, label]) => `<option value="${v}">${label}</option>`).join("");

  const backdrop = document.createElement("div");
  backdrop.id = "ct-ssd-modal-backdrop";
  backdrop.innerHTML = `
    <div id="ct-ssd-modal">
      <div class="ct-ssd-hdr">
        <h3>⚡ Colab 本地 SSD 模型按需下载 <span id="ct-ssd-free" class="ct-ssd-badge">SSD 检测中...</span></h3>
        <button class="ct-ssd-close" id="ct-ssd-close-btn">✕</button>
      </div>
      <div class="ct-ssd-body">
        <div class="ct-ssd-section">
          <div class="ct-ssd-sec-title">1️⃣ 按工作流批量下载所需模型（自动识别工作流内全部模型并去重）</div>
          <button class="ct-ssd-action canvas-btn" id="ct-ssd-canvas-btn">
            ⚡ 一键解析并下载【当前画布工作流】所需模型到本地 SSD
          </button>
          <div class="ct-ssd-row">
            <input class="ct-ssd-input" id="ct-ssd-spec-input"
              placeholder="或输入工作流 GitHub URL / 官方模板名 / 网盘文件名 (如 video_minimax_h3_r2v.json)" />
            <button class="ct-ssd-action" id="ct-ssd-url-btn">📥 下载工作流模型</button>
          </div>
        </div>

        <div class="ct-ssd-section">
          <div class="ct-ssd-sec-title">2️⃣ 指定单个模型 URL 下载到对应目录 (/content/ComfyUI/models/&lt;目录&gt;)</div>
          <div class="ct-ssd-row">
            <input class="ct-ssd-input" id="ct-ssd-model-url"
              placeholder="粘贴模型下载 URL (HuggingFace blob/resolve 链接或直链，粘贴后自动识别目录和文件名)" />
          </div>
          <div class="ct-ssd-row">
            <span style="font-size:12px;color:#a1a1aa;">目标目录:</span>
            <select class="ct-ssd-select" id="ct-ssd-dir-select">${dirOptionsHtml}</select>
            <input class="ct-ssd-input" id="ct-ssd-custom-dir" placeholder="输入自定义子目录名 (如 xlabs/ipadapters)" style="display:none;max-width:190px;" />
            <input class="ct-ssd-input" id="ct-ssd-model-name" placeholder="保存文件名 (留空则从 URL 自动提取)" style="max-width:220px;" />
            <button class="ct-ssd-action model-btn" id="ct-ssd-model-btn">🎯 下载到该目录</button>
          </div>
        </div>

        <div class="ct-ssd-progress-wrap" id="ct-ssd-prog-wrap">
          <div class="ct-ssd-progress-bar" id="ct-ssd-prog-bar"></div>
        </div>
        <div id="ct-ssd-logs">等待指令...（已存在于本地 SSD 的模型会自动跳过，下载完成后自动刷新节点列表）</div>
        <div>
          <div style="font-size:12px;color:#a1a1aa;margin-bottom:4px;font-weight:600;">
            📦 当前本地 SSD 已缓存模型 (/content/ComfyUI/models)：
          </div>
          <div class="ct-ssd-models" id="ct-ssd-model-list">加载中...</div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(backdrop);

  const btn = document.createElement("button");
  btn.id = "ct-ssd-btn";
  btn.innerHTML = "⬇️ SSD 模型下载";
  btn.title = "按工作流或模型 URL 下载到 Colab 本地高速 SSD (已存在的模型自动跳过)";

  function mountButton() {
    const rightMenu = document.querySelector(".comfyui-menu-right") ||
                      document.querySelector(".actionbar-container") ||
                      document.querySelector(".comfyui-menu");
    if (rightMenu && btn.parentElement !== rightMenu) {
      btn.classList.remove("ct-floating");
      rightMenu.prepend(btn);
    } else if (!btn.parentElement) {
      btn.classList.add("ct-floating");
      document.body.appendChild(btn);
    }
  }
  mountButton();
  setInterval(mountButton, 2000);

  const logBox = document.getElementById("ct-ssd-logs");
  const progWrap = document.getElementById("ct-ssd-prog-wrap");
  const progBar = document.getElementById("ct-ssd-prog-bar");
  const freeBadge = document.getElementById("ct-ssd-free");
  const modelListEl = document.getElementById("ct-ssd-model-list");
  const canvasBtn = document.getElementById("ct-ssd-canvas-btn");
  const urlBtn = document.getElementById("ct-ssd-url-btn");
  const specInput = document.getElementById("ct-ssd-spec-input");
  const modelUrlInput = document.getElementById("ct-ssd-model-url");
  const dirSelect = document.getElementById("ct-ssd-dir-select");
  const customDirInput = document.getElementById("ct-ssd-custom-dir");
  const modelNameInput = document.getElementById("ct-ssd-model-name");
  const modelBtn = document.getElementById("ct-ssd-model-btn");
  let pollTimer = null;

  dirSelect.onchange = () => {
    customDirInput.style.display = dirSelect.value === "__custom__" ? "inline-block" : "none";
  };

  modelUrlInput.oninput = () => {
    const raw = modelUrlInput.value.trim();
    if (!raw) return;
    try {
      const clean = decodeURIComponent(raw.split("?")[0]);
      const parts = clean.split("/").filter(Boolean);
      const last = parts[parts.length - 1] || "";
      if (last && /\.(safetensors|ckpt|pt|pth|bin|gguf|sft)$/i.test(last)) {
        modelNameInput.value = last;
      }
      const knownDirs = MODEL_DIRS.map(d => d[0]).filter(d => d !== "__custom__");
      for (const seg of parts) {
        if (knownDirs.includes(seg)) {
          dirSelect.value = seg;
          customDirInput.style.display = "none";
          break;
        }
      }
    } catch (e) {}
  };

  async function refreshStatus() {
    try {
      const r = await fetch("/api/ct/ssd-status", { credentials: "include" });
      const d = await r.json();
      freeBadge.textContent = `SSD 剩余: ${d.free_ssd_gb} GB`;
      modelListEl.replaceChildren();
      if (!d.models || d.models.length === 0) {
        const empty = document.createElement("div");
        empty.style.color = "#71717a";
        empty.textContent = "暂无已下载模型";
        modelListEl.appendChild(empty);
      } else {
        for (const m of d.models) {
          const row = document.createElement("div");
          row.className = "ct-ssd-model-item";
          const pathSpan = document.createElement("span");
          pathSpan.textContent = String(m.path);
          const sizeSpan = document.createElement("span");
          sizeSpan.textContent = `${m.size_gb} GB`;
          row.appendChild(pathSpan);
          row.appendChild(sizeSpan);
          modelListEl.appendChild(row);
        }
      }
      if (d.active_job && !pollTimer) {
        watchJob(d.active_job.job_id);
      }
    } catch (e) {}
  }

  function setBusy(busy) {
    canvasBtn.disabled = busy;
    urlBtn.disabled = busy;
    modelBtn.disabled = busy;
    btn.innerHTML = busy ? "⏳ SSD 下载中..." : "⬇️ SSD 模型下载";
  }

  function watchJob(jobId) {
    if (pollTimer) clearInterval(pollTimer);
    setBusy(true);
    progWrap.style.display = "block";
    pollTimer = setInterval(async () => {
      try {
        const r = await fetch(`/api/ct/pull-workflow/${jobId}`, { credentials: "include" });
        const job = await r.json();
        if (job.logs && job.logs.length) {
          logBox.textContent = job.logs.join("\n");
          logBox.scrollTop = logBox.scrollHeight;
        }
        const pct = job.total > 0 ? Math.round((job.completed / job.total) * 100) : 15;
        progBar.style.width = `${pct}%`;
        if (job.status === "done" || job.status === "error") {
          clearInterval(pollTimer);
          pollTimer = null;
          setBusy(false);
          progBar.style.width = job.status === "done" ? "100%" : "0%";
          await refreshStatus();
          if (job.status === "done") {
            logBox.textContent += "\n✅ 全部模型已就绪！正在自动刷新 ComfyUI 节点模型列表...";
            logBox.scrollTop = logBox.scrollHeight;
            try { await window.app?.refreshComboInNodes?.(); } catch (e) {}
          }
        }
      } catch (e) {}
    }, 1500);
  }

  async function startJob(payload) {
    setBusy(true);
    logBox.textContent = "正在提交下载任务...";
    progWrap.style.display = "block";
    progBar.style.width = "10%";
    try {
      const r = await fetch("/api/ct/pull-workflow", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok) {
        logBox.textContent = `❌ 错误: ${data.error || r.statusText}`;
        setBusy(false);
        return;
      }
      watchJob(data.job_id);
    } catch (e) {
      logBox.textContent = `❌ 请求失败: ${e}`;
      setBusy(false);
    }
  }

  btn.onclick = () => {
    backdrop.style.display = "flex";
    refreshStatus();
  };
  document.getElementById("ct-ssd-close-btn").onclick = () => {
    backdrop.style.display = "none";
  };
  backdrop.onclick = (e) => {
    if (e.target === backdrop) backdrop.style.display = "none";
  };
  canvasBtn.onclick = () => {
    const wfJson = window.app?.graph?.serialize?.();
    if (!wfJson || !wfJson.nodes || !wfJson.nodes.length) {
      logBox.textContent = "⚠️ 当前画布为空，请先在画布中打开一个工作流，或在下方输入工作流 URL / 模板名。";
      return;
    }
    startJob({ workflow_json: wfJson, workflow_name: "current_canvas_workflow.json" });
  };
  urlBtn.onclick = () => {
    const spec = specInput.value.trim();
    if (!spec) {
      logBox.textContent = "⚠️ 请输入工作流 GitHub URL、官方模板名或网盘工作流文件名。";
      return;
    }
    startJob({ workflow: spec });
  };
  modelBtn.onclick = () => {
    const mUrl = modelUrlInput.value.trim();
    if (!mUrl || !/^https?:\/\//i.test(mUrl)) {
      logBox.textContent = "⚠️ 请输入有效的模型下载 URL (http:// 或 https://)。";
      return;
    }
    const targetDir = (dirSelect.value === "__custom__" ? customDirInput.value : dirSelect.value).trim().replace(/^\/+|\/+$/g, "");
    if (!targetDir) {
      logBox.textContent = "⚠️ 请选择或输入目标模型子目录（如 loras, diffusion_models, vae）。";
      return;
    }
    let mName = modelNameInput.value.trim().replace(/^\/+/, "");
    if (!mName) {
      try {
        mName = decodeURIComponent(mUrl.split("?")[0]).split("/").filter(Boolean).pop() || "";
      } catch (e) {}
    }
    if (!mName) {
      logBox.textContent = "⚠️ 无法从 URL 自动提取文件名，请在「保存文件名」输入框中指定文件名（如 model.safetensors）。";
      return;
    }
    startJob({
      model_url: mUrl,
      directory: targetDir,
      filename: mName,
      workflow_name: `direct_model:${targetDir}/${mName}`,
      workflow_json: {
        properties: {
          models: [{ url: mUrl, directory: targetDir, name: mName }]
        }
      }
    });
  };
})();
