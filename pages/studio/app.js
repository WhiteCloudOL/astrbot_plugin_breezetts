const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
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
let playbackSequence = 0;
let settingsSaving = false;
let editingPrompt;
let expressionProfile;
let editingExpression;
const clearKeys = new Set();
const titles = {
  studio: "合成试听",
  voices: "音色管理",
  settings: "连接设置",
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
  const wasDisabled = button.disabled;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await operation();
  } catch (error) {
    notice(error.message || "操作失败，请稍后重试。", true);
  } finally {
    button.disabled = button.id === "save" ? !dirty : wasDisabled;
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
  $("save").disabled = settingsSaving;
  updateSummary();
}

function updateSummary() {
  if (!draft) return;
  const generated = state.audio.filter((item) => item.kind !== "reference");
  const cacheMiB = generated.reduce((total, item) => total + item.size, 0) / 1024 / 1024;
  $("cache-usage").textContent = `当前成品与预览：${generated.length} 条 · ${cacheMiB.toFixed(1)} MiB`;
  $("trigger-probability").disabled = !draft.behavior.auto_tts;
  const missingToneSettings = [
    !draft.behavior.auto_tts && "随机语音",
    !draft.behavior.dynamic_tone && "动态语调",
  ].filter(Boolean);
  $("random-tone-hint").textContent = missingToneSettings.length
    ? `还需开启“${missingToneSettings.join("”和“")}”才会生效；开启自适应时可在弹窗中一并启用。`
    : draft.behavior.random_tone_adaptive
      ? "已启用：概率命中后请所选 LLM 推荐语气或标签，可不添加，台词保持原样；20 秒超时或失败回退默认语气。"
      : "开启后，概率命中时请所选 LLM 推荐语气或标签，可不添加，台词保持原样；20 秒超时或失败回退默认语气。";
  $("fixed-seed-field").hidden = draft.local.random_seed;
  $("seed").disabled = draft.local.random_seed;
  for (const scope of ["private", "group"]) {
    const inherit = draft.behavior[`${scope}_trigger_probability`] < 0;
    $(`${scope}-probability-mode`).value = inherit ? "inherit" : "custom";
    $(`${scope}-probability-mode`).disabled = !draft.behavior.auto_tts;
    $(`${scope}-probability-field`).hidden = inherit;
    $(`${scope}-trigger-probability`).disabled = !draft.behavior.auto_tts;
    $(`${scope}-trigger-probability`).min = inherit ? "-1" : "0";
  }
  const cloud = draft.backend === "cloud";
  const hosted = draft.backend === "provider";
  const backendName = hosted ? "AstrBot TTS" : cloud ? "BreezeBlue 官方" : "本地 API";
  $("provider-settings").hidden = !hosted;
  fillExpression();
  $("backend-label").textContent = backendName;
  $("studio-backend").textContent = backendName;
  $("local-settings").hidden = cloud || hosted;
  $("cloud-settings").hidden = !cloud;
  $("local-voices-panel").hidden = cloud || hosted;
  $("cloud-voices-panel").hidden = !cloud;
  $("voice-page-title").textContent = hosted ? "AstrBot 提供商音色" : cloud
    ? "BreezeBlue 官方音色"
    : "本地参考音色";
  $("voice-page-hint").textContent = hosted ? "请在 AstrBot 的 TTS 提供商配置中调整音色和声音参数。" : cloud
    ? "查询账号音色，或创建克隆与设计音色"
    : "上传录音、填写准确文字稿，再设为当前音色";

  const reference = state.audio.find(
    (item) => item.id === draft.local.reference_id,
  );
  const voice = catalog.voices.find(
    (item) => item.voice_id === draft.cloud.voice_id,
  );
  $("selected-voice").textContent = hosted ? draft.provider.provider_id || "请选择 TTS 提供商" : cloud
    ? voice?.name || draft.cloud.voice_id || "尚未选择官方音色"
    : draft.local.mode === "reference"
      ? reference?.name || "尚未选择参考录音"
      : draft.local.mode === "design"
        ? "文字声音设计"
        : "普通朗读";
  $("voice-hint").textContent = hosted ? "音色在 AstrBot 提供商配置中调整。" : cloud
    ? "使用官方已保存音色，支持表演指令与语速控制。"
    : draft.local.mode === "reference"
      ? reference?.ref_text
        ? "参考录音与文字稿已配对。"
        : "请在音色库编辑录音，填写准确文字稿。"
      : "在连接与设置中调整声音描述和 CFG。";
  $("synthesis-hint").textContent = hosted ? "复用 AstrBot TTS · PCM WAV" : cloud
    ? `官方输出 · ${draft.cloud.output_format.toUpperCase()} · ${draft.cloud.delivery}`
    : "本地 PCM 自动封装为 WAV";
  $("text-count").textContent =
    `${Array.from($("synthesis-text").value).length} / ${draft.behavior.max_characters}`;
}

