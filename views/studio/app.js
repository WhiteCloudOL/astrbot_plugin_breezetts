const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);
let state;
let draft;
let dirty = false;
let currentAudio;
let objectUrl;
let preview;
let editingReference;
let deletingAudio;
let catalog = { voices: [], page: 1, next_page_token: "", has_more: false };
let catalogFilter;
let disposed = false;
let noticeTimer;
const titles = {
  studio: "让文字，有声音。",
  voices: "收藏你的声音。",
  settings: "准备好，开始发声。",
};

function notice(message, error = false) {
  clearTimeout(noticeTimer);
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = false;
  if (!error)
    noticeTimer = setTimeout(() => {
      $("notice").hidden = true;
    }, 6500);
}

async function action(button, operation) {
  if (button.disabled) return;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await operation();
  } catch (error) {
    notice(error.message || "操作失败，请稍后重试。", true);
  } finally {
    button.disabled = button.id === "save" && !dirty;
    button.removeAttribute("aria-busy");
  }
}

function navigate(page) {
  if (!titles[page]) page = "studio";
  document.querySelectorAll(".page").forEach((element) => {
    element.hidden = element.id !== `page-${page}`;
  });
  document.querySelectorAll(".nav").forEach((element) => {
    const selected = element.dataset.page === page;
    element.classList.toggle("active", selected);
    if (selected) element.setAttribute("aria-current", "page");
    else element.removeAttribute("aria-current");
  });
  $("page-title").textContent = titles[page];
  history.replaceState(null, "", `#${page}`);
  window.scrollTo(0, 0);
}

function markDirty() {
  dirty = true;
  $("save-state").textContent = "有未保存的更改";
  $("save").disabled = false;
  updateSummary();
}

function updateSummary() {
  if (!draft) return;
  const cloud = draft.backend === "cloud";
  const backendName = cloud ? "BreezeBlue 官方" : "本地 API";
  $("backend-label").textContent = backendName;
  $("studio-backend").textContent = backendName;
  $("local-settings").hidden = cloud;
  $("cloud-settings").hidden = !cloud;
  const reference = state.audio.find(
    (item) => item.id === draft.local.reference_id,
  );
  const voice = catalog.voices.find(
    (item) => item.voice_id === draft.cloud.voice_id,
  );
  $("selected-voice").textContent = cloud
    ? voice?.name || draft.cloud.voice_id || "尚未选择官方音色"
    : draft.local.mode === "reference"
      ? reference?.name || "尚未选择参考录音"
      : draft.local.mode === "design"
        ? "文字声音设计"
        : "普通朗读";
  $("voice-hint").textContent = cloud
    ? "使用官方已保存音色，支持表演指令与语速控制。"
    : draft.local.mode === "reference"
      ? reference?.ref_text
        ? "参考录音与文字稿已配对。"
        : "请在音色库编辑录音，填写准确文字稿。"
      : "在连接与设置中调整声音描述和 CFG。";
  $("synthesis-hint").textContent = cloud
    ? `官方输出 · ${draft.cloud.output_format.toUpperCase()} · ${draft.cloud.delivery}`
    : "本地 PCM 自动封装为 WAV";
  $("text-count").textContent =
    `${$("synthesis-text").value.length} / ${draft.behavior.max_characters}`;
}

function fillSettings() {
  document.querySelectorAll("[data-config]").forEach((element) => {
    const parts = element.dataset.config.split(".");
    const value =
      parts.length === 1 ? draft[parts[0]] : draft[parts[0]][parts[1]];
    if (element.type === "checkbox") element.checked = value;
    else element.value = value;
  });
  for (const backend of ["local", "cloud"]) {
    $(`${backend}-key`).placeholder = state.credentials[backend]
      ? "已配置密钥；留空保留，输入新值替换"
      : "尚未配置密钥";
    $(`clear-${backend}-key`).checked = false;
  }
  $("save-state").textContent = "配置已保存";
  $("save").disabled = true;
  dirty = false;
  updateSummary();
}

