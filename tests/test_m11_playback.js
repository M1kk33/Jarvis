"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/jarvis/interfaces/static/voice_player.js"), "utf8");
const window = {};
vm.runInNewContext(source, { window, AbortController });
const Player = window.JarvisVoicePlayer;

class FakeContext {
  static instances = [];
  constructor() {
    this.destination = {};
    this.nodes = [];
    this.closed = false;
    FakeContext.instances.push(this);
  }
  async resume() {}
  createGain() { this.gain = { gain: { value: 1 }, connect() {} }; return this.gain; }
  async decodeAudioData(data) { return data; }
  createBufferSource() {
    const node = {
      stopped: false,
      connect() {},
      start() {},
      stop() { this.stopped = true; this.onended?.(); },
      finish() { this.onended?.(); },
    };
    this.nodes.push(node);
    return node;
  }
  async close() { this.closed = true; }
}

async function until(predicate) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error("Playback did not reach expected state");
}

function fixture(chunks = 2) {
  const fetched = [];
  const stopped = [];
  const statuses = [];
  const player = new Player({
    api: async (url) => {
      if (url === "/api/spoken/start") return { session_id: "session-1", chunks };
      stopped.push(url);
      return { stopped: true };
    },
    fetchChunk: async (_session, index) => {
      fetched.push(index);
      return new ArrayBuffer(64);
    },
    AudioContextType: FakeContext,
    onStatus: (status) => statuses.push(status),
    onActive: () => {},
  });
  return { player, fetched, stopped, statuses };
}

test("chunks play once, in order, then release the audio context", async () => {
  const { player, fetched, statuses } = fixture();
  const task = player.play("c", "t", "m");
  const context = FakeContext.instances.at(-1);
  await until(() => context.nodes.length === 1);
  context.nodes[0].finish();
  await until(() => context.nodes.length === 2);
  context.nodes[1].finish();
  await task;
  assert.deepEqual(fetched, [0, 1]);
  assert.equal(context.closed, true);
  assert.equal(player.active, null);
  assert.equal(statuses.at(-1), "Playback finished.");
});

test("stop during playback prevents the next chunk", async () => {
  const { player, fetched, stopped } = fixture();
  const task = player.play("c", "t", "m");
  const context = FakeContext.instances.at(-1);
  await until(() => context.nodes.length === 1);
  player.stop();
  await task;
  assert.equal(context.nodes[0].stopped, true);
  assert.deepEqual(fetched, [0]);
  assert.deepEqual(stopped, ["/api/spoken/session-1/stop"]);
});

test("a stale start response cannot resume cancelled playback", async () => {
  let completeStart;
  const fetched = [];
  const stopped = [];
  const player = new Player({
    api: (url) => url === "/api/spoken/start"
      ? new Promise((resolve) => { completeStart = resolve; })
      : (stopped.push(url), Promise.resolve({ stopped: true })),
    fetchChunk: async (_session, index) => { fetched.push(index); return new ArrayBuffer(64); },
    AudioContextType: FakeContext,
    onStatus: () => {},
    onActive: () => {},
  });
  const task = player.play("c", "t", "m");
  await until(() => !!completeStart);
  player.stop();
  completeStart({ session_id: "late-session", chunks: 1 });
  await task;
  assert.deepEqual(fetched, []);
  assert.deepEqual(stopped, ["/api/spoken/late-session/stop"]);
});

test("volume changes affect the active gain and the next playback without another synthesis", async () => {
  const { player, fetched } = fixture(1);
  player.setVolume(0.25);
  const task = player.play("c", "t", "m");
  const context = FakeContext.instances.at(-1);
  await until(() => context.nodes.length === 1);
  assert.equal(context.gain.gain.value, 0.25);
  player.setVolume(0);
  assert.equal(context.gain.gain.value, 0);
  player.setVolume(2);
  assert.equal(context.gain.gain.value, 0);
  context.nodes[0].finish();
  await task;
  assert.deepEqual(fetched, [0]);
});
