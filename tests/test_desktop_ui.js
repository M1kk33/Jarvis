"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { randomUUID } = require("node:crypto");
const source = fs.readFileSync(path.join(__dirname, "../src/jarvis/interfaces/static/app.js"), "utf8");
const markdownSource = fs.readFileSync(path.join(__dirname, "../src/jarvis/interfaces/static/markdown.js"), "utf8");
const marked = require("../src/jarvis/interfaces/static/marked.umd.js");

function fixture({ vision = false, spoken = false, clipboard, regenerate, decode, history, loadHistory, pageText = "<script>source is literal</script>", sendShortcut = "enter", conversations: initialConversations = [] } = {}) {
  class Element {
    constructor() { this.children = []; this.handlers = {}; this.hidden = true; this.value = ""; this.files = []; this.textContent = ""; this.style = { setProperty(name, value) { this[name] = value; } }; this.scrollHeight = 36; this.clientWidth = 1280; this.classes = new Set(); this.classList = { add: name => this.classes.add(name), remove: name => this.classes.delete(name) }; this.captured = new Set(); }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    set textContent(value) { this._text = value; this.children = []; }
    append(...items) { for (const item of items) this.children.push(...(item.fragment ? item.children : [item])); }
    replaceChildren(...items) { this._text = ""; this.children = []; this.append(...items); }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    click() { this.clicks = (this.clicks || 0) + 1; this.handlers.click?.(); }
    requestSubmit() { return this.handlers.submit({ preventDefault() {} }); }
    setPointerCapture(id) { this.captured.add(id); }
    hasPointerCapture(id) { return this.captured.has(id); }
    releasePointerCapture(id) { this.captured.delete(id); this.handlers.lostpointercapture?.({ pointerId: id }); }
  }
  const elements = new Map();
  const get = (id) => {
    if (["setup-form", "refresh", "toggle-activity", "approval-count"].includes(id)) return null;
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const requests = [];
  let conversations = [...initialConversations];
  let createdConversations = 0;
  let approvals = { approvals: [], uncertain_actions: [], uncertain_files: [] };
  let failSend = false;
  let failPin = false;
  let failDelete = false;
  let jobs = [];
  const sources = [];
  const handlers = {};
  const canvases = [];
  const revoked = [];
  const voiceCalls = [];
  const document = { getElementById: get, createElement: (tag) => {
    const el = new Element();
    el.tagName = tag;
    if (tag === "canvas") {
      el.getContext = () => ({ drawImage() {} });
      el.toBlob = (callback) => callback(new Blob(["png-test"], { type: "image/png" }));
      canvases.push(el);
    }
    return el;
  }, createDocumentFragment() { const el = new Element(); el.fragment = true; return el; }, createTextNode(text) { const el = new Element(); el.textContent = text; return el; }, createElementNS() { return new Element(); }, addEventListener(name, handler) { handlers[name] = handler; } };
  const windowHandlers = {};
  const observers = [];
  const window = {
    marked,
    location: { hash: "", pathname: "/", search: "" }, addEventListener(name, handler) { windowHandlers[name] = handler; },
    getComputedStyle() { return { minHeight: "36px", maxHeight: "156px" }; },
    JarvisVoicePlayer: class {
      constructor(options) { this.options = options; }
      isPlaying(conversationId, turnId, messageId) { return this.active?.messageId === messageId && this.active?.turnId === turnId && this.active?.conversationId === conversationId; }
      play(conversationId, turnId, messageId) { this.active = { conversationId, turnId, messageId }; voiceCalls.push(this.active); this.options.onActive(true); }
      stop() { this.active = null; this.options.onActive(false); }
      setVolume() {}
    },
    JarvisSettings: class { constructor() { this.preferences = { send_shortcut: sendShortcut }; } async initialize() {} },
  };
  const context = vm.createContext({
    window, document, navigator: { clipboard }, URLSearchParams, crypto: { randomUUID }, Blob, File, clearTimeout,
    ResizeObserver: class { constructor(callback) { this.callback = callback; observers.push(this); } observe(target) { this.target = target; } disconnect() { this.disconnected = true; } },
    URL: { createObjectURL: () => "blob:preview", revokeObjectURL: (url) => revoked.push(url) },
    createImageBitmap: decode || (async () => ({ width: 1920, height: 1080, close() {} })),
    FileReader: class {
      readAsDataURL(file) { this.result = "data:image/png;base64,cG5nLXRlc3Q="; this.onload(); }
    },
    EventSource: class {
      constructor() { this.handlers = {}; sources.push(this); }
      addEventListener(name, handler) { this.handlers[name] = handler; }
      close() { this.closed = true; }
    },
    fetch: async (url, options = {}) => {
      const payload = options.body ? JSON.parse(options.body) : null;
      requests.push({ url, method: options.method, payload });
      let value;
      let ok = true;
      if (url === "/api/session") value = { csrf_token: "test", speech_enabled: false, spoken_enabled: spoken, vision_enabled: vision };
      else if (url === "/api/conversations" && options.method === "POST") {
        value = { id: `conversation-${++createdConversations}`, title: payload.title };
        conversations = [value, ...conversations];
      } else if (url === "/api/conversations") value = { conversations: [...conversations.filter((c) => c.pinned), ...conversations.filter((c) => !c.pinned)] };
      else if (url.endsWith("/pin")) {
        const id = decodeURIComponent(url.split("/").at(-2));
        if (failPin) { value = { detail: "Pin request failed" }; ok = false; }
        else {
          conversations = conversations.map((c) => c.id === id ? { ...c, pinned: payload.pinned } : c);
          value = conversations.find((c) => c.id === id);
        }
      }
      else if (url === "/api/jobs") value = { jobs };
      else if (url.startsWith("/api/pages/")) value = { text: pageText };
      else if (options.method === "DELETE") {
        if (failDelete) { value = { detail: "Delete request failed" }; ok = false; }
        else { conversations = conversations.filter(c => !url.endsWith(`/${c.id}`)); value = { deleted: true }; }
      }
      else if (url === "/api/approvals") value = approvals;
      else if (url.endsWith("/regenerate")) {
        value = regenerate ? await regenerate(payload) : { job: { id: "job-regenerated" } };
        ok = !value.detail;
      }
      else if (url.endsWith("/messages") || url.endsWith("/vision")) {
        value = failSend ? { detail: "Try again" } : { job: { id: "job-1" } };
        ok = !failSend;
      } else {
        value = { conversation: conversations.find((c) => url.endsWith(`/${c.id}`)), messages: [], turns: [], drafts: [], ...history };
        if (loadHistory) value = await loadHistory(value);
      }
      return { ok, status: ok ? 200 : 503, json: async () => value };
    },
  });
  vm.runInContext(markdownSource, context);
  vm.runInContext(source, context);
  return { get, requests, handlers, canvases, revoked, sources, observers, voiceCalls, resizeWindow(width) { get("app-layout").clientWidth = width; observers.find(o => o.target === get("app-layout")).callback(); }, blurApp() { windowHandlers.blur(); }, hidePage() { windowHandlers.pagehide(); }, focusApp() { return windowHandlers.focus(); }, activeElement() { return document.activeElement; }, failPin(value) { failPin = value; }, failDelete(value) { failDelete = value; }, setJobs(value) { jobs = value; }, setApprovals(value) { approvals = value; }, failSend(value) { failSend = value; } };
}
const settle = () => new Promise(setImmediate);

test("deletion requires confirmation, handles failure and keeps drafts and other selection", async () => {
  const f = fixture({ conversations: [{ id: "chat", title: "Current" }, { id: "old", title: "Other <img>" }] });
  await settle();
  const nav = f.get("conversations");
  f.get("message-input").value = "Unsent draft";
  nav.children[1].children[2].handlers.click();
  assert.equal(f.get("delete-chat-dialog").open, true);
  assert.equal(f.get("delete-chat-name").textContent, "Other <img>");
  assert.equal(f.activeElement(), f.get("cancel-delete-chat"));
  f.get("cancel-delete-chat").handlers.click();
  assert.equal(f.requests.some(r => r.method === "DELETE"), false);
  nav.children[1].children[2].handlers.click();
  f.failDelete(true);
  await f.get("delete-chat-form").requestSubmit();
  assert.equal(nav.children.length, 2);
  assert.equal(f.get("delete-chat-dialog").open, true);
  assert.equal(f.get("delete-chat-status").textContent, "Delete request failed");
  assert.equal(f.get("message-input").value, "Unsent draft");
  f.failDelete(false);
  const pending = f.get("delete-chat-form").requestSubmit();
  await f.get("delete-chat-form").requestSubmit();
  await pending;
  assert.equal(f.requests.filter(r => r.method === "DELETE").length, 2);
  assert.equal(nav.children.length, 1);
  assert.match(nav.children[0].className, /selected/);
  assert.equal(f.get("conversation-title").textContent, "Current");
  assert.equal(f.get("message-input").value, "Unsent draft");
  nav.children[0].children[2].handlers.click();
  await f.get("delete-chat-form").requestSubmit();
  assert.equal(nav.children.length, 0);
  assert.equal(f.get("conversation-title").textContent, "Новый диалог");
  assert.equal(f.get("message-input").value, "Unsent draft");
  assert.equal(f.get("delete-chat-dialog").open, false);
  await f.focusApp();
  assert.equal(nav.children.length, 0);
});

const replyHistory = () => ({ turns: [{ id: "turn", status: "complete" }], messages: [
  { id: "user", turn_id: "turn", role: "user", text: "Question" },
  { id: "answer", turn_id: "turn", role: "assistant", text: "**Bold**\n\n```js\n1 < 2\n```" },
], drafts: [] });
function findAction(f, label) {
  const visit = node => node["aria-label"] === label ? node : node.children.map(visit).find(Boolean);
  return visit(f.get("messages"));
}

test("reply actions use compact accessible icons and copy committed raw Markdown with failure feedback", async () => {
  const copied = [];
  let denied = false;
  const f = fixture({ conversations: [{ id: "chat", title: "Chat" }], history: replyHistory(), clipboard: { async writeText(text) { if (denied) throw new Error(); copied.push(text); } } });
  await settle();
  const copy = findAction(f, "Копировать ответ");
  assert.equal(copy.textContent, "");
  assert.match(copy.className, /reply-action/);
  assert.equal(copy.children[0]["aria-hidden"], "true");
  assert.equal(findAction(f, "Озвучить"), undefined);
  await copy.handlers.click();
  assert.deepEqual(copied, [replyHistory().messages[1].text]);
  assert.match(f.get("messages").textContent, /Скопировано/);
  denied = true;
  await copy.handlers.click();
  assert.match(f.get("messages").textContent, /Не удалось скопировать/);
  assert.equal(f.requests.some(r => r.method === "POST"), false);
});

test("speaker icon toggles the existing player and keeps its pressed/stop label through refresh", async () => {
  const f = fixture({ spoken: true, conversations: [{ id: "chat", title: "Chat" }], history: replyHistory() });
  await settle();
  const speak = findAction(f, "Озвучить");
  speak.handlers.click();
  assert.equal(speak["aria-pressed"], "true");
  assert.equal(speak["aria-label"], "Остановить озвучивание");
  assert.equal(f.voiceCalls.length, 1);
  assert.equal(f.voiceCalls[0].messageId, "answer");
  await f.focusApp();
  const active = findAction(f, "Остановить озвучивание");
  assert.equal(active["aria-pressed"], "true");
  active.handlers.click();
  assert.equal(active["aria-label"], "Озвучить");
  assert.equal(active["aria-pressed"], "false");
  assert.equal(f.voiceCalls.length, 1);
});

test("regeneration keeps retry identity, rejects double click and preserves unsent input", async () => {
  let failed = true;
  const history = replyHistory();
  const f = fixture({ conversations: [{ id: "chat", title: "Chat" }], history, regenerate: async () => {
    if (failed) return { detail: "Try again" };
    history.turns.push({ id: "version", status: "pending", regeneration_of: "turn" });
    history.messages.push({ id: "user-version", turn_id: "version", role: "user", text: "Question" });
    return { job: { id: "job-version" } };
  } });
  await settle();
  f.get("message-input").value = "Unsent draft";
  const retry = findAction(f, "Перегенерировать ответ");
  const first = retry.handlers.click();
  await retry.handlers.click();
  await first;
  assert.equal(f.requests.filter(r => r.url.endsWith("/regenerate")).length, 1);
  failed = false;
  await findAction(f, "Перегенерировать ответ").handlers.click();
  const calls = f.requests.filter(r => r.url.endsWith("/regenerate"));
  assert.equal(calls.length, 2);
  assert.equal(calls[0].payload.submission_id, calls[1].payload.submission_id);
  assert.equal(calls[1].url, "/api/conversations/chat/turns/turn/regenerate");
  assert.equal(f.get("message-input").value, "Unsent draft");
  assert.equal(f.sources.length, 1);
  assert.equal(f.get("messages").children.filter(c => c.className === "message user").length, 1);
  assert.match(f.get("messages").textContent, /Готовлю новую версию ответа/);
  await findAction(f, "Предыдущая версия ответа").handlers.click();
  assert.match(f.get("messages").textContent, /Bold/);
  assert.equal(findAction(f, "Перегенерировать ответ").disabled, true);
});

test("failed versions retain drafts and pager selection while old replies have no regeneration action", async () => {
  const history = replyHistory();
  history.turns.push({ id: "version", status: "failed", regeneration_of: "turn" });
  history.messages.push({ id: "user-version", turn_id: "version", role: "user", text: "Question" });
  history.drafts.push({ turn_id: "version", text: "Partial version" });
  const f = fixture({ conversations: [{ id: "chat", title: "Chat" }], history });
  await settle();
  assert.match(f.get("messages").textContent, /Partial version/);
  assert.equal(findAction(f, "Копировать ответ"), undefined);
  await findAction(f, "Предыдущая версия ответа").handlers.click();
  await f.focusApp();
  assert.match(f.get("messages").textContent, /Bold/);
  await findAction(f, "Следующая версия ответа").handlers.click();
  assert.match(f.get("messages").textContent, /Partial version/);
  history.turns.push({ id: "next", status: "complete" });
  history.messages.push({ id: "next-user", turn_id: "next", role: "user", text: "Next" }, { id: "next-answer", turn_id: "next", role: "assistant", text: "Latest reply" });
  await f.focusApp();
  await findAction(f, "Предыдущая версия ответа").handlers.click();
  const actions = f.get("messages").children.filter(c => c.className === "message assistant").map(c => c.children.at(-1));
  assert.equal(actions[0].children.some(c => c["aria-label"] === "Перегенерировать ответ"), false);
  assert.equal(actions[1].children.some(c => c["aria-label"] === "Перегенерировать ответ"), true);
});

test("sidebar dragging captures one pointer, preserves the draft/chat and stops on release, cancellation and blur", async () => {
  const f = fixture({ conversations: [{ id: "chat", title: "Existing chat" }] });
  await settle();
  const handle = f.get("sidebar-resizer"), layout = f.get("app-layout");
  const pointer = (x, id = 7, button = 0) => ({ clientX: x, pointerId: id, button, isPrimary: true, preventDefault() {} });
  f.get("message-input").value = "Unsent message";
  f.get("conversations").scrollTop = 240;
  handle.handlers.pointerdown(pointer(226, 8, 2));
  assert.equal(handle.hasPointerCapture(8), false);
  handle.handlers.pointerdown(pointer(226));
  assert.equal(handle.hasPointerCapture(7), true);
  handle.handlers.pointermove(pointer(400, 9));
  assert.equal(layout.style["--sidebar-width"], "226px");
  handle.handlers.pointermove(pointer(360));
  assert.equal(layout.style["--sidebar-width"], "360px");
  handle.handlers.pointerup(pointer(360));
  assert.equal(handle.hasPointerCapture(7), false);
  assert.equal(layout.classes.has("sidebar-resizing"), false);
  handle.handlers.pointermove(pointer(180));
  assert.equal(layout.style["--sidebar-width"], "360px");
  handle.handlers.pointerdown(pointer(360)); handle.handlers.pointermove(pointer(200));
  handle.handlers.pointercancel(pointer(200));
  assert.equal(layout.style["--sidebar-width"], "360px");
  handle.handlers.pointerdown(pointer(360)); handle.handlers.pointermove(pointer(300)); f.blurApp();
  assert.equal(layout.style["--sidebar-width"], "300px");
  assert.equal(handle.hasPointerCapture(7), false);
  assert.equal(f.get("message-input").value, "Unsent message");
  assert.equal(f.get("conversation-title").textContent, "Existing chat");
  assert.equal(f.get("conversations").scrollTop, 240);
  assert.equal(f.requests.some(r => r.method === "POST"), false);
});

test("sidebar keyboard limits follow the viewport, reset defaults and cancel with Escape", async () => {
  const f = fixture(); await settle();
  const handle = f.get("sidebar-resizer"), layout = f.get("app-layout");
  const key = key => handle.handlers.keydown({ key, preventDefault() {}, stopPropagation() {} });
  key("End"); assert.equal(layout.style["--sidebar-width"], "420px");
  f.resizeWindow(520); assert.equal(layout.style["--sidebar-width"], "200px");
  assert.equal(handle["aria-valuemax"], "200");
  key("ArrowRight"); assert.equal(layout.style["--sidebar-width"], "200px");
  key("Home"); assert.equal(layout.style["--sidebar-width"], "150px");
  key("ArrowLeft"); assert.equal(layout.style["--sidebar-width"], "150px");
  key("ArrowRight"); assert.equal(layout.style["--sidebar-width"], "160px");
  handle.handlers.dblclick(); assert.equal(layout.style["--sidebar-width"], "150px");
  f.resizeWindow(850); assert.equal(layout.style["--sidebar-width"], "210px");
  const pointer = x => ({ clientX: x, pointerId: 7, button: 0, isPrimary: true, preventDefault() {} });
  handle.handlers.pointerdown(pointer(210)); handle.handlers.pointermove(pointer(350)); key("Escape");
  assert.equal(layout.style["--sidebar-width"], "210px");
  f.resizeWindow(1280); assert.equal(layout.style["--sidebar-width"], "226px");
  handle.handlers.pointerdown(pointer(226)); f.hidePage();
  assert.equal(handle.hasPointerCapture(7), false);
  assert.ok(f.observers.every(o => o.disconnected));
});

test("compact input caps its height, keeps a failed draft and shrinks after send or reset", async () => {
  const f = fixture();
  await settle();
  const input = f.get("message-input");
  input.value = "A long draft";
  input.scrollHeight = 400;
  input.handlers.input();
  assert.equal(input.style.height, "156px");
  assert.equal(input.style.overflowY, "auto");
  f.failSend(true);
  await f.get("send-form").requestSubmit();
  assert.equal(input.value, "A long draft");
  assert.equal(input.style.height, "156px");
  // Empty content has one line of layout in a real textarea.
  input.scrollHeight = 36;
  f.failSend(false);
  await f.get("send-form").requestSubmit();
  assert.equal(input.value, "");
  assert.equal(input.style.height, "36px");
  assert.equal(input.style.overflowY, "hidden");
  input.style.height = "156px";
  await f.get("new-conversation").handlers.click();
  assert.equal(input.style.height, "36px");
});

test("attachment icon opens the file picker without submitting or clearing text", async () => {
  const f = fixture({ vision: true });
  await settle();
  f.get("message-input").value = "Keep this draft";
  f.get("attach-image").click();
  assert.equal(f.get("screenshot-input").clicks, 1);
  assert.equal(f.get("message-input").value, "Keep this draft");
  assert.equal(f.requests.some((r) => r.method === "POST"), false);
});

test("new conversations appear at the top and reveal it without resetting older-chat browsing", async () => {
  const f = fixture({ conversations: [
    { id: "recent", title: "Recent" }, { id: "old", title: "Old" },
  ] });
  await settle();
  const nav = f.get("conversations");
  assert.deepEqual(nav.children.map((row) => row.children[0].textContent), ["Recent", "Old"]);
  assert.match(nav.children[0].className, /selected/);
  assert.ok(f.requests.some((r) => r.url === "/api/conversations/recent"));
  nav.scrollTop = 400;
  await nav.children[1].children[0].handlers.click();
  assert.equal(nav.scrollTop, 400);
  assert.match(nav.children[1].className, /selected/);
  await f.get("new-conversation").handlers.click();
  assert.equal(nav.scrollTop, 0);
  nav.scrollTop = 400;
  f.get("message-input").value = "Newest";
  await f.get("send-form").requestSubmit();
  assert.equal(nav.scrollTop, 0);
  assert.deepEqual(nav.children.map((row) => row.children[0].textContent), ["Newest", "Recent", "Old"]);
  assert.match(nav.children[0].className, /selected/);
  await f.focusApp();
  assert.equal(nav.children[0].children[0].textContent, "Newest");
});

test("pinning moves a chat above newer ones without switching or discarding a draft", async () => {
  const f = fixture({ conversations: [{ id: "recent", title: "Recent" }, { id: "old", title: "Old" }] });
  await settle();
  const nav = f.get("conversations");
  f.get("message-input").value = "Unsent draft";
  const pin = nav.children[1].children[1];
  pin.focus();
  await pin.handlers.click();
  assert.deepEqual(f.requests.find((r) => r.url === "/api/conversations/old/pin").payload, { pinned: true });
  assert.deepEqual(nav.children.map((row) => row.children[0].textContent), ["Old", "Recent"]);
  assert.equal(nav.children[0].children[1]["aria-pressed"], "true");
  assert.equal(f.activeElement(), nav.children[0].children[1]);
  assert.match(nav.children[1].className, /selected/);
  assert.equal(f.get("message-input").value, "Unsent draft");
  assert.equal(f.get("conversation-title").textContent, "Recent");
  await nav.children[0].children[1].handlers.click();
  assert.deepEqual(nav.children.map((row) => row.children[0].textContent), ["Recent", "Old"]);
  assert.equal(nav.children[1].children[1]["aria-pressed"], "false");
  f.failPin(true);
  const failedPin = nav.children[1].children[1];
  await failedPin.handlers.click();
  assert.equal(failedPin.disabled, false);
  assert.equal(failedPin["aria-pressed"], "false");
  assert.equal(f.get("notice").textContent, "Pin request failed");
  assert.equal(f.requests.filter((r) => r.url.endsWith("/messages")).length, 0);
});

test("image progress updates the message and ignores stale closed streams", async () => {
  const f = fixture({ history: {
    messages: [{ id: "message-1", turn_id: "turn-1", role: "user", text: "Cat" }],
    turns: [{ id: "turn-1", status: "pending" }],
  } });
  await settle();
  const job = { id: "job-1", conversation_id: "conversation-1", turn_id: "turn-1", status: "running", progress: "image_loading", updated_at: "2026-10-09T10:00:00Z" };
  f.setJobs([job]);
  f.get("message-input").value = "Cat";
  await f.get("send-form").requestSubmit();
  const strings = (node) => [node.textContent, ...node.children.flatMap(strings)].join(" ");
  assert.match(strings(f.get("messages")), /Подготавливаю модель изображений/);
  const source = f.sources.at(-1);
  const newer = { ...job, progress: "image_generating", updated_at: "2026-10-09T10:00:01Z" };
  await source.handlers.progress({ data: JSON.stringify({ job: newer }) });
  // /api/jobs still returns the older snapshot; it must not undo the SSE update.
  assert.match(strings(f.get("messages")), /Генерирую изображение/);
  await f.get("new-conversation").handlers.click();
  await source.handlers.progress({ data: JSON.stringify({ job: newer, draft: "stale draft" }) });
  assert.doesNotMatch(strings(f.get("messages")), /stale draft/);
  assert.equal(f.get("conversation-title").textContent, "Новый диалог");
});

test("streaming reply stays in assistant flow through refresh, completion and stale events", async () => {
  const history = {
    messages: [{ id: "user-1", turn_id: "turn-1", role: "user", text: "Story" }],
    turns: [{ id: "turn-1", status: "generating" }],
    drafts: [{ turn_id: "turn-1", text: "Saved partial" }],
  };
  const f = fixture({ history });
  await settle();
  f.get("message-input").value = "Story";
  await f.get("send-form").requestSubmit();
  const messages = f.get("messages");
  const partial = () => messages.children[1];
  assert.equal(partial().className, "message assistant");
  assert.equal(partial().children[0].textContent, "Jarvis");
  assert.equal(partial().children[1].textContent, "Saved partial");
  const job = { id: "job-1", conversation_id: "conversation-1", turn_id: "turn-1", status: "running", progress: "generating", updated_at: "2026-10-11T10:00:00Z" };
  const source = f.sources.at(-1);
  const progress = (text, status = "running") => source.handlers.progress({ data: JSON.stringify({ job: { ...job, status }, draft: text }) });
  messages.clientHeight = 400;
  messages.scrollHeight = 1000;
  messages.scrollTop = 600;
  await progress("Live partial");
  assert.equal(partial().children[1].textContent, "Live partial");
  assert.equal(messages.scrollTop, 1000);
  messages.scrollTop = 100;
  await progress("Live partial with more text");
  assert.equal(messages.scrollTop, 100, "reading older content must not be interrupted");
  await f.focusApp();
  assert.equal(messages.children.length, 2, "refresh must not duplicate the draft inside the user card");
  assert.equal(messages.children[0].children.length, 3);
  assert.equal(partial().children[1].textContent, "Live partial with more text", "older persisted checkpoint must not replace live text");
  history.turns[0].status = "complete";
  history.messages.push({ id: "assistant-1", turn_id: "turn-1", role: "assistant", text: "Final story" });
  await progress("Final story", "succeeded");
  assert.equal(source.closed, true);
  assert.equal(messages.children.length, 2);
  assert.equal(partial().className, "message assistant");
  assert.equal(partial().children[1].textContent, "Final story");
  await progress("stale terminal draft");
  assert.equal(partial().children[1].textContent, "Final story");
});

test("assistant Markdown renders on restored drafts, streaming and completion; user text stays literal", async () => {
  const userText = "**literal** <img src=x>";
  const history = {
    messages: [{ id: "user-1", turn_id: "turn-1", role: "user", text: userText }],
    turns: [{ id: "turn-1", status: "generating" }],
    drafts: [{ turn_id: "turn-1", text: "**Сохранено**" }],
  };
  const f = fixture({ history });
  await settle();
  f.get("message-input").value = userText;
  await f.get("send-form").requestSubmit();
  const cards = () => f.get("messages").children;
  assert.equal(cards()[0].children[1].tagName, "pre");
  assert.equal(cards()[0].children[1].textContent, userText);
  const formatted = () => cards()[1].children[1];
  assert.equal(formatted().className, "markdown");
  assert.equal(formatted().children[0].children[0].tagName, "strong");
  assert.equal(formatted().textContent, "Сохранено");
  const source = f.sources.at(-1);
  const event = (draft, status = "running") => source.handlers.progress({ data: JSON.stringify({
    job: { id: "job-1", conversation_id: "conversation-1", turn_id: "turn-1", status, progress: "generating" }, draft,
  }) });
  await event("**Пишу");
  assert.equal(formatted().textContent, "**Пишу");
  await event("**Пишу**");
  assert.equal(formatted().children[0].children[0].tagName, "strong");
  await f.focusApp();
  assert.equal(formatted().textContent, "Пишу");
  history.turns[0].status = "complete";
  history.messages.push({ id: "assistant-1", turn_id: "turn-1", role: "assistant", text: "# Итог\n\n**Ответ**" });
  await event("# Итог\n\n**Ответ**", "succeeded");
  assert.equal(cards().length, 2);
  assert.equal(formatted().children[0].tagName, "h1");
  assert.equal(formatted().children[1].children[0].tagName, "strong");
  await event("stale text");
  assert.equal(formatted().children[0].tagName, "h1");
});

test("generated images belong to Jarvis while uploads stay with their user's turn", async () => {
  const generated = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const uploaded = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const unavailable = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const history = {
    messages: [
      { id: "user-1", turn_id: "turn-1", role: "user", text: "Draw it" },
      { id: "assistant-1", turn_id: "turn-1", role: "assistant", text: "Here it is" },
      { id: "user-2", turn_id: "turn-2", role: "user", text: "Describe my screenshot" },
      { id: "assistant-2", turn_id: "turn-2", role: "assistant", text: "Description" },
    ],
    turns: [{ id: "turn-1", status: "complete" }, { id: "turn-2", status: "complete" }],
    artifacts: [
      { id: generated, turn_id: "turn-1", kind: "image/png", status: "available" },
      { id: uploaded, turn_id: "turn-2", kind: "screenshot/png", status: "available" },
      { id: unavailable, turn_id: "turn-1", kind: "image/png", status: "unavailable" },
      { id: "../../invalid", turn_id: "turn-1", kind: "image/png", status: "available" },
      { id: uploaded, turn_id: "turn-1", kind: "unsupported", status: "available" },
    ],
  };
  const f = fixture({ conversations: [{ id: "saved", title: "Saved" }], history });
  await settle();
  const images = card => card.children.filter(node => node.className === "generated-image");
  const links = card => card.children.filter(node => node.href);
  for (let refresh = 0; refresh < 2; refresh++) {
    const cards = f.get("messages").children;
    assert.equal(cards.length, 4);
    assert.equal(images(cards[0]).length, 0);
    assert.equal(cards[1].children[0].textContent, "Jarvis");
    assert.deepEqual(images(cards[1]).map(node => node.src), [`/api/artifacts/${generated}/preview`]);
    assert.deepEqual(links(cards[1]).map(node => node.href), [`/api/artifacts/${generated}`]);
    assert.ok(cards[1].children.some(node => node.textContent.includes(unavailable)));
    assert.equal(cards[2].children[0].textContent, "Вы");
    assert.deepEqual(images(cards[2]).map(node => node.src), [`/api/artifacts/${uploaded}/preview`]);
    assert.equal(images(cards[2])[0].alt, "Исходный скриншот");
    assert.equal(images(cards[3]).length, 0);
    await f.focusApp();
  }
  assert.equal(f.requests.some(request => request.method === "POST"), false);
});

for (const status of ["pending", "generating", "failed", "cancelled", "interrupted"]) {
  test(`a committed image without reply text stays under Jarvis for a ${status} turn`, async () => {
    const id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const history = {
      messages: [{ id: "user-1", turn_id: "turn-1", role: "user", text: "Draw it" }],
      turns: [{ id: "turn-1", status }],
      artifacts: [{ id, turn_id: "turn-1", kind: "image/png", status: "available" }],
    };
    const f = fixture({ history });
    await settle();
    f.get("message-input").value = "Draw it";
    await f.get("send-form").requestSubmit();
    const assertImage = () => {
      const cards = f.get("messages").children;
      assert.equal(cards.length, 2);
      assert.equal(cards[0].children.some(node => node.src), false);
      assert.equal(cards[1].className, "message assistant");
      assert.equal(cards[1].children[0].textContent, "Jarvis");
      assert.equal(cards[1].hidden, false);
      assert.equal(cards[1].children.filter(node => node.src).length, 1);
    };
    assertImage();
    const source = f.sources.at(-1);
    await source.handlers.progress({ data: JSON.stringify({
      job: { id: "job-1", conversation_id: "conversation-1", turn_id: "turn-1", status: "running", progress: "generating" }, draft: "",
    }) });
    assertImage();
    await f.focusApp();
    assertImage();
    history.turns[0].status = "complete";
    history.messages.push({ id: "assistant-1", turn_id: "turn-1", role: "assistant", text: "Here it is" });
    await source.handlers.progress({ data: JSON.stringify({
      job: { id: "job-1", conversation_id: "conversation-1", turn_id: "turn-1", status: "succeeded" }, draft: "Here it is",
    }) });
    const cards = f.get("messages").children;
    assert.equal(cards.length, 2);
    assert.equal(cards[1].children[1].textContent, "Here it is");
    assert.equal(cards[1].children.filter(node => node.src).length, 1);
  });
}

test("an unavailable generated image keeps a Jarvis placeholder visible without text", async () => {
  const id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const f = fixture({ conversations: [{ id: "saved", title: "Saved" }], history: {
    messages: [{ id: "user-1", turn_id: "turn-1", role: "user", text: "Draw it" }],
    turns: [{ id: "turn-1", status: "failed" }],
    artifacts: [{ id, turn_id: "turn-1", kind: "image/png", status: "unavailable" }],
  } });
  await settle();
  const card = f.get("messages").children[1];
  assert.equal(card.children[0].textContent, "Jarvis");
  assert.equal(card.hidden, false);
  assert.ok(card.children.some(node => node.textContent.includes(id)));
  assert.equal(card.children.some(node => node.src || node.href), false);
});

for (const status of ["failed", "cancelled", "interrupted"]) {
  test(`restored ${status} draft remains an assistant message beside its turn`, async () => {
    const f = fixture({ conversations: [{ id: "saved", title: "Saved" }], history: {
      messages: [
        { id: "user-1", turn_id: "turn-1", role: "user", text: "Earlier" },
        { id: "user-2", turn_id: "turn-2", role: "user", text: "Later" },
      ],
      turns: [{ id: "turn-1", status }, { id: "turn-2", status: "pending" }],
      drafts: [{ turn_id: "turn-1", text: "Preserved partial" }],
    } });
    await settle();
    const cards = f.get("messages").children;
    assert.equal(cards[1].className, "message assistant");
    assert.equal(cards[1].children[1].textContent, "Preserved partial");
    assert.equal(cards[1].hidden, false);
    assert.equal(cards[2].className, "message user");
    assert.equal(cards[3].hidden, true, "empty pending draft must not add visible space");
  });
}

test("a delayed conversation fetch cannot put a draft back after terminal refresh", async () => {
  let defer = false;
  let release;
  const history = {
    messages: [{ id: "user-1", turn_id: "turn-1", role: "user", text: "Story" }],
    turns: [{ id: "turn-1", status: "generating" }], drafts: [],
  };
  const f = fixture({ history, loadHistory: (value) => {
    if (!defer) return value;
    defer = false;
    const snapshot = JSON.parse(JSON.stringify(value));
    return new Promise((resolve) => { release = () => resolve(snapshot); });
  } });
  await settle();
  f.get("message-input").value = "Story";
  await f.get("send-form").requestSubmit();
  defer = true;
  const older = f.focusApp();
  await settle();
  history.turns[0].status = "complete";
  history.messages.push({ id: "assistant-1", turn_id: "turn-1", role: "assistant", text: "Committed reply" });
  await f.sources.at(-1).handlers.progress({ data: JSON.stringify({
    job: { id: "job-1", conversation_id: "conversation-1", turn_id: "turn-1", status: "succeeded" }, draft: "Committed reply",
  }) });
  release();
  await older;
  assert.equal(f.get("messages").children.length, 2);
  assert.equal(f.get("messages").children[1].children[1].textContent, "Committed reply");
});

test("an output limit explains the partial reply without reporting a model crash", async () => {
  const f = fixture({ conversations: [{ id: "saved", title: "Saved" }], history: {
    messages: [{ id: "user-1", turn_id: "turn-1", role: "user", text: "Explain" }],
    turns: [{ id: "turn-1", status: "failed", error_code: "output_limit" }],
    drafts: [{ turn_id: "turn-1", text: "Preserved tail" }],
  } });
  await settle();
  const cards = f.get("messages").children;
  const strings = (node) => [node.textContent, ...node.children.flatMap(strings)].join(" ");
  assert.match(strings(cards[0]), /Достигнут лимит длины ответа/);
  assert.doesNotMatch(strings(cards[0]), /Ошибка/);
  assert.equal(cards[1].children[1].textContent, "Preserved tail");
  assert.match(strings(cards[1]), /разбейте вопрос на части/);
});

test("first message creates one conversation and failed send keeps submission identity", async () => {
  const f = fixture();
  await settle();
  assert.equal(f.get("messages").children[0].className, "welcome");
  f.get("message-input").value = "Привет";
  f.failSend(true);
  await f.get("send-form").requestSubmit();
  const first = f.requests.find((r) => r.url.endsWith("/messages"));
  assert.equal(f.get("message-input").value, "Привет");
  f.failSend(false);
  await f.get("send-form").requestSubmit();
  const sends = f.requests.filter((r) => r.url.endsWith("/messages"));
  assert.equal(sends.length, 2);
  assert.equal(sends[1].payload.submission_id, first.payload.submission_id);
  assert.equal(f.requests.filter((r) => r.url === "/api/conversations" && r.method === "POST").length, 1);
  assert.equal(f.get("message-input").value, "");
});

test("new uncertain outcomes open attention panel; closing it does not acknowledge them", async () => {
  const f = fixture();
  await settle();
  f.setApprovals({ approvals: [], uncertain_actions: [{ id: "uncertain-1", name: "generate_image", target_identity: "local", arguments_json: "{}" }] });
  await f.focusApp();
  assert.equal(f.get("activity-panel").hidden, false);
  assert.equal(f.get("attention-notice").hidden, true);
  f.get("close-activity").handlers.click();
  await f.focusApp();
  assert.equal(f.get("activity-panel").hidden, true);
  assert.equal(f.get("attention-notice").hidden, false);
  assert.equal(f.get("attention-notice").textContent, "Требуют внимания: 1");
  f.get("attention-notice").handlers.click();
  assert.equal(f.get("activity-panel").hidden, false);
  assert.equal(f.get("attention-notice").hidden, true);
  assert.equal(f.requests.filter((r) => r.method === "POST").length, 0);
});

test("task status opens its controls without permanent header buttons", async () => {
  const f = fixture({ conversations: [{ id: "saved", title: "Saved" }], history: {
    messages: [{ id: "user-1", turn_id: "turn-1", role: "user", text: "Explain" }],
    turns: [{ id: "turn-1", status: "pending" }],
  } });
  await settle();
  assert.equal(f.get("refresh"), null);
  assert.equal(f.get("toggle-activity"), null);
  assert.equal(f.get("attention-notice").hidden, true);
  const status = f.get("messages").children[0].children[2];
  assert.equal(status.title, "Показать действия по запросу");
  status.handlers.click();
  assert.equal(f.get("activity-panel").hidden, false);
  f.get("close-activity").handlers.click();
  assert.equal(f.activeElement(), f.get("message-input"));
  assert.equal(f.get("attention-notice").hidden, true);
  status.handlers.click();
  f.handlers.keydown({ key: "Escape" });
  assert.equal(f.get("activity-panel").hidden, true);
  assert.equal(f.get("attention-notice").hidden, true);
  assert.equal(f.requests.filter((r) => r.method === "POST").length, 0);
});

test("Enter sends; Shift+Enter and IME composition do not", async () => {
  const f = fixture();
  await settle();
  let submitted = 0;
  f.get("send-form").requestSubmit = () => { submitted++; };
  const press = (extra) => f.get("message-input").handlers.keydown({ key: "Enter", preventDefault() {}, ...extra });
  press({ shiftKey: true });
  press({ isComposing: true });
  press({ keyCode: 229 });
  assert.equal(submitted, 0);
  press({});
  assert.equal(submitted, 1);
  f.get("send-button").disabled = true;
  press({});
  assert.equal(submitted, 1);
});

test("new conversation stays empty through refresh and gets its first message as title", async () => {
  const f = fixture();
  await settle();
  f.get("message-input").value = "First conversation";
  await f.get("send-form").requestSubmit();
  await f.get("new-conversation").handlers.click();
  await f.focusApp();
  assert.equal(f.get("conversation-title").textContent, "Новый диалог");
  assert.equal(f.requests.filter((r) => r.url === "/api/conversations" && r.method === "POST").length, 1);
  f.get("message-input").value = "Second conversation";
  await f.get("send-form").requestSubmit();
  const creates = f.requests.filter((r) => r.url === "/api/conversations" && r.method === "POST");
  assert.equal(creates.length, 2);
  assert.equal(creates[1].payload.title, "Second conversation");
});

test("pasted screenshot is resized, previewed, and submitted through vision", async () => {
  const f = fixture({ vision: true });
  await settle();
  let prevented = false;
  f.get("send-form").handlers.paste({
    clipboardData: { items: [{ kind: "file", type: "image/png", getAsFile: () => new File(["pixels"], "clip.png", { type: "image/png" }) }] },
    preventDefault() { prevented = true; },
  });
  await settle();
  assert.equal(prevented, true);
  assert.equal(f.get("screenshot-preview").hidden, false);
  assert.equal(f.get("attachment-preview").hidden, false);
  assert.equal(f.canvases[0].width, 1024);
  assert.equal(f.canvases[0].height, 576);
  assert.equal(f.requests.filter((r) => r.method === "POST").length, 0);
  f.get("message-input").value = "What is shown?";
  await f.get("send-form").requestSubmit();
  const sent = f.requests.find((r) => r.url.endsWith("/vision"));
  assert.equal(sent.payload.question, "What is shown?");
  assert.equal(sent.payload.image_base64, "cG5nLXRlc3Q=");
  assert.equal(f.get("screenshot-preview").hidden, true);
  assert.equal(f.get("attachment-preview").hidden, true);
  assert.equal(f.revoked.length, 1);
});

test("text paste is untouched and dropped JPEG can be removed before sending", async () => {
  const f = fixture({ vision: true });
  await settle();
  f.get("send-form").handlers.paste({ clipboardData: { items: [{ kind: "string", type: "text/plain" }] }, preventDefault() { throw new Error("Text paste intercepted"); } });
  f.handlers.drop({ dataTransfer: { files: [new File(["pixels"], "photo.jpg", { type: "image/jpeg" })] }, preventDefault() {} });
  await settle();
  assert.equal(f.get("screenshot-preview").hidden, false);
  f.get("remove-screenshot").handlers.click();
  assert.equal(f.get("screenshot-preview").hidden, true);
  f.get("message-input").value = "Text only";
  await f.get("send-form").requestSubmit();
  assert.equal(f.requests.some((r) => r.url.endsWith("/vision")), false);
});

test("unfinished screenshot conversion cannot attach to another conversation", async () => {
  let finish;
  const f = fixture({ vision: true, decode: () => new Promise((resolve) => { finish = resolve; }) });
  await settle();
  f.handlers.drop({ dataTransfer: { files: [new File(["pixels"], "a.png", { type: "image/png" })] }, preventDefault() {} });
  f.get("message-input").value = "Not ready";
  await f.get("send-form").requestSubmit();
  assert.equal(f.requests.filter((r) => r.method === "POST").length, 0);
  await f.get("new-conversation").handlers.click();
  finish({ width: 100, height: 100, close() {} });
  await settle();
  assert.equal(f.get("screenshot-preview").hidden, true);
});

test("image admission failure explains actual memory and keeps technical details", async () => {
  const f = fixture({ history: {
    messages: [{ id: "message-1", turn_id: "turn-1", role: "user", text: "Cat" }],
    turns: [{ id: "turn-1", status: "failed" }],
    invocations: [{ turn_id: "turn-1", name: "generate_image", status: "complete",
      result_json: JSON.stringify({ status: "error", code: "out_of_memory", memory: { resource: "ram", available_gib: 6.5, required_gib: 7 } }) }],
  } });
  await settle();
  f.get("message-input").value = "Cat";
  await f.get("send-form").requestSubmit();
  const strings = (node) => [node.textContent, ...node.children.flatMap(strings)].join(" ");
  const rendered = strings(f.get("messages"));
  assert.match(rendered, /7\.0 ГиБ свободной ОЗУ/);
  assert.match(rendered, /было 6\.5 ГиБ/);
  assert.match(rendered, /generate_image: не выполнено/);
  assert.match(rendered, /out_of_memory/);
});

test("Ctrl+Enter preference leaves Enter for newlines and preserves composition guards", async () => {
  const f = fixture({ sendShortcut: "ctrl_enter" });
  await settle();
  let submitted = 0;
  f.get("send-form").requestSubmit = () => { submitted += 1; };
  const key = f.get("message-input").handlers.keydown;
  const event = overrides => ({ key: "Enter", preventDefault() {}, ...overrides });
  key(event({}));
  key(event({ shiftKey: true, ctrlKey: true }));
  key(event({ ctrlKey: true, isComposing: true }));
  key(event({ ctrlKey: true, altKey: true }));
  assert.equal(submitted, 0);
  key(event({ ctrlKey: true }));
  assert.equal(submitted, 1);
});


test("a context overflow explains how to recover while preserving the failed turn", async () => {
  const f = fixture({ conversations: [{ id: "saved", title: "Saved" }], history: {
    messages: [{ id: "user-1", turn_id: "turn-1", role: "user", text: "Read a page" }],
    turns: [{ id: "turn-1", status: "failed", error_code: "context_limit" }],
  } });
  await settle();
  const strings = (node) => [node.textContent, ...node.children.flatMap(strings)].join(" ");
  assert.match(strings(f.get("messages").children[0]), /Не хватило места в контексте модели/);
  assert.match(strings(f.get("messages").children[0]), /уменьшите лимит ответа/);
});

test("page progress pauses without a reconnect loop and keeps explicit resume", async () => {
  const f = fixture({ history: {
    messages: [{ id: "message-1", turn_id: "turn-1", role: "user", text: "Page" }],
    turns: [{ id: "turn-1", status: "pending" }],
  } });
  await settle();
  const job = { id: "job-1", conversation_id: "conversation-1", turn_id: "turn-1", status: "running", progress: "page_read:4:12" };
  f.setJobs([job]);
  f.get("message-input").value = "Page";
  await f.get("send-form").requestSubmit();
  assert.match(f.get("messages").textContent, /Чтение страницы: 4 из 12/);
  const stream = f.sources.at(-1);
  const paused = { ...job, status: "interrupted", progress: "page_paused", error_code: "page_paused" };
  f.setJobs([paused]);
  f.get("message-input").value = "Unsent draft";
  await stream.handlers.progress({ data: JSON.stringify({ job: paused }) });
  assert.equal(stream.closed, true);
  assert.equal(f.sources.length, 1);
  assert.match(f.get("jobs").textContent, /Продолжить/);
  assert.match(f.get("messages").textContent, /сохранённого места/);
  assert.equal(f.get("message-input").value, "Unsent draft");
});

test("page coverage is honest and saved text loads only on demand as literal text", async () => {
  const f = fixture({ conversations: [{ id: "saved", title: "Page" }], history: {
    messages: [{ id: "user", turn_id: "turn", role: "user", text: "Read" }],
    turns: [{ id: "turn", status: "interrupted" }],
    invocations: [{ id: "inv", turn_id: "turn", name: "fetch_public_page", status: "complete", result_json: JSON.stringify({ status: "ok", data: { snapshot_id: "inv" } }) }],
    pages: [{ invocation_id: "inv", processed: 4, total: 12, status: "reading", input_tokens: 1000, output_tokens: 40 }],
  } });
  await settle();
  const card = f.get("messages").children[0];
  assert.match(card.textContent, /Обработано частей: 4 из 12/);
  assert.match(card.textContent, /ещё не завершён/);
  assert.doesNotMatch(card.textContent, /Все части прочитаны/);
  assert.equal(f.requests.some(r => r.url.startsWith("/api/pages/")), false);
  const details = card.children.find(node => node.tagName === "details");
  const saved = details.children.find(node => node.tagName === "details");
  saved.open = true;
  await saved.handlers.toggle();
  await saved.handlers.toggle();
  assert.equal(f.requests.filter(r => r.url.startsWith("/api/pages/")).length, 1);
  assert.equal(saved.children.at(-1).tagName, "pre");
  assert.equal(saved.children.at(-1).textContent, "<script>source is literal</script>");
});