function fillExpression() {
  let preset = draft.backend === "provider"
    ? state.tts_providers?.find((p) => p.id === draft.provider.provider_id)?.profile
    : state.expression_presets?.[`breeze_${draft.backend}`];
  const dashscope = draft.backend === "provider" && state.tts_providers?.find((p) => p.id === draft.provider.provider_id)?.type === "dashscope_tts";
  $("provider-protocol").hidden = !dashscope;
  if (dashscope && draft.provider.preset) preset = {...state.expression_presets[draft.provider.preset], key: draft.provider.preset};
  const key = preset?.key || (draft.backend === "provider" ? "" : `breeze_${draft.backend}`);
  $("expression-controls").hidden = !key;
  if (!key) { expressionProfile = undefined; return; }
  expressionProfile = {...preset, key};
  $("expression-policy").textContent = `${preset.name} · ${preset.tag_policy === "reference" ? "推荐参考，允许表外自由语气" : preset.mode === "fixed" ? "严格固定值" : "自由指令"}。${preset.note}`;
}

function fillSettings() {
  const tts = new Map((state.tts_providers || []).map((p) => [p.id, `${p.name} · ${p.profile.name}`]));
  if (draft.provider.provider_id && !tts.has(draft.provider.provider_id)) tts.set(draft.provider.provider_id, `${draft.provider.provider_id}（不可用或模型不支持）`);
  $("tts-provider").replaceChildren(new Option("请选择已配置的 TTS 提供商", ""), ...Array.from(tts, ([id, name]) => new Option(name, id)));
  fillExpression();
  const providers = new Map((state.llm_providers || []).map((provider) => [provider.id, provider.name]));
  if (draft.behavior.tone_provider_id && !providers.has(draft.behavior.tone_provider_id))
    providers.set(draft.behavior.tone_provider_id, `${draft.behavior.tone_provider_id}（当前不可用）`);
  $("tone-provider").replaceChildren(new Option("跟随当前会话模型", ""), ...Array.from(providers, ([id, name]) => new Option(name, id)));
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

    $(`clear-${backend}-key`).textContent = "清空";
    $(`clear-${backend}-key`).disabled = !state.credentials[backend];
    $(`${backend}-key-state`).textContent = state.credentials[backend]
      ? "已配置密钥 · 输入新值可替换"
      : backend === "local"
        ? "当前未配置密钥 · 自动使用无鉴权连接"
        : "填写官方开发者 API Key";
  }
  $("save-state").textContent = "配置已保存";
  $("save").disabled = true;
  dirty = false;
  clearKeys.clear();
  updateSummary();
}