async function saveSettings() {
  for (const element of document.querySelectorAll("[data-config]")) {
    if (!element.closest("[hidden]") && !element.reportValidity())
      throw new Error("请检查配置字段的格式和取值范围。");
  }
  const clearKeys = ["local", "cloud"].filter(
    (backend) => $(`clear-${backend}-key`).checked,
  );
  state = await bridge.apiPost("settings", {
    config: draft,
    clear_keys: clearKeys,
  });
  draft = structuredClone(state.config);
  renderAudio();
  fillSettings();
}

async function refreshAudio() {
  const next = await bridge.apiGet("state");
  state.audio = next.audio;
  renderAudio();
  updateSummary();
}

function button(label, operation, className = "text-button") {
  const element = document.createElement("button");
  element.type = "button";
  element.className = className;
  element.textContent = label;
  element.addEventListener("click", () => action(element, operation));
  return element;
}

function empty(target, text) {
  const paragraph = document.createElement("p");
  paragraph.className = "empty";
  paragraph.textContent = text;
  target.replaceChildren(paragraph);
}

function renderAudio() {
  const references = state.audio.filter((item) => item.kind === "reference");
  $("reference-count").textContent = references.length;
  for (const id of ["local-reference", "clone-reference"]) {
    const select = $(id);
    const oldValue =
      id === "local-reference" ? draft.local.reference_id : select.value;
    select.replaceChildren(new Option("请选择参考录音", ""));
    for (const item of references)
      select.add(new Option(`${item.name} · ${item.duration}s`, item.id));
    select.value = oldValue;
  }
  $("references").replaceChildren();
  if (!references.length)
    empty(
      $("references"),
      "还没有参考录音。点击“上传录音”，为本地复刻或官方克隆准备声音。",
    );
  for (const item of references) {
    const card = document.createElement("article");
    card.className = `reference-card${item.id === draft.local.reference_id ? " selected" : ""}`;
    const title = document.createElement("strong");
    title.textContent = item.name;
    const meta = document.createElement("small");
    meta.textContent = `${item.duration} 秒 · ${item.sample_rate / 1000} kHz · ${(item.size / 1024).toFixed(0)} KiB`;
    const text = document.createElement("p");
    text.textContent = item.ref_text || "尚未填写文字稿 · 本地复刻前请先编辑";
    const controls = document.createElement("div");
    controls.className = "button-row";
    controls.append(
      button("试听", () => play(item)),
      button("下载", () => download(item)),
      button("编辑", async () => {
        editingReference = item;
        $("reference-name").value = item.name;
        $("reference-text").value = item.ref_text;
        $("reference-dialog").showModal();
      }),
      button(
        item.id === draft.local.reference_id ? "默认音色 ✓" : "设为本地音色",
        async () => {
          if (!item.ref_text)
            throw new Error("请先编辑此录音并填写准确文字稿。");
          draft.local.reference_id = item.id;
          draft.local.mode = "reference";
          draft.backend = "local";
          await saveSettings();
          notice("已保存为默认本地音色。");
        },
      ),
      button("删除", async () => askDelete(item)),
    );
    card.append(title, meta, text, controls);
    $("references").append(card);
  }
  $("history").replaceChildren();
  const outputs = state.audio.filter((item) => item.kind !== "reference");
  if (!outputs.length)
    empty($("history"), "还没有合成记录。写一段文字，生成第一条语音。");
  for (const item of outputs) {
    const row = document.createElement("article");
    row.className = "audio-row";
    const icon = document.createElement("span");
    icon.className = "voice-mark";
    icon.textContent = "≋";
    icon.setAttribute("aria-hidden", "true");
    const info = document.createElement("div");
    info.className = "audio-info";
    const title = document.createElement("div");
    title.className = "audio-title";
    title.textContent = item.name;
    const meta = document.createElement("span");
    meta.className = "audio-meta";
    meta.textContent = `${item.kind === "preview" ? "音色预览" : item.backend === "cloud" ? "官方合成" : "本地合成"} · ${item.extension.toUpperCase()} · ${(item.size / 1024).toFixed(0)} KiB · ${new Date(item.created * 1000).toLocaleString()}`;
    info.append(title, meta);
    const controls = document.createElement("div");
    controls.className = "audio-actions";
    controls.append(
      button("试听", () => play(item)),
      button("下载 ↓", () => download(item)),
      button("删除", async () => askDelete(item)),
    );
    row.append(icon, info, controls);
    $("history").append(row);
  }
}

