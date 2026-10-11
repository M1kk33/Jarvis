"use strict";

class JarvisVoicePlayer {
  constructor({ api, fetchChunk, AudioContextType, onStatus, onActive }) {
    this.api = api;
    this.fetchChunk = fetchChunk;
    this.AudioContextType = AudioContextType;
    this.onStatus = onStatus;
    this.onActive = onActive;
    this.active = null;
    this.generation = 0;
    this.volume = 0.8;
  }

  setVolume(volume) {
    if (!Number.isFinite(volume) || volume < 0 || volume > 1) return;
    this.volume = volume;
    if (this.active?.gain) this.active.gain.gain.value = volume;
  }

  isPlaying(conversationId, turnId, messageId) {
    return this.active?.conversationId === conversationId
      && this.active?.turnId === turnId && this.active?.messageId === messageId;
  }

  stop(notifyServer = true) {
    this.generation += 1;
    const active = this.active;
    this.active = null;
    this.onActive(false);
    if (!active) return;
    active.controller?.abort();
    try { active.node?.stop(); } catch (_) { /* already stopped */ }
    active.context.close().catch(() => {});
    if (notifyServer && active.sessionId) {
      this.api(`/api/spoken/${encodeURIComponent(active.sessionId)}/stop`, "POST", {}).catch(() => {});
    }
    this.onStatus("Playback stopped.");
  }

  async play(conversationId, turnId, messageId) {
    this.stop();
    if (!this.AudioContextType) { this.onStatus("Audio playback is unavailable."); return; }
    let context;
    try { context = new this.AudioContextType(); }
    catch (_) { this.onStatus("Audio playback is unavailable."); return; }
    const gain = context.createGain();
    gain.gain.value = this.volume;
    gain.connect(context.destination);
    const active = { conversationId, turnId, messageId, context, gain, sessionId: null, controller: null, node: null };
    const generation = ++this.generation;
    this.active = active;
    this.onActive(true);
    this.onStatus("Preparing voice…");
    try {
      await context.resume();
      const started = await this.api("/api/spoken/start", "POST", {
        conversation_id: conversationId, turn_id: turnId, message_id: messageId,
      });
      active.sessionId = started.session_id;
      if (this.active !== active || this.generation !== generation) {
        this.api(`/api/spoken/${encodeURIComponent(started.session_id)}/stop`, "POST", {}).catch(() => {});
        return;
      }
      if (!Number.isInteger(started.chunks) || started.chunks < 1 || started.chunks > 24) throw new Error("Invalid speech queue.");
      for (let index = 0; index < started.chunks; index += 1) {
        if (this.active !== active || this.generation !== generation) return;
        active.controller = new AbortController();
        this.onStatus(`Preparing voice ${index + 1} of ${started.chunks}…`);
        const data = await this.fetchChunk(active.sessionId, index, active.controller.signal);
        if (this.active !== active || this.generation !== generation) return;
        if (data.byteLength < 44 || data.byteLength > 1000000) throw new Error("Audio chunk is invalid.");
        const decoded = await context.decodeAudioData(data);
        if (this.active !== active || this.generation !== generation) return;
        await new Promise((resolve) => {
          const node = context.createBufferSource();
          active.node = node;
          node.buffer = decoded;
          node.connect(active.gain);
          node.onended = resolve;
          this.onStatus(`Speaking ${index + 1} of ${started.chunks}…`);
          node.start();
        });
        active.node = null;
      }
      if (this.active === active && this.generation === generation) {
        this.stop(false);
        this.onStatus("Playback finished.");
      }
    } catch (error) {
      if (this.active === active && this.generation === generation) {
        this.stop();
        this.onStatus(error.message || "Voice failed. The text is still available.");
      }
    }
  }
}

window.JarvisVoicePlayer = JarvisVoicePlayer;