async function saveSettings() {
  if (settingsSaving) throw new Error("配置正在保存，请稍候");
  for (const element of document.querySelectorAll("[data-config]")) {
    if (!element.closest("[hidden]") && !element.reportValidity())
      throw new Error("请检查配置字段的格式和取值范围");
  }
  settingsSaving = true;
  const snapshot = structuredClone(draft);
  const cleared = [...clearKeys];
  try {
    const saved = await bridge.apiPost("settings", {
      config: snapshot,
      clear_keys: cleared,
    });
    const editedDuringSave =
      JSON.stringify(draft) !== JSON.stringify(snapshot) ||
      JSON.stringify([...clearKeys]) !== JSON.stringify(cleared);
    state = saved;
    if (!editedDuringSave) {
      draft = structuredClone(state.config);
      fillSettings();
    } else notice("配置已保存，保存期间的新更改仍待保存");
    renderAudio();
    updateSummary();
  } finally {
    settingsSaving = false;
  }
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
    const selected = item.id === draft.local.reference_id;
    const card = document.createElement("article");
    card.className = `reference-card${selected ? " selected" : ""}`;
    const title = document.createElement("strong");
    title.textContent = item.name;
    const meta = document.createElement("small");
    meta.textContent = `${item.duration} 秒 · ${item.sample_rate / 1000} kHz · ${(item.size / 1024).toFixed(0)} KiB`;
    const text = document.createElement("p");
    text.className = "reference-transcript";
    text.textContent =
      item.ref_text || "填写准确文字稿后，即可用于本地音色复刻";
    const select = button(
      selected ? "当前音色 ✓" : "设为当前音色",
      async () => {
        if (!item.ref_text) {
          editingReference = item;
          $("reference-name").value = item.name;
          $("reference-text").value = "";
          $("reference-dialog").showModal();
          notice("先填写录音文字稿，保存后即可设置音色");
          return;
        }
        draft.local.reference_id = item.id;
        draft.local.mode = "reference";
        draft.backend = "local";
        await saveSettings();
        notice("已保存为当前本地音色");
      },
      "primary select-voice",
    );
    select.disabled = selected;
    const controls = document.createElement("div");
    controls.className = "reference-actions";
    controls.append(
      button("试听", () => play(item), "secondary"),
      button("下载", () => download(item), "secondary"),
      button(
        "编辑信息",
        async () => {
          editingReference = item;
          $("reference-name").value = item.name;
          $("reference-text").value = item.ref_text;
          $("reference-dialog").showModal();
        },
        "secondary",
      ),
    );
    const footer = document.createElement("div");
    footer.className = "reference-footer";
    const transcript = document.createElement("span");
    transcript.textContent = item.ref_text ? "文字稿已配对" : "待填写文字稿";
    const remove = button(
      "删除录音",
      async () => askDelete(item),
      "danger-text",
    );
    remove.disabled = selected;
    if (selected) remove.title = "更换当前音色后可删除这条录音";
    footer.append(transcript, remove);
    card.append(title, meta, text, select, controls, footer);
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
    meta.textContent = `${item.kind === "preview" ? "音色预览" : item.backend === "provider" ? "AstrBot TTS" : item.backend === "cloud" ? "官方合成" : "本地合成"} · ${item.extension.toUpperCase()} · ${(item.size / 1024).toFixed(0)} KiB · ${new Date(item.created * 1000).toLocaleString()}`;
    info.append(title, meta);
    const controls = document.createElement("div");
    controls.className = "audio-actions";
    controls.append(
      button("试听", () => play(item), "secondary"),
      button("下载", () => download(item), "secondary"),
      button("删除", async () => askDelete(item), "danger-text"),
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

async function play(item, { autoplay = true } = {}) {
  const sequence = ++playbackSequence;
  currentAudio = item;
  if (autoplay) navigate("studio");
  $("player-name").textContent = item.name;
  $("player-detail").textContent = "正在载入音频…";
  $("player-state").textContent = "载入中";
  $("download-current").disabled = false;
  $("retry-playback").hidden = true;
  $("player").pause();
  $("player").hidden = true;
  const chunks = [];
  let offset = 0;
  let mime;
  try {
    while (!disposed) {
      const chunk = await bridge.apiGet("audio/chunk", { id: item.id, offset });
      if (sequence !== playbackSequence) return;
      if (
        typeof chunk.audio_base64 !== "string" ||
        !Number.isSafeInteger(chunk.next) ||
        chunk.next < offset ||
        chunk.next > 64 * 1024 * 1024 + 44 ||
        typeof chunk.eof !== "boolean" ||
        (!chunk.eof && chunk.next === offset)
      )
        throw new Error("音频分块无效");
      const bytes = Uint8Array.from(atob(chunk.audio_base64), (character) =>
        character.charCodeAt(0),
      );
      if (bytes.length !== chunk.next - offset)
        throw new Error("音频分块长度不一致");
      chunks.push(bytes);
      offset = chunk.next;
      mime = chunk.mime;
      if (chunk.eof) break;
    }
    if (disposed || sequence !== playbackSequence) return;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = URL.createObjectURL(new Blob(chunks, { type: mime }));
    $("player").src = objectUrl;
    $("player").hidden = false;
    $("player").load();
    $("player-detail").textContent =
      `${item.extension.toUpperCase()} · ${(item.size / 1024).toFixed(0)} KiB`;
    $("player-state").textContent = "已载入";
    if (autoplay && matchMedia("(max-width: 900px)").matches)
      $("player").scrollIntoView({
        block: "center",
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto"
          : "smooth",
      });
    if (autoplay)
      await $("player")
        .play()
        .catch(() => {
          $("player-state").textContent = "点击播放";
        });
  } catch {
    if (sequence !== playbackSequence || disposed) return;
    $("player-state").textContent = "载入失败";
    $("player-detail").textContent = "音频已保存，可重试载入或直接下载";
    $("retry-playback").hidden = false;
    throw new Error("试听载入失败，音频仍可下载，请点击“重新载入音频”重试");
  }
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
    card.dataset.voiceId = voice.voice_id;
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
          document.querySelectorAll(".voice-card").forEach((entry) => {
            const selected = entry.dataset.voiceId === voice.voice_id;
            entry.classList.toggle("selected", selected);
            const control = entry.querySelector("button");
            control.disabled = selected;
            control.textContent = selected ? "当前音色 ✓" : "使用此音色";
          });
          notice(`已选用“${voice.name}”`);
        },
        "primary select-voice",
      ),
    );
    if (voice.voice_id === draft.cloud.voice_id) {
      card.querySelector("button").disabled = true;
      card.querySelector("button").textContent = "当前音色 ✓";
    }
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
    if ([...document.querySelectorAll("[data-config]")].some((element) => {
      const [group, key] = element.dataset.config.split(".");
      const section = key ? state.config?.[group] : state.config;
      return !section || !Object.hasOwn(section, key || group);
    }) || !state.prompt_variables || !state.prompt_defaults || !state.config?.expression || !state.expression_presets ||
        typeof state.config?.behavior?.speech_prompt !== "string" || typeof state.config?.behavior?.tone_prompt !== "string") {
      throw new Error("插件文件已更新，请在插件管理中重载 BreezeTTS，再刷新工作台");
    }
    draft = structuredClone(state.config);
    renderAudio();
    fillSettings();
    navigate(location.hash.slice(1) || "studio");
    const latest = state.audio.find((item) => item.kind === "output");
    if (latest)
      play(latest, { autoplay: false }).catch((error) =>
        notice(error.message, true),
      );
  } catch (error) {
    document.querySelectorAll("main button, main input, main select, main textarea")
      .forEach((element) => { element.disabled = true; });
    notice(error.message || "工作台初始化失败，请重载插件后刷新。", true);
  }
}