function askDelete(item) {
  deletingAudio = item;
  $("delete-message").textContent =
    `确定删除“${item.name}”及其音频文件？此操作无法撤销。`;
  $("delete-dialog").showModal();
}

async function download(item) {
  await bridge.download(
    "audio/download",
    { id: item.id },
    `breezetts-${item.id.slice(0, 8)}.${item.extension}`,
  );
}

async function play(item) {
  $("player-name").textContent = "正在载入音频……";
  const chunks = [];
  let offset = 0;
  let mime;
  while (!disposed) {
    const chunk = await bridge.apiGet("audio/chunk", { id: item.id, offset });
    if (chunk.next < offset || (!chunk.eof && chunk.next === offset))
      throw new Error("音频读取未完成，请刷新后重试。");
    chunks.push(
      Uint8Array.from(atob(chunk.data), (character) => character.charCodeAt(0)),
    );
    offset = chunk.next;
    mime = chunk.mime;
    if (chunk.eof) break;
  }
  if (disposed) return;
  $("player").pause();
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = URL.createObjectURL(new Blob(chunks, { type: mime }));
  $("player").src = objectUrl;
  $("player-name").textContent = item.name;
  currentAudio = item;
  $("download-current").disabled = false;
  await $("player")
    .play()
    .catch(() => {
      notice("音频已载入，点击播放器开始试听。");
    });
  if ($("page-studio").hidden)
    notice("音频已载入，可在“合成试听”中查看播放器。");
}

