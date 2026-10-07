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

if (setupForm) {
  setupForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    say("");
    const button = setupForm.querySelector("button");
    button.disabled = true;
    try {
      await responseJson(await fetch("/api/setup", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: document.getElementById("setup-token").value }),
      }));
      window.location.assign("/");
    } catch (error) {
      say(error.message);
      button.disabled = false;
    }
  });
} else {
  const state = { csrf: null, selected: null, source: null, pending: null };
  const conversationsNode = document.getElementById("conversations");
  const messagesNode = document.getElementById("messages");
  const draftNode = document.getElementById("draft");
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

  async function refreshConversation() {
    if (!state.selected) {
      titleNode.textContent = "Select a conversation";
      messagesNode.replaceChildren();
      return;
    }
    const data = await api(`/api/conversations/${encodeURIComponent(state.selected)}`);
    titleNode.textContent = data.conversation.title;
    messagesNode.replaceChildren();
    const turns = new Map(data.turns.map((turn) => [turn.id, turn]));
    const drafts = new Map(data.drafts.map((draft) => [draft.turn_id, draft]));
    const artifacts = new Map();
    for (const artifact of data.artifacts || []) {
      if (!artifacts.has(artifact.turn_id)) artifacts.set(artifact.turn_id, []);
      artifacts.get(artifact.turn_id).push(artifact);
    }
    for (const message of data.messages) {
      const card = document.createElement("article");
      card.className = `message ${message.role}`;
      line(card, message.role === "user" ? "You" : "Jarvis", "role");
      const content = document.createElement("pre");
      content.textContent = message.text;
      card.append(content);
      if (message.role === "user") {
        const turn = turns.get(message.turn_id);
        if (turn && turn.status !== "complete") {
          line(card, `Status: ${turn.status.replaceAll("_", " ")}`, "subtle");
          const draft = drafts.get(turn.id);
          if (draft) {
            const partial = document.createElement("pre");
            partial.textContent = draft.text;
            card.append(partial);
          }
        }
        for (const artifact of artifacts.get(message.turn_id) || []) {
          if (!/^[0-9a-f-]{36}$/.test(artifact.id) || artifact.kind !== "image/png") continue;
          if (artifact.status !== "available") {
            line(card, `Image ${artifact.id} is unavailable`, "subtle");
            continue;
          }
          const preview = document.createElement("img");
          preview.className = "generated-image";
          preview.alt = `Generated image ${artifact.id}`;
          preview.loading = "lazy";
          preview.src = `/api/artifacts/${artifact.id}/preview`;
          card.append(preview);
          const download = document.createElement("a");
          download.href = `/api/artifacts/${artifact.id}`;
          download.textContent = `Download image ${artifact.id}`;
          card.append(download);
        }
      }
      messagesNode.append(card);
    }
    messagesNode.scrollTop = messagesNode.scrollHeight;
  }

  async function refreshConversations() {
    const data = await api("/api/conversations");
    conversationsNode.replaceChildren();
    if (!state.selected && data.conversations.length) state.selected = data.conversations[0].id;
    for (const conversation of data.conversations) {
      const item = button(conversation.title, async () => {
        if (state.source) state.source.close();
        state.source = null;
        draftNode.textContent = "";
        state.selected = conversation.id;
        state.pending = null;
        await refreshAll();
      });
      if (conversation.id === state.selected) item.className = "selected";
      conversationsNode.append(item);
    }
  }

  function listen(jobId) {
    if (state.source) state.source.close();
    const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    state.source = source;
    source.addEventListener("progress", async (event) => {
      const snapshot = JSON.parse(event.data);
      draftNode.textContent = snapshot.draft || "";
      if (["succeeded", "failed", "cancelled"].includes(snapshot.job.status)) {
        source.close();
        if (state.source === source) state.source = null;
        draftNode.textContent = "";
        await refreshConversation();
      }
      await refreshJobs();
      await refreshApprovals();
    });
  }

  async function refreshJobs() {
    const data = await api("/api/jobs");
    jobsNode.replaceChildren();
    const visible = data.jobs.filter((job) => job.conversation_id === state.selected).slice(-10).reverse();
    if (!visible.length) line(jobsNode, "No activity yet.", "empty");
    for (const job of visible) {
      const card = document.createElement("div");
      card.className = "card";
      line(card, job.status.replaceAll("_", " "));
      line(card, job.progress.replaceAll("_", " "), "subtle");
      if (!["succeeded", "failed", "cancelled"].includes(job.status)) {
        card.append(button("Cancel", async () => {
          try { await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, "POST"); await refreshJobs(); }
          catch (error) { say(error.message); }
        }, true));
      }
      if (["queued", "interrupted", "waiting_approval"].includes(job.status)) {
        card.append(button("Resume", async () => {
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
    if (!visible.length && !(data.uncertain_files || []).length) line(approvalsNode, "Nothing needs approval.", "empty");
    for (const item of visible) {
      const card = document.createElement("div");
      card.className = "card";
      line(card, `${item.name} · version ${item.version}`);
      line(card, `Target: ${item.target}`, "subtle");
      line(card, `Job: ${item.job_id} · policy: ${item.policy_version}`, "subtle");
      if (item.name === "create_text_file" && item.preview) {
        line(card, `Create new file: ${item.preview.path}`);
        line(card, "Exact UTF-8 content to write:");
        const content = document.createElement("pre");
        content.textContent = item.preview.content;
        card.append(content);
      } else {
        const argumentsText = document.createElement("pre");
        argumentsText.textContent = item.arguments;
        card.append(argumentsText);
      }
      if (item.actionable) {
        card.append(button("Approve", async () => {
          try { await api(`/api/approvals/${encodeURIComponent(item.invocation_id)}/approve`, "POST"); listen(item.job_id); await refreshApprovals(); }
          catch (error) { say(error.message); }
        }));
      } else {
        line(card, "This approval expired or was invalidated. Cancel this job and make a new request.", "subtle");
      }
      card.append(button("Reject", async () => {
        try { await api(`/api/approvals/${encodeURIComponent(item.invocation_id)}/reject`, "POST"); await refreshAll(); }
        catch (error) { say(error.message); }
      }, true));
      approvalsNode.append(card);
    }
    for (const item of data.uncertain_files || []) {
      const card = document.createElement("div");
      card.className = "card";
      line(card, `Uncertain file creation: ${item.path}`);
      line(card, "Verification checks the exact expected file and content. It never creates the file.", "subtle");
      card.append(button("Verify file", async () => {
        try { await api(`/api/file-reconciliation/${encodeURIComponent(item.invocation_id)}`, "POST"); await refreshAll(); }
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
    const title = window.prompt("Conversation name", "New conversation");
    if (title === null) return;
    try {
      const created = await api("/api/conversations", "POST", { title });
      state.selected = created.id;
      await refreshAll();
    } catch (error) { say(error.message); }
  });
  document.getElementById("refresh").addEventListener("click", () => refreshAll().catch((error) => say(error.message)));
  document.getElementById("send-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.selected) { say("Create a conversation first."); return; }
    const text = inputNode.value;
    if (!state.pending || state.pending.text !== text) {
      state.pending = { text, id: crypto.randomUUID() };
    }
    const sendButton = document.getElementById("send-button");
    sendButton.disabled = true;
    try {
      const result = await api(`/api/conversations/${encodeURIComponent(state.selected)}/messages`, "POST", {
        text, submission_id: state.pending.id,
      });
      state.pending = null;
      inputNode.value = "";
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
      speechStatus.textContent = "Microphone recording is unavailable in this browser.";
      return;
    }
    const mimeType = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus"]
      .find((type) => MediaRecorder.isTypeSupported(type));
    if (!mimeType) {
      speechStatus.textContent = "This browser has no supported audio recorder.";
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
          speechStatus.textContent = "Recording is empty or too large.";
          return;
        }
        cancelTranscription.hidden = false;
        discardTranscript = false;
        speechStatus.textContent = "Transcribing locally…";
        try {
          const response = await fetch("/api/transcribe", {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": mimeType, "X-CSRF-Token": state.csrf },
            body: clip,
          });
          const result = await responseJson(response);
          if (discardTranscript) return;
          if (!result.text) {
            speechStatus.textContent = "No speech detected. Nothing was sent.";
          } else {
            inputNode.value = [inputNode.value.trim(), result.text].filter(Boolean).join("\n");
            inputNode.focus();
            speechStatus.textContent = "Review and edit the transcript, then press Send.";
          }
        } catch (error) { speechStatus.textContent = error.message; }
        finally { cancelTranscription.hidden = true; }
      };
      recorder.start();
      speechStatus.textContent = "Recording (up to 19 seconds).";
      recordButton.hidden = true;
      stopRecording.hidden = false;
      recordTimer = setTimeout(() => { if (recorder?.state === "recording") recorder.stop(); }, 19000);
    } catch (error) {
      releaseMicrophone();
      speechStatus.textContent = "Microphone access was denied or unavailable.";
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
      speechStatus.textContent = "Transcription cancelled. Nothing was sent.";
    } catch (error) { speechStatus.textContent = error.message; }
    finally { cancelTranscription.disabled = false; cancelTranscription.hidden = true; }
  });
  window.addEventListener("pagehide", () => {
    if (recorder) {
      recorder.onstop = null;
      if (recorder.state === "recording") recorder.stop();
    }
    releaseMicrophone();
  });

  api("/api/session").then((session) => {
    state.csrf = session.csrf_token;
    speechControls.hidden = !session.speech_enabled;
    return refreshAll();
  }).catch((error) => say(error.message));
}