document
  .querySelectorAll("[data-page]")
  .forEach((element) =>
    element.addEventListener("click", () => navigate(element.dataset.page)),
  );
$("go-voices").addEventListener("click", () => navigate("voices"));
$("voice-settings").addEventListener("click", () => navigate("settings"));
$("upload-for-clone").addEventListener("click", () => $("upload").click());
$("retry-playback").addEventListener("click", () =>
  action($("retry-playback"), async () => {
    await play(currentAudio);
    notice("音频已重新载入");
  }),
);
window.addEventListener("hashchange", () => {
  const page = location.hash.slice(1);
  if (titles[page]) navigate(page);
});
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
    if (element.id === "random-tone-adaptive" && value &&
        (!draft.behavior.auto_tts || !draft.behavior.dynamic_tone)) {
      $("enable-tone-dialog").showModal();
      return;
    }
    if (parts.length === 1) draft[parts[0]] = value;
    else draft[parts[0]][parts[1]] = value;
    markDirty();
  }),
);
$("cancel-enable-tone").addEventListener("click", () => $("enable-tone-dialog").close());
$("confirm-enable-tone").addEventListener("click", () => {
  draft.behavior.auto_tts = true;
  draft.behavior.dynamic_tone = true;
  draft.behavior.random_tone_adaptive = true;
  document.querySelector('[data-config="behavior.auto_tts"]').checked = true;
  $("dynamic-tone").checked = true;
  $("enable-tone-dialog").close();
  markDirty();
});
$("enable-tone-dialog").addEventListener("close", () => {
  $("random-tone-adaptive").checked = draft.behavior.random_tone_adaptive;
});
document.querySelectorAll("[data-edit-prompt]").forEach((button) =>
  button.addEventListener("click", () => {
    editingPrompt = button.dataset.editPrompt;
    $("speech-prompt-title").textContent = editingPrompt === "speech_prompt" ? "编辑聊天语音提示词" : "编辑概率语音自适应提示词";
    $("prompt-editor").value = draft.behavior[editingPrompt];
    $("prompt-editor-error").hidden = true;
    $("prompt-variable-list").replaceChildren();
    for (const [name, variable] of Object.entries(state.prompt_variables)) {
      if (!variable.scope.includes(editingPrompt)) continue;
      const row = document.createElement("div");
      row.className = "prompt-variable";
      const insert = document.createElement("button");
      insert.type = "button";
      insert.className = "text-button";
      insert.textContent = `{${name}}`;
      insert.dataset.insertVariable = name;
      insert.title = `插入：${variable.label}`;
      insert.addEventListener("click", () => {
        const editor = $("prompt-editor");
        const token = `{${name}}`;
        if (editor.value.length - (editor.selectionEnd - editor.selectionStart) + token.length > editor.maxLength) {
          $("prompt-editor-error").textContent = "提示词最多 6000 字符，请先缩短内容。";
          $("prompt-editor-error").hidden = false;
          return;
        }
        editor.setRangeText(token, editor.selectionStart, editor.selectionEnd, "end");
        editor.focus();
        $("prompt-editor-error").hidden = true;
      });
      const description = document.createElement("small");
      description.textContent = `${variable.label}：${variable.description}`;
      row.append(insert, description);
      $("prompt-variable-list").append(row);
    }
    $("speech-prompt-dialog").showModal();
    $("prompt-editor").focus();
    $("prompt-editor").setSelectionRange(0, 0);
  }),
);
$("restore-prompt").addEventListener("click", () => {
  $("prompt-editor").value = state.prompt_defaults[editingPrompt];
  $("prompt-editor-error").hidden = true;
  $("prompt-editor").focus();
});
$("cancel-prompt").addEventListener("click", () => $("speech-prompt-dialog").close());
$("apply-prompt").addEventListener("click", () => {
  const value = $("prompt-editor").value;
  const unsupported = [...value.matchAll(/\{([A-Za-z_][A-Za-z_0-9]*)\}/g)]
    .map((match) => match[1]).filter((name) => !state.prompt_variables[name]?.scope.includes(editingPrompt));
  if (value.length > 6000 || unsupported.length) {
    $("prompt-editor-error").textContent = unsupported.length
      ? `此提示词不支持的变量：${[...new Set(unsupported)].map((name) => `{${name}}`).join("、")}`
      : "提示词最多 6000 字符。";
    $("prompt-editor-error").hidden = false;
    return;
  }
  if (draft.behavior[editingPrompt] !== value) {
    draft.behavior[editingPrompt] = value;
    markDirty();
  }
  $("speech-prompt-dialog").close();
});
$("speech-prompt-dialog").addEventListener("close", () => { editingPrompt = undefined; });