async function runJob(operation, payload, onProgress) {
  const job = await bridge.apiPost("jobs/submit", { operation, payload });
  while (!disposed) {
    const result = await bridge.apiGet("jobs/status", { id: job.id });
    if (result.status === "ready") return result.result;
    if (result.status !== "running")
      throw new Error(result.error || "任务未完成。");
    if (onProgress) onProgress();
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
  throw new Error("页面已关闭。");
}

async function loadVoices(more = false) {
  if (dirty) await saveSettings();
  if (!state.credentials.cloud)
    throw new Error("请先在连接设置中保存官方 API Key。");
  if (!more) {
    catalog = { voices: [], page: 1, next_page_token: "", has_more: false };
    catalogFilter = {
      voice_type: $("voice-type").value,
      search: $("voice-search").value,
    };
  }
  const next = await bridge.apiGet("voices", {
    ...catalogFilter,
    token: more ? catalog.next_page_token : "",
    page: more ? catalog.page + 1 : 1,
  });
  catalog = {
    ...next,
    voices: [
      ...catalog.voices,
      ...next.voices.filter(
        (voice) =>
          !catalog.voices.some(
            (existing) => existing.voice_id === voice.voice_id,
          ),
      ),
    ],
  };
  $("cloud-voices").replaceChildren();
  if (!catalog.voices.length)
    empty($("cloud-voices"), "没有找到匹配的音色，请尝试其他关键词或范围。");
  for (const voice of catalog.voices) {
    const card = document.createElement("article");
    card.className = "voice-card";
    const name = document.createElement("strong");
    name.textContent = voice.name;
    const id = document.createElement("small");
    id.textContent = voice.voice_id;
    const meta = document.createElement("small");
    meta.textContent = [voice.origin, voice.language_code]
      .filter(Boolean)
      .join(" · ");
    card.append(
      name,
      id,
      meta,
      button(
        "使用此音色",
        async () => {
          draft.cloud.voice_id = voice.voice_id;
          draft.backend = "cloud";
          await saveSettings();
          notice(`已选用“${voice.name}”。`);
        },
        "secondary",
      ),
    );
    $("cloud-voices").append(card);
  }
  $("more-voices").hidden = !catalog.has_more;
  updateSummary();
}

if (!bridge) {
  notice("请从 AstrBot WebUI 的插件详情页打开 BreezeTTS 工作台。", true);
} else {
  try {
    await bridge.ready();
    state = await bridge.apiGet("state");
    draft = structuredClone(state.config);
    renderAudio();
    fillSettings();
    navigate(location.hash.slice(1) || "studio");
  } catch (error) {
    notice(error.message || "工作台初始化失败，请重载插件后刷新。", true);
  }
}

document
  .querySelectorAll("[data-page]")
  .forEach((element) =>
    element.addEventListener("click", () => navigate(element.dataset.page)),
  );
$("go-voices").addEventListener("click", () => navigate("voices"));
window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
document.querySelectorAll("[data-config]").forEach((element) =>
  element.addEventListener("input", () => {
    if (!draft) return;
    const parts = element.dataset.config.split(".");
    const value =
      element.type === "checkbox"
        ? element.checked
        : element.type === "number"
          ? Number(element.value)
          : element.value;
    if (parts.length === 1) draft[parts[0]] = value;
    else draft[parts[0]][parts[1]] = value;
    markDirty();
  }),
);
for (const backend of ["local", "cloud"])
  $(`clear-${backend}-key`).addEventListener("change", markDirty);
$("synthesis-text").addEventListener("input", updateSummary);
$("save").addEventListener("click", () =>
  action($("save"), async () => {
    await saveSettings();
    notice("配置已保存。");
    $("save").disabled = true;
  }),
);
$("refresh-audio").addEventListener("click", () =>
  action($("refresh-audio"), refreshAudio),
);
$("download-current").addEventListener("click", () =>
  action($("download-current"), () => download(currentAudio)),
);
$("player").addEventListener("error", () =>
  notice("浏览器无法播放此格式，可下载文件或将官方输出格式改为 WAV。", true),
);
$("synthesize").addEventListener("click", () =>
  action($("synthesize"), async () => {
    if (!draft) throw new Error("配置尚未加载，请刷新工作台。");
    const text = $("synthesis-text").value;
    if (!text.trim() || text.length > draft.behavior.max_characters)
      throw new Error(`请输入 1–${draft.behavior.max_characters} 个字符。`);
    if (dirty) await saveSettings();
    const started = Date.now();
    $("job-status").hidden = false;
    try {
      const item = await runJob("synthesize", { text }, () => {
        $("job-status").textContent =
          `正在排队或合成 · 已等待 ${Math.floor((Date.now() - started) / 1000)} 秒`;
      });
      await refreshAudio();
      await play(item);
      notice("语音已生成并保存。");
    } finally {
      $("job-status").hidden = true;
    }
  }),
);
$("upload").addEventListener("change", async () => {
  const file = $("upload").files[0];
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) {
    notice("文件超过 20 MiB 上限。", true);
    $("upload").value = "";
    return;
  }
  $("upload").disabled = true;
  try {
    const item = await bridge.upload("audio/upload", file);
    await refreshAudio();
    editingReference = item;
    $("reference-name").value = item.name;
    $("reference-text").value = "";
    $("reference-dialog").showModal();
    notice("录音已上传，请填写准确文字稿。");
  } catch (error) {
    notice(error.message, true);
  } finally {
    $("upload").disabled = false;
    $("upload").value = "";
  }
});
$("reference-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(event.submitter, async () => {
    await bridge.apiPost("audio/reference", {
      id: editingReference.id,
      name: $("reference-name").value,
      ref_text: $("reference-text").value,
    });
    $("reference-dialog").close();
    await refreshAudio();
    notice("参考录音信息已保存。");
  });
});
$("close-reference").addEventListener("click", () =>
  $("reference-dialog").close(),
);
$("cancel-delete").addEventListener("click", () => $("delete-dialog").close());
$("confirm-delete").addEventListener("click", () =>
  action($("confirm-delete"), async () => {
    await bridge.apiPost("audio/delete", { id: deletingAudio.id });
    if (currentAudio?.id === deletingAudio.id) {
      $("player").pause();
      $("player").removeAttribute("src");
      $("player").load();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = undefined;
      currentAudio = undefined;
      $("download-current").disabled = true;
      $("player-name").textContent = "生成后，在这里试听";
    }
    $("delete-dialog").close();
    await refreshAudio();
    notice("音频已删除。");
  }),
);
$("test-connection").addEventListener("click", () =>
  action($("test-connection"), async () => {
    await saveSettings();
    $("connection-result").textContent = "正在检查……";
    try {
      const result = await bridge.apiPost("connection", {});
      $("connection-result").textContent = "连接成功";
      if (result.models)
        $("models").replaceChildren(
          ...result.models.map(
            (item) => new Option(item.model_id, item.model_id),
          ),
        );
      notice("连接成功，配置已保存。");
    } catch (error) {
      $("connection-result").textContent = "连接失败";
      throw error;
    }
  }),
);
for (const [id, url] of [
  ["preset-local", "http://127.0.0.1:7860"],
  ["preset-auth", "http://127.0.0.1:7861"],
]) {
  $(id).addEventListener("click", () => {
    draft.local.base_url = url;
    $("local-url").value = url;
    if (id === "preset-local") {
      draft.local.api_key = "";
      $("local-key").value = "";
      $("clear-local-key").checked = true;
    }
    markDirty();
  });
}
$("voice-search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action($("sync-voices"), () => loadVoices());
});
$("more-voices").addEventListener("click", () =>
  action($("more-voices"), () => loadVoices(true)),
);
$("preview-kind").addEventListener("change", () => {
  const design = $("preview-kind").value === "design";
  $("design-fields").hidden = !design;
  $("clone-fields").hidden = design;
});
$("preview-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action($("create-preview"), async () => {
    if (dirty) await saveSettings();
    if (draft.backend !== "cloud")
      throw new Error("请在连接设置中切换并保存官方 API。");
    $("preview-result").hidden = true;
    notice("正在创建官方音色预览，请稍候。可在最近生成中查看试听音频。");
    preview = await runJob($("preview-kind").value, {
      name: $("preview-name").value,
      language_code: $("preview-language").value,
      reference_id: $("clone-reference").value,
      remove_noise: $("remove-noise").checked,
      description: $("voice-description").value,
      text: $("preview-text").value,
    });
    $("preview-message").textContent = preview.requires_verification
      ? `此音色需在官方控制台完成验证。预览 ID：${preview.preview_id}`
      : `“${preview.name}”预览已生成。请先试听，再保存到官方账号。`;
    $("save-preview").hidden = preview.requires_verification;
    $("play-preview").hidden = !preview.audio;
    $("preview-result").hidden = false;
    await refreshAudio();
  });
});
$("play-preview").addEventListener("click", () =>
  action($("play-preview"), () => play(preview.audio)),
);
$("save-preview").addEventListener("click", () =>
  action($("save-preview"), async () => {
    const saved = await bridge.apiPost("voices/save-preview", {
      preview_id: preview.preview_id,
      name: preview.name,
      language_code: preview.language_code,
    });
    draft.cloud.voice_id = saved.voice_id;
    await saveSettings();
    $("preview-result").hidden = true;
    notice("音色已保存并设为当前官方音色。");
  }),
);
window.addEventListener("beforeunload", () => {
  disposed = true;
  clearTimeout(noticeTimer);
  if (objectUrl) URL.revokeObjectURL(objectUrl);
});
