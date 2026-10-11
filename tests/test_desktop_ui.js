"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { randomUUID } = require("node:crypto");
const source = fs.readFileSync(path.join(__dirname, "../src/jarvis/interfaces/static/app.js"), "utf8");

function fixture({ vision = false, decode, history, loadHistory, conversations: initialConversations = [] } = {}) {
  class Element {
    constructor() { this.children = []; this.handlers = {}; this.hidden = true; this.value = ""; this.files = []; this.textContent = ""; this.style = {}; this.scrollHeight = 36; this.classList = { add() {}, remove() {} }; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    focus() { document.activeElement = this; }
    click() { this.clicks = (this.clicks || 0) + 1; this.handlers.click?.(); }
    requestSubmit() { return this.handlers.submit({ preventDefault() {} }); }
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
  let jobs = [];
  const sources = [];
  const handlers = {};
  const canvases = [];
  const revoked = [];
  const document = { getElementById: get, createElement: (tag) => {
    const el = new Element();
    if (tag === "canvas") {
      el.getContext = () => ({ drawImage() {} });
      el.toBlob = (callback) => callback(new Blob(["png-test"], { type: "image/png" }));
      canvases.push(el);
    }
    return el;
  }, createElementNS() { return new Element(); }, addEventListener(name, handler) { handlers[name] = handler; } };
  const windowHandlers = {};
  const window = {
    location: { hash: "", pathname: "/", search: "" }, addEventListener(name, handler) { windowHandlers[name] = handler; },
    getComputedStyle() { return { minHeight: "36px", maxHeight: "156px" }; },
    JarvisVoicePlayer: class { stop() {} },
  };
  vm.runInNewContext(source, {
    window, document, URLSearchParams, crypto: { randomUUID }, Blob, File,
    ResizeObserver: class { observe() {} disconnect() {} },
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
      if (url === "/api/session") value = { csrf_token: "test", speech_enabled: false, vision_enabled: vision };
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
      else if (url === "/api/approvals") value = approvals;
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
  return { get, requests, handlers, canvases, revoked, sources, focusApp() { return windowHandlers.focus(); }, activeElement() { return document.activeElement; }, failPin(value) { failPin = value; }, setJobs(value) { jobs = value; }, setApprovals(value) { approvals = value; }, failSend(value) { failSend = value; } };
}
const settle = () => new Promise(setImmediate);

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