document.querySelectorAll("[data-probability-scope]").forEach((element) =>
  element.addEventListener("change", () => {
    if (!draft) return;
    const scope = element.dataset.probabilityScope;
    const value = element.value === "inherit" ? -1 : draft.behavior.trigger_probability;
    draft.behavior[`${scope}_trigger_probability`] = value;
    $(`${scope}-trigger-probability`).value = value;
    markDirty();
  }),
);
$("edit-expression").addEventListener("click", () => {
  if (!expressionProfile) return;
  editingExpression = expressionProfile;
  const reference = editingExpression.tag_policy === "reference";
  $("expression-title").textContent = `编辑 ${editingExpression.name} 控制表`;
  $("expression-description").textContent = reference
    ? "Breeze 使用自由语气控制，表格只是推荐参考，并非穷举白名单。LLM 可以自行编写表外表现，用 [语气:自由描述] 或 instruction 传递，无需先登记；标签和指令均可不添加。"
    : `此平台${editingExpression.mode === "fixed" ? "使用固定情绪允许值" : "支持自由语气指令"}；原生事件按实际模型支持的标签拼写传递。${editingExpression.note}`;
  $("custom-tags-label").textContent = reference ? "原生标签推荐参考（可选）" : "原生正文标签";
  $("custom-tags-hint").textContent = reference
    ? "每行填写一个完整原生标签。非空内容仅调整给 LLM 的推荐参考，不限制表外自由语气；已知原生事件仍可识别并隐藏。自由描述无需加入表中，请写成 [语气:描述]。"
    : "每行一个当前模型实际支持的完整标签，例如 [laughs]。非空表替换此平台预设，聊天文字会隐藏这些标签；自由描述使用 [语气:描述]。";
  $("custom-tags").value = draft.expression[`${editingExpression.key}_tags`];
  $("custom-tags").placeholder = (editingExpression.prompt_tags || editingExpression.tags).join("\n") || "当前模型没有预设原生正文标签，可按模型文档填写。";
  $("custom-styles").value = draft.expression[`${editingExpression.key}_styles`];
  $("custom-styles").placeholder = editingExpression.styles.join("\n");
  $("custom-styles-field").hidden = editingExpression.mode !== "fixed";
  $("custom-styles").disabled = editingExpression.mode !== "fixed";
  $("expression-error").hidden = true;
  $("expression-dialog").showModal();
  $("custom-tags").focus();
});
$("reset-expression").addEventListener("click", () => {
  $("custom-tags").value = "";
  $("custom-styles").value = "";
  $("expression-error").hidden = true;
});
$("cancel-expression").addEventListener("click", () => $("expression-dialog").close());
$("apply-expression").addEventListener("click", () => {
  if (!editingExpression) return;
  const fields = editingExpression.mode === "fixed" ? ["tags", "styles"] : ["tags"];
  for (const field of fields) {
    const value = $(`custom-${field}`).value;
    const lines = value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    let error = "";
    if (value.length > 12000 || lines.length > 100 || /[\x00-\x09\x0b\x0c\x0e-\x1f]/.test(value)) error = "控制表最多 100 行、12000 字符，不能包含控制字符。";
    else if (field === "tags" && lines.some((line) => !/^(?:\[[^\[\]<>`]{1,100}\]|\([^()<>`]{1,100}\))$/.test(line) || /^\[(?:语气|style|話し方)\s*[:：]/i.test(line))) error = "每行填写一个完整原生标签，例如 [laughs]；自由语气通过 [语气:描述] 使用，无需填入标签表。";
    else if (field === "styles" && lines.some((line) => line.length > 100 || /[\[\]<>()`]/.test(line))) error = "每行填写一个平台实际支持的情绪值，不能包含标记。";
    if (error) {
      $("expression-error").textContent = error;
      $("expression-error").hidden = false;
      return;
    }
  }
  let changed = false;
  for (const field of fields) {
    const key = `${editingExpression.key}_${field}`;
    const value = $(`custom-${field}`).value;
    if (draft.expression[key] !== value) {
      draft.expression[key] = value;
      changed = true;
    }
  }
  if (changed) markDirty();
  $("expression-dialog").close();
});
$("expression-dialog").addEventListener("close", () => { editingExpression = undefined; });
for (const backend of ["local", "cloud"]) {
  $(`clear-${backend}-key`).addEventListener("click", () => {
    if (clearKeys.has(backend)) {
      clearKeys.delete(backend);
      $(`clear-${backend}-key`).textContent = "清空";
      $(`${backend}-key-state`).textContent = state.credentials[backend]
        ? "已配置密钥 · 输入新值可替换"
        : "当前未配置密钥";
    } else {
      clearKeys.add(backend);
      draft[backend].api_key = "";
      $(`${backend}-key`).value = "";
      $(`clear-${backend}-key`).textContent = "撤销清空";
      $(`${backend}-key-state`).textContent = "密钥已清空 · 保存后生效";
    }
    markDirty();
  });
  $(`${backend}-key`).addEventListener("input", () => {
    clearKeys.delete(backend);
    $(`clear-${backend}-key`).textContent = "清空";
    $(`clear-${backend}-key`).disabled =
      !$(`${backend}-key`).value && !state.credentials[backend];
    $(`${backend}-key-state`).textContent = $(`${backend}-key`).value
      ? "新密钥待保存"
      : "留空将保留已保存的密钥";
  });
}
$("synthesis-text").addEventListener("input", updateSummary);
$("save").addEventListener("click", () =>
  action($("save"), async () => {
    await saveSettings();
    notice("配置已保存。");
  }),
);
$("refresh-audio").addEventListener("click", () =>
  action($("refresh-audio"), refreshAudio),
);
$("download-current").addEventListener("click", () =>
  action($("download-current"), () => download(currentAudio)),
);
$("player").addEventListener("error", () => {
  if (!$("player").getAttribute("src")) return;
  $("player-state").textContent = "播放失败";
  $("player-detail").textContent =
    "可下载音频，或将输出格式改为 WAV 后重新生成";
  $("retry-playback").hidden = false;
  notice("浏览器无法播放此音频，可下载文件或选择 WAV 格式", true);
});
$("player").addEventListener("loadedmetadata", () => {
  if (Number.isFinite($("player").duration))
    $("player-state").textContent = `${$("player").duration.toFixed(1)} 秒`;
});
$("synthesize").addEventListener("click", () =>
  action($("synthesize"), async () => {
    if (!draft) throw new Error("配置尚未加载，请刷新工作台。");
    const text = $("synthesis-text").value;
    if (!text.trim() || Array.from(text).length > draft.behavior.max_characters)
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
    if (draft.backend === "cloud") {
      $("clone-reference").value = item.id;
      notice("克隆录音已上传，填写名称和语言即可生成预览");
    } else {
      editingReference = item;
      $("reference-name").value = item.name;
      $("reference-text").value = "";
      $("reference-dialog").showModal();
      notice("录音已上传，请填写准确文字稿");
    }
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
      playbackSequence += 1;
      $("download-current").disabled = true;
      $("player-name").textContent = "生成语音或从列表选择音频";
      $("player-state").textContent = "等待选择音频";
      $("player-detail").textContent = "音频准备好后即可播放和下载";
      $("player").hidden = true;
      $("retry-playback").hidden = true;
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
      $("connection-result").textContent = result.backend === "provider" ? "提供商配置可用（尚未合成）" : "连接成功";
      if (result.models)
        $("models").replaceChildren(
          ...result.models.map(
            (item) => new Option(item.model_id, item.model_id),
          ),
        );
      notice(result.backend === "provider" ? "提供商及控制协议已就绪，请合成试听验证上游连接。" : "连接成功，配置已保存。");
    } catch (error) {
      $("connection-result").textContent = "连接失败";
      throw error;
    }
  }),
);
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
      : preview.audio_unavailable
        ? `“${preview.name}”预览已创建，可保存到官方账号；试听音频可在官方控制台查看`
        : `“${preview.name}”预览已生成，请先试听，再保存到官方账号`;
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
    if (!preview.saved_voice_id) {
      const saved = await bridge.apiPost("voices/save-preview", {
        preview_id: preview.preview_id,
        name: preview.name,
        language_code: preview.language_code,
      });
      preview.saved_voice_id = saved.voice_id;
      $("preview-message").textContent = `音色已保存 · ${saved.voice_id}`;
      $("save-preview").textContent = "设为当前音色";
    }
    draft.cloud.voice_id = preview.saved_voice_id;
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
