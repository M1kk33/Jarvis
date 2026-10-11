"use strict";

const notice = document.getElementById("notice");
const setupForm = document.getElementById("setup-form");

function say(message) {
  notice.textContent = message;
}

async function responseJson(response) {
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.detail || `Request failed (${response.status})`);
  return value;
}

// Also clear a fragment inherited by a redirect into an already signed-in tab.
const launchToken = new URLSearchParams(window.location.hash.slice(1)).get("token");
if (window.location.hash) {
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
}

if (setupForm) {
  const tokenInput = document.getElementById("setup-token");
  async function exchangeToken(event) {
    if (event) event.preventDefault();
    say("");
    const button = setupForm.querySelector("button");
    button.disabled = true;
    try {
      await responseJson(await fetch("/api/setup", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: tokenInput.value }),
      }));
      tokenInput.value = "";
      window.location.replace("/");
    } catch (error) {
      say(error.message);
      button.disabled = false;
    }
  }
  setupForm.addEventListener("submit", exchangeToken);
  if (launchToken) {
    tokenInput.value = launchToken;
    exchangeToken();
  }
} else {
  const state = { csrf: null, selected: null, fresh: false, source: null, pending: null, spokenEnabled: false };
  const jobUpdates = new Map();
  const turnStatusNodes = new Map();
  const draftNodes = new Map();
  const liveDrafts = new Map();
  const conversationsNode = document.getElementById("conversations");
  let conversationRefreshGeneration = 0;
  const messagesNode = document.getElementById("messages");
  let messageRefreshGeneration = 0;
  const jobsNode = document.getElementById("jobs");
  const approvalsNode = document.getElementById("approvals");
  const titleNode = document.getElementById("conversation-title");
  const inputNode = document.getElementById("message-input");
  const speechControls = document.getElementById("speech-controls");
  const recordButton = document.getElementById("record-button");
  const stopRecording = document.getElementById("stop-recording");
  const cancelTranscription = document.getElementById("cancel-transcription");
  const speechStatus = document.getElementById("speech-status");
  let recorder = null;
  let microphone = null;
  let recordTimer = null;
  let discardTranscript = false;
  const stopVoiceButton = document.getElementById("stop-voice");
  const voiceStatus = document.getElementById("voice-status");
  const visionControls = document.getElementById("vision-controls");
  const screenshotInput = document.getElementById("screenshot-input");
  const screenshotPreview = document.getElementById("screenshot-preview");
  let previewUrl = null;
  let screenshotFile = null;
  let attachmentGeneration = 0;
  let attachmentPending = false;
  const screenshotStatus = document.getElementById("screenshot-status");
  const removeScreenshot = document.getElementById("remove-screenshot");
  const attachmentPreview = document.getElementById("attachment-preview");
  const sendForm = document.getElementById("send-form");
  const activityPanel = document.getElementById("activity-panel");
  const attentionNotice = document.getElementById("attention-notice");
  let attentionIds = new Set();

  function resizeInput() {
    const scrollTop = inputNode.scrollTop;
    inputNode.style.height = "auto";
    const style = window.getComputedStyle(inputNode);
    const height = Math.max(parseFloat(style.minHeight), Math.min(inputNode.scrollHeight, parseFloat(style.maxHeight)));
    inputNode.style.height = `${height}px`;
    inputNode.style.overflowY = inputNode.scrollHeight > height ? "auto" : "hidden";
    inputNode.scrollTop = scrollTop;
  }

  function setInput(value) {
    inputNode.value = value;
    resizeInput();
  }

  inputNode.addEventListener("input", resizeInput);
  // Width also changes when the activity panel or voice controls open.
  // Ignore height-only notifications to avoid a resize feedback loop.
  let inputWidth = 0;
  const inputObserver = new ResizeObserver(([entry]) => {
    if (entry.contentRect.width === inputWidth) return;
    inputWidth = entry.contentRect.width;
    resizeInput();
  });
  inputObserver.observe(inputNode);
  resizeInput();

  function showActivity(visible) {
    activityPanel.hidden = !visible;
    attentionNotice.hidden = visible || !attentionIds.size;
    attentionNotice.setAttribute("aria-expanded", String(visible));
  }
  attentionNotice.addEventListener("click", () => showActivity(true));
  document.getElementById("close-activity").addEventListener("click", () => {
    showActivity(false);
    inputNode.focus();
  });
  document.addEventListener("keydown", (event) => {
    if (document.getElementById("settings-dialog").open) return;
    if (event.key === "Escape" && !activityPanel.hidden) {
      showActivity(false);
      inputNode.focus();
    }
  });
  inputNode.addEventListener("keydown", (event) => {
    const sendKey = settings.preferences.send_shortcut === "ctrl_enter" ? (event.ctrlKey || event.metaKey) : !(event.ctrlKey || event.metaKey);
    if (event.key === "Enter" && sendKey && !event.shiftKey && !event.altKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      if (!document.getElementById("send-button").disabled) document.getElementById("send-form").requestSubmit();
    }
  });

  function welcome() {
    const panel = document.createElement("section");
    panel.className = "welcome";
    line(panel, "J", "brand-mark");
    const heading = document.createElement("h2");
    heading.textContent = "Чем могу помочь?";
    panel.append(heading);
    const subtitle = document.createElement("p");
    subtitle.textContent = "Разберём идею, найдём решение или просто поговорим. Начните с того, что важно вам.";
    panel.append(subtitle);
    const suggestions = document.createElement("div");
    suggestions.className = "suggestions";
    for (const [label, text] of [["Разобраться в теме", "Помоги разобраться в теме: "], ["Составить план", "Помоги составить план для "], ["Обсудить идею", "Давай обсудим идею: "]]) {
      suggestions.append(button(label, () => { setInput(text); inputNode.focus(); }));
    }
    panel.append(suggestions);
    messagesNode.append(panel);
  }

  function clearScreenshot() {
    attachmentGeneration += 1;
    attachmentPending = false;
    screenshotFile = null;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
    screenshotPreview.hidden = true;
    screenshotPreview.removeAttribute("src");
    screenshotInput.value = "";
    screenshotStatus.textContent = "";
    removeScreenshot.hidden = true;
    attachmentPreview.hidden = true;
  }

  async function attachScreenshot(file) {
    if (visionControls.hidden) { say("Распознавание скриншотов не подключено."); return; }
    if (!file || !["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      say("Прикрепите изображение PNG, JPEG или WebP."); return;
    }
    if (file.size > 20 * 1024 * 1024) { say("Исходное изображение должно быть не больше 20 МиБ."); return; }
    const generation = ++attachmentGeneration;
    attachmentPending = true;
    attachmentPreview.hidden = false;
    screenshotStatus.textContent = "Подготавливаю скриншот…";
    let bitmap;
    try {
      bitmap = await createImageBitmap(file);
      const scale = Math.min(1, 1024 / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      let png;
      for (;;) {
        const context = canvas.getContext("2d");
        if (!context) throw new Error("Не удалось подготовить скриншот.");
        context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        png = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
        if (!png) throw new Error("Не удалось прочитать изображение.");
        if (png.size <= 2 * 1024 * 1024) break;
        if (Math.max(canvas.width, canvas.height) <= 256) throw new Error("Не удалось уменьшить скриншот до 2 МиБ.");
        canvas.width = Math.max(1, Math.floor(canvas.width * 0.8));
        canvas.height = Math.max(1, Math.floor(canvas.height * 0.8));
      }
      if (generation !== attachmentGeneration) return;
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      screenshotFile = new File([png], "screenshot.png", { type: "image/png" });
      previewUrl = URL.createObjectURL(screenshotFile);
      screenshotPreview.src = previewUrl;
      screenshotPreview.hidden = false;
      removeScreenshot.hidden = false;
      const resized = canvas.width !== bitmap.width || canvas.height !== bitmap.height;
      screenshotStatus.textContent = `${resized ? "Уменьшен до" : "Скриншот"} ${canvas.width} × ${canvas.height} · готов к отправке`;
      say("");
    } catch (error) {
      if (generation === attachmentGeneration) {
        screenshotStatus.textContent = screenshotFile ? "Предыдущий скриншот сохранён." : "";
        attachmentPreview.hidden = !screenshotFile;
        say(error.message || "Не удалось прочитать изображение.");
      }
    } finally {
      bitmap?.close();
      if (generation === attachmentGeneration) attachmentPending = false;
    }
  }

  removeScreenshot.addEventListener("click", clearScreenshot);
  document.getElementById("attach-image").addEventListener("click", () => screenshotInput.click());
  screenshotInput.addEventListener("change", () => {
    const file = screenshotInput.files[0];
    screenshotInput.value = "";
    if (file) attachScreenshot(file);
  });
  sendForm.addEventListener("paste", (event) => {
    const item = [...(event.clipboardData?.items || [])].find((item) => item.kind === "file" && item.type.startsWith("image/"));
    if (!item) return; // Ordinary text still uses the browser's native paste.
    event.preventDefault();
    attachScreenshot(item.getAsFile());
  });
  document.addEventListener("dragover", (event) => {
    if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
    event.preventDefault(); // Never navigate away from the local app to a file.
    sendForm.classList.add("drag-over");
  });
  document.addEventListener("dragleave", (event) => {
    if (!event.relatedTarget) sendForm.classList.remove("drag-over");
  });
  document.addEventListener("drop", (event) => {
    sendForm.classList.remove("drag-over");
    const files = [...(event.dataTransfer?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    if (files.length !== 1) { say("Прикрепите один скриншот за раз."); return; }
    attachScreenshot(files[0]);
  });

  function imageBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error("Could not read the screenshot."));
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
      reader.readAsDataURL(file);
    });
  }

  async function api(path, method = "GET", payload = undefined) {
    const headers = {};
    if (method !== "GET") headers["X-CSRF-Token"] = state.csrf;
    if (payload !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method, credentials: "same-origin", headers,
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    if (response.status === 401) {
      window.location.assign("/setup");
      throw new Error("Your local session ended.");
    }
    return responseJson(response);
  }

  function button(label, action, quiet = false) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    if (quiet) element.className = "quiet";
    element.addEventListener("click", action);
    return element;
  }

  function line(parent, text, className = "") {
    const element = document.createElement("div");
    element.textContent = text;
    if (className) element.className = className;
    parent.append(element);
    return element;
  }

  function statusLabel(value) {
    return ({ queued: "В очереди", running: "Выполняется", waiting_approval: "Ожидает подтверждения",
      succeeded: "Готово", complete: "Завершено", cancelled: "Отменено", interrupted: "Прервано",
      failed: "Ошибка", pending: "Ожидание", streaming: "Формирую ответ", loading: "Загружаю модель",
      image_loading: "Подготавливаю модель изображений", image_generating: "Генерирую изображение",
      image_finalizing: "Сохраняю изображение",
      cancel_requested: "Отменяется", created: "Создано", started: "Начато" })[value] || value.replaceAll("_", " ");
  }

  function jobLabel(job) {
    if (job.error_code === "output_limit") return "Достигнут лимит длины ответа";
    return statusLabel(job.status === "running" ? (job.progress || job.status) : job.status);
  }

  function updateJobProgress(job) {
    if (job.conversation_id !== state.selected) return;
    const prior = jobUpdates.get(job.turn_id);
    if (prior?.updated_at && job.updated_at && prior.updated_at > job.updated_at) return;
    jobUpdates.set(job.turn_id, job);
    if (jobUpdates.size > 64) jobUpdates.delete(jobUpdates.keys().next().value);
    const node = turnStatusNodes.get(job.turn_id);
    if (node) node.textContent = `Состояние: ${jobLabel(job)}`;
  }

  const voicePlayer = new window.JarvisVoicePlayer({
    api,
    fetchChunk: async (sessionId, index, signal) => {
      const response = await fetch(`/api/spoken/${encodeURIComponent(sessionId)}/chunks/${index}`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
        body: "{}", signal,
      });
      if (!response.ok) await responseJson(response);
      return response.arrayBuffer();
    },
    AudioContextType: window.AudioContext || window.webkitAudioContext,
    onStatus: (message) => {
      voiceStatus.textContent = ({ "Playback stopped.": "", "Audio playback is unavailable.": "Воспроизведение недоступно.",
        "Preparing voice…": "Готовлю озвучивание…", "Playback finished.": "Озвучивание завершено.",
        "Voice failed. The text is still available.": "Озвучивание не удалось. Ответ доступен в тексте.",
        "Only a completed assistant reply can be spoken": "Можно озвучить только завершённый ответ." })[message]
        ?? message.replace(/Preparing voice (\d+) of (\d+)…/, "Готовлю озвучивание $1 из $2…").replace(/Speaking (\d+) of (\d+)…/, "Озвучивание $1 из $2…");
    },
    onActive: (active) => { stopVoiceButton.hidden = !active; },
  });

  const settings = new window.JarvisSettings({ api, onApply: (preferences) => {
    voicePlayer.setVolume(preferences.voice_volume / 100);
    document.getElementById("send-button").title = preferences.send_shortcut === "enter" ? "Отправить (Enter)" : "Отправить (Ctrl + Enter)";
    resizeInput();
  } });

  async function refreshConversation() {
    const generation = ++messageRefreshGeneration;
    if (!state.selected) {
      voicePlayer.stop();
      titleNode.textContent = "Новый диалог";
      messagesNode.replaceChildren();
      turnStatusNodes.clear();
      draftNodes.clear();
      welcome();
      return;
    }
    const selected = state.selected;
    const data = await api(`/api/conversations/${encodeURIComponent(selected)}`);
    if (selected !== state.selected || generation !== messageRefreshGeneration) return;
    if (voicePlayer.active && !data.messages.some((message) =>
      message.id === voicePlayer.active.messageId && message.role === "assistant"
      && data.turns.some((turn) => turn.id === voicePlayer.active.turnId && turn.status === "complete"))) {
      voicePlayer.stop();
    }
    titleNode.textContent = data.conversation.title;
    messagesNode.replaceChildren();
    turnStatusNodes.clear();
    draftNodes.clear();
    if (!data.messages.length) welcome();
    const turns = new Map(data.turns.map((turn) => [turn.id, turn]));
    for (const turn of data.turns) {
      if (turn.status === "complete") liveDrafts.delete(turn.id);
    }
    const drafts = new Map(data.drafts.map((draft) => [draft.turn_id, draft]));
    const artifacts = new Map();
    for (const artifact of data.artifacts || []) {
      if (!artifacts.has(artifact.turn_id)) artifacts.set(artifact.turn_id, []);
      artifacts.get(artifact.turn_id).push(artifact);
    }
    for (const message of data.messages) {
      const card = document.createElement("article");
      card.className = `message ${message.role}`;
      line(card, message.role === "user" ? "Вы" : "Jarvis", "role");
      const content = document.createElement("pre");
      content.textContent = message.text;
      card.append(content);
      if (state.spokenEnabled && message.role === "assistant"
          && turns.get(message.turn_id)?.status === "complete") {
        card.append(button("Озвучить", () => {
          if (voicePlayer.isPlaying(state.selected, message.turn_id, message.id)) voicePlayer.stop();
          else voicePlayer.play(state.selected, message.turn_id, message.id);
        }, true));
      }
      if (message.role === "user") {
        for (const item of (data.invocations || []).filter((item) => item.turn_id === message.turn_id)) {
          let toolResult;
          try { toolResult = JSON.parse(item.result_json || "null"); } catch (_) { /* Keep raw diagnostic details below. */ }
          const toolFailed = toolResult?.status === "error";
          if (toolFailed && toolResult.code === "out_of_memory") {
            const memory = toolResult.memory;
            let explanation = "Генерация не запущена: недостаточно свободной памяти с учётом защитного резерва. Закройте тяжёлые приложения и отправьте новый запрос.";
            if (memory && ["ram", "vram"].includes(memory.resource)) {
              const kind = memory.resource === "ram" ? "ОЗУ" : "видеопамяти";
              explanation = Number.isFinite(memory.available_gib) && Number.isFinite(memory.required_gib)
                ? `Для генерации нужно ${memory.required_gib.toFixed(1)} ГиБ свободной ${kind} с учётом резерва. На момент запроса было ${memory.available_gib.toFixed(1)} ГиБ. Освободите память и отправьте новый запрос.`
                : `Не удалось проверить свободный объём ${kind}. Генерация не запущена.`;
            }
            line(card, explanation, "notice");
          }
          const details = document.createElement("details");
          const label = document.createElement("summary");
          label.textContent = `${item.name}: ${toolFailed ? "не выполнено" : statusLabel(item.status)}${item.acknowledged_at ? " (неизвестный результат подтверждён)" : ""} · подробности`;
          details.append(label);
          const receipt = document.createElement("pre");
          receipt.textContent = `Назначение: ${item.target_identity}\nArguments: ${item.arguments_json}\nReceipt: ${item.receipt_json || "none"}\nResult: ${item.result_json || "none"}`;
          details.append(receipt);
          card.append(details);
        }
        const turn = turns.get(message.turn_id);
        if (turn && turn.status !== "complete") {
          const job = jobUpdates.get(turn.id);
          const details = button("", () => showActivity(true), true);
          details.className = "turn-status";
          details.title = "Показать действия по запросу";
          details.setAttribute("aria-controls", "activity-panel");
          const node = document.createElement("span");
          node.className = "subtle";
          node.textContent = `Состояние: ${job ? jobLabel(job) : turn.error_code === "output_limit" ? "Достигнут лимит длины ответа" : statusLabel(turn.status)}`;
          node.setAttribute("role", "status");
          details.append(node);
          card.append(details);
          turnStatusNodes.set(turn.id, node);
        }
        for (const artifact of artifacts.get(message.turn_id) || []) {
          if (!/^[0-9a-f-]{36}$/.test(artifact.id)
              || !["image/png", "screenshot/png"].includes(artifact.kind)) continue;
          if (artifact.status !== "available") {
            line(card, `Image ${artifact.id} is unavailable`, "subtle");
            continue;
          }
          const preview = document.createElement("img");
          preview.className = "generated-image";
          preview.alt = artifact.kind === "screenshot/png" ? "Исходный скриншот" : `Generated image ${artifact.id}`;
          preview.loading = "lazy";
          preview.src = `/api/artifacts/${artifact.id}/preview`;
          card.append(preview);
          const download = document.createElement("a");
          download.href = `/api/artifacts/${artifact.id}`;
          download.textContent = artifact.kind === "screenshot/png" ? "Скачать исходный скриншот" : `Скачать изображение ${artifact.id}`;
          card.append(download);
        }
      }
      messagesNode.append(card);
      const turn = turns.get(message.turn_id);
      if (message.role === "user" && turn && turn.status !== "complete") {
        // Keep the partial reply in the same message flow and geometry as the final reply.
        const partial = document.createElement("article");
        partial.className = "message assistant";
        line(partial, "Jarvis", "role");
        const content = document.createElement("pre");
        content.textContent = liveDrafts.get(turn.id) ?? drafts.get(turn.id)?.text ?? "";
        partial.append(content);
        if (turn.error_code === "output_limit") {
          line(partial, "Ответ остановлен на лимите длины. Попросите более краткий ответ или разбейте вопрос на части.", "subtle");
        }
        partial.hidden = !content.textContent;
        draftNodes.set(turn.id, { card: partial, content });
        messagesNode.append(partial);
      }
    }
    messagesNode.scrollTop = messagesNode.scrollHeight;
  }

  async function refreshConversations(focusPin = null, focusId = null) {
    const generation = ++conversationRefreshGeneration;
    const data = await api("/api/conversations");
    if (generation !== conversationRefreshGeneration) return;
    const restorePinFocus = focusPin && document.activeElement === focusPin;
    conversationsNode.replaceChildren();
    if (!state.selected && !state.fresh && data.conversations.length) state.selected = data.conversations[0].id;
    for (const conversation of data.conversations) {
      const row = document.createElement("div");
      row.className = "conversation-row" + (conversation.pinned ? " pinned" : "");
      const item = button(conversation.title, async () => {
        voicePlayer.stop();
        clearScreenshot();
        if (state.source) state.source.close();
        state.source = null;
        liveDrafts.clear();
        draftNodes.clear();
        state.selected = conversation.id;
        state.fresh = false;
        state.pending = null;
        await refreshAll();
      });
      item.className = "conversation-title";
      if (conversation.id === state.selected) {
        row.className += " selected";
        item.setAttribute("aria-current", "true");
      }
      item.title = conversation.title;
      const pin = button("", async () => {
        if (pin.disabled) return;
        pin.disabled = true;
        say("");
        try {
          await api(`/api/conversations/${encodeURIComponent(conversation.id)}/pin`, "POST", { pinned: !conversation.pinned });
          const restoredFocus = await refreshConversations(pin, conversation.id);
          if (!restoredFocus) conversationsNode.scrollTop = 0;
        } catch (error) { say(error.message); }
        finally { pin.disabled = false; }
      });
      pin.className = "pin-chat";
      pin.title = conversation.pinned ? "Открепить диалог" : "Закрепить диалог";
      pin.setAttribute("aria-label", `${pin.title}: ${conversation.title}`);
      pin.setAttribute("aria-pressed", String(Boolean(conversation.pinned)));
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("aria-hidden", "true");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", "M16 9V4l1-1H7l1 1v5l-3 3v2h6v7l2-2v-5h6v-2l-3-3Z");
      icon.append(path);
      pin.append(icon);
      row.append(item, pin);
      conversationsNode.append(row);
      if (restorePinFocus && conversation.id === focusId) pin.focus();
    }
    return Boolean(restorePinFocus);
  }

  function listen(jobId) {
    if (state.source) state.source.close();
    const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    state.source = source;
    const selected = state.selected;
    source.addEventListener("progress", async (event) => {
      if (state.source !== source || state.selected !== selected) return;
      const snapshot = JSON.parse(event.data);
      updateJobProgress(snapshot.job);
      if (typeof snapshot.draft === "string") {
        liveDrafts.set(snapshot.job.turn_id, snapshot.draft);
        const draft = draftNodes.get(snapshot.job.turn_id);
        if (draft) {
          const follow = messagesNode.scrollHeight - messagesNode.clientHeight - messagesNode.scrollTop < 80;
          draft.content.textContent = snapshot.draft;
          draft.card.hidden = !snapshot.draft;
          if (follow) messagesNode.scrollTop = messagesNode.scrollHeight;
        }
      }
      if (["succeeded", "failed", "cancelled"].includes(snapshot.job.status)) {
        source.close();
        if (state.source === source) state.source = null;
        await refreshConversation();
      }
      await refreshJobs();
      await refreshApprovals();
    });
  }

  async function refreshJobs() {
    const selected = state.selected;
    const data = await api("/api/jobs");
    if (state.selected !== selected) return;
    jobsNode.replaceChildren();
    const visible = data.jobs.filter((job) => job.conversation_id === state.selected).slice(-10).reverse();
    if (!visible.length) line(jobsNode, "Здесь появится ход выполнения ваших запросов.", "empty");
    for (const job of visible) {
      updateJobProgress(job);
      const card = document.createElement("div");
      card.className = "card";
      line(card, jobLabel(job));
      line(card, statusLabel(job.progress), "subtle");
      if (!["succeeded", "failed", "cancelled"].includes(job.status)) {
        card.append(button("Отменить", async () => {
          try { await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, "POST"); await refreshJobs(); }
          catch (error) { say(error.message); }
        }, true));
      }
      if (["queued", "interrupted", "waiting_approval"].includes(job.status)) {
        card.append(button("Продолжить", async () => {
          try { await api(`/api/jobs/${encodeURIComponent(job.id)}/resume`, "POST"); listen(job.id); await refreshJobs(); }
          catch (error) { say(error.message); }
        }));
      }
      jobsNode.append(card);
    }
    const active = visible.find((job) => job.status === "running");
    if (active && !state.source) listen(active.id);
  }

  async function refreshApprovals() {
    const data = await api("/api/approvals");
    approvalsNode.replaceChildren();
    const visible = data.approvals;
    const nextAttention = new Set([
      ...visible.map((item) => item.invocation_id),
      ...(data.uncertain_actions || []).map((item) => item.id),
      ...(data.uncertain_files || []).map((item) => item.invocation_id),
    ]);
    const newAttention = [...nextAttention].some((id) => !attentionIds.has(id));
    attentionIds = nextAttention;
    attentionNotice.textContent = `Требуют внимания: ${attentionIds.size}`;
    showActivity(newAttention || !activityPanel.hidden);
    if (!visible.length && !(data.uncertain_actions || []).length && !(data.uncertain_files || []).length) line(approvalsNode, "Всё спокойно. Подтверждения не требуются.", "empty");
    for (const item of visible) {
      const card = document.createElement("div");
      card.className = "card";
      line(card, `${item.name} · version ${item.version}`);
      line(card, `Назначение: ${item.target}`, "subtle");
      line(card, `Job: ${item.job_id} · policy: ${item.policy_version}`, "subtle");
      if (item.name === "create_text_file" && item.preview) {
        line(card, `Создать файл: ${item.preview.path}`);
        line(card, "Точное содержимое файла (UTF-8):");
        const content = document.createElement("pre");
        content.textContent = item.preview.content;
        card.append(content);
      } else if (item.name === "fetch_public_page" && item.preview) {
        line(card, "Отправить один GET-запрос по указанному адресу:");
        const url = document.createElement("pre");
        url.textContent = item.preview.url;
        card.append(url);
        line(card, "Сайт получит весь адрес и параметры запроса. Проверьте, нет ли в них личных данных. Перенаправление потребует нового запроса.", "subtle");
      } else if (item.name === "render_public_page" && item.preview) {
        line(card, "Загрузить указанный адрес и отобразить HTML во временном браузере без доступа к сети:");
        const url = document.createElement("pre");
        url.textContent = item.preview.url;
        card.append(url);
        line(card, "Сайт получит весь адрес и параметры запроса. Проверьте, нет ли личных данных. Встроенный JavaScript выполняется; внешние ресурсы, запросы страницы и перенаправления заблокированы.", "subtle");
      } else {
        const argumentsText = document.createElement("pre");
        argumentsText.textContent = item.arguments;
        card.append(argumentsText);
      }
      if (item.actionable) {
        card.append(button("Разрешить", async () => {
          try { await api(`/api/approvals/${encodeURIComponent(item.invocation_id)}/approve`, "POST"); listen(item.job_id); await refreshApprovals(); }
          catch (error) { say(error.message); }
        }));
      } else {
        line(card, "Подтверждение устарело. Отмените задачу и создайте новый запрос.", "subtle");
      }
      card.append(button("Отклонить", async () => {
        try { await api(`/api/approvals/${encodeURIComponent(item.invocation_id)}/reject`, "POST"); await refreshAll(); }
        catch (error) { say(error.message); }
      }, true));
      approvalsNode.append(card);
    }
    for (const item of data.uncertain_files || []) {
      const card = document.createElement("div");
      card.className = "card";
      line(card, `Создание файла не подтверждено: ${item.path}`);
      line(card, "Проверка сверяет указанный файл и его содержимое. Она не создаёт файл.", "subtle");
      card.append(button("Проверить файл", async () => {
        try { await api(`/api/file-reconciliation/${encodeURIComponent(item.invocation_id)}`, "POST"); await refreshAll(); }
        catch (error) { say(error.message); }
      }));
      approvalsNode.append(card);
    }
    for (const item of data.uncertain_actions || []) {
      const card = document.createElement("div");
      card.className = "card";
      line(card, `Неизвестный результат: ${item.name}`);
      line(card, `Назначение: ${item.target_identity}`, "subtle");
      const proposal = document.createElement("pre");
      proposal.textContent = item.arguments_json;
      card.append(proposal);
      line(card, "Сначала отмените задачу. Подтверждение позволит отправлять новые сообщения. Результат останется неизвестным; действие не будет повторено.", "subtle");
      card.append(button("Подтвердить неизвестный результат", async () => {
        try { await api(`/api/uncertain-actions/${encodeURIComponent(item.id)}/acknowledge`, "POST"); await refreshAll(); }
        catch (error) { say(error.message); }
      }));
      approvalsNode.append(card);
    }
  }

  async function refreshAll() {
    say("");
    await refreshConversations();
    await Promise.all([refreshConversation(), refreshJobs(), refreshApprovals()]);
  }

  document.getElementById("new-conversation").addEventListener("click", async () => {
    try {
      voicePlayer.stop();
      if (state.source) state.source.close();
      state.source = null;
      state.pending = null;
      liveDrafts.clear();
      draftNodes.clear();
      clearScreenshot();
      state.selected = null;
      state.fresh = true;
      setInput("");
      conversationsNode.scrollTop = 0;
      await refreshAll();
      inputNode.focus();
    } catch (error) { say(error.message); }
  });
  window.addEventListener("focus", () => {
    if (state.csrf) return refreshAll().catch((error) => say(error.message));
  });
  document.getElementById("send-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (attachmentPending) { say("Дождитесь подготовки скриншота."); return; }
    voicePlayer.stop();
    const text = inputNode.value;
    if (!text.trim()) { inputNode.focus(); return; }
    const screenshot = screenshotFile;
    if (screenshot && text.length > 1024) {
      say("Вопрос к скриншоту — не более 1 024 символов.");
      return;
    }
    if (!state.pending || state.pending.text !== text || state.pending.file !== screenshot) {
      state.pending = { text, file: screenshot, id: crypto.randomUUID() };
    }
    const sendButton = document.getElementById("send-button");
    if (sendButton.disabled) return;
    sendButton.disabled = true;
    try {
      if (!state.selected) {
        const created = await api("/api/conversations", "POST", { title: text.trim().slice(0, 60) });
        state.selected = created.id;
        state.fresh = false;
        conversationsNode.scrollTop = 0;
      }
      const result = screenshot
        ? await api(`/api/conversations/${encodeURIComponent(state.selected)}/vision`, "POST", {
          question: text, submission_id: state.pending.id,
          image_base64: await imageBase64(screenshot),
        })
        : await api(`/api/conversations/${encodeURIComponent(state.selected)}/messages`, "POST", {
          text, submission_id: state.pending.id,
        });
      state.pending = null;
      setInput("");
      clearScreenshot();
      listen(result.job.id);
      await refreshAll();
    } catch (error) { say(error.message); }
    finally { sendButton.disabled = false; }
  });

  function releaseMicrophone() {
    clearTimeout(recordTimer);
    if (microphone) microphone.getTracks().forEach((track) => track.stop());
    microphone = null;
    recorder = null;
    recordButton.hidden = false;
    stopRecording.hidden = true;
  }

  recordButton.addEventListener("click", async () => {
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      speechStatus.textContent = "Запись с микрофона недоступна.";
      return;
    }
    const mimeType = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus"]
      .find((type) => MediaRecorder.isTypeSupported(type));
    if (!mimeType) {
      speechStatus.textContent = "Поддерживаемый формат записи недоступен.";
      return;
    }
    recordButton.disabled = true;
    try {
      microphone = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      const chunks = [];
      recorder = new MediaRecorder(microphone, { mimeType });
      recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
      recorder.onstop = async () => {
        releaseMicrophone();
        const clip = new Blob(chunks, { type: mimeType });
        if (!clip.size || clip.size > 3 * 1024 * 1024) {
          speechStatus.textContent = "Запись пуста или слишком велика.";
          return;
        }
        cancelTranscription.hidden = false;
        discardTranscript = false;
        speechStatus.textContent = "Распознаю речь на компьютере…";
        try {
          const response = await fetch("/api/transcribe", {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": mimeType, "X-CSRF-Token": state.csrf },
            body: clip,
          });
          const result = await responseJson(response);
          if (discardTranscript) return;
          if (!result.text) {
            speechStatus.textContent = "Речь не обнаружена. Ничего не отправлено.";
          } else {
            setInput([inputNode.value.trim(), result.text].filter(Boolean).join("\n"));
            inputNode.focus();
            speechStatus.textContent = "Проверьте распознанный текст и нажмите «Отправить».";
          }
        } catch (error) { speechStatus.textContent = error.message; }
        finally { cancelTranscription.hidden = true; }
      };
      recorder.start();
      speechStatus.textContent = "Запись — до 19 секунд.";
      recordButton.hidden = true;
      stopRecording.hidden = false;
      recordTimer = setTimeout(() => { if (recorder?.state === "recording") recorder.stop(); }, 19000);
    } catch (error) {
      releaseMicrophone();
      speechStatus.textContent = "Нет доступа к микрофону.";
    } finally { recordButton.disabled = false; }
  });
  stopRecording.addEventListener("click", () => {
    if (recorder?.state === "recording") recorder.stop();
  });
  cancelTranscription.addEventListener("click", async () => {
    discardTranscript = true;
    cancelTranscription.disabled = true;
    try {
      await api("/api/transcribe/cancel", "POST");
      speechStatus.textContent = "Распознавание отменено. Ничего не отправлено.";
    } catch (error) { speechStatus.textContent = error.message; }
    finally { cancelTranscription.disabled = false; cancelTranscription.hidden = true; }
  });
  window.addEventListener("pagehide", () => {
    inputObserver.disconnect();
    voicePlayer.stop();
    if (recorder) {
      recorder.onstop = null;
      if (recorder.state === "recording") recorder.stop();
    }
    releaseMicrophone();
  });
  stopVoiceButton.addEventListener("click", () => voicePlayer.stop());

  api("/api/session").then(async (session) => {
    state.csrf = session.csrf_token;
    state.spokenEnabled = session.spoken_enabled;
    speechControls.hidden = !session.speech_enabled;
    visionControls.hidden = !session.vision_enabled;
    await settings.initialize(session);
    return refreshAll();
  }).catch((error) => say(error.message));
}
