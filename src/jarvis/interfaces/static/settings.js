"use strict";

window.JarvisSettings = class {
  constructor({ api, onApply, getSelectedConversation = () => null }) {
    this.api = api;
    this.onApply = onApply;
    this.getSelectedConversation = getSelectedConversation;
    this.statisticsScope = document.getElementById("statistics-scope");
    this.statisticsScope.addEventListener("change", () => this.loadStatistics());
    document.getElementById("refresh-statistics").addEventListener("click", () => this.openStatistics());
    this.preferences = { theme: "dark", accent: "mint", text_size: 14, send_shortcut: "enter", voice_volume: 80, output_tokens: 2048 };
    this.defaults = { ...this.preferences };
    this.dialog = document.getElementById("settings-dialog");
    this.launch = document.getElementById("open-settings");
    this.status = document.getElementById("settings-status");
    this.saveButton = document.getElementById("save-settings");
    this.fields = document.getElementById("settings-fields");
    this.resetButton = document.getElementById("reset-settings");
    this.controls = Object.fromEntries(Object.keys(this.preferences).map(key => [key, document.getElementById(`setting-${key.replaceAll("_", "-")}`)]));
    this.systemTheme = window.matchMedia("(prefers-color-scheme: light)");
    this.systemTheme.addEventListener("change", () => this.apply());
    this.launch.addEventListener("click", () => this.open());
    document.getElementById("close-settings").addEventListener("click", () => this.close());
    this.dialog.addEventListener("cancel", event => { event.preventDefault(); this.close(); });
    this.dialog.addEventListener("close", () => this.launch.focus());
    document.getElementById("reset-settings").addEventListener("click", () => { if (this.saving) return; this.fill(this.defaults); this.status.textContent = "Нажмите «Сохранить», чтобы применить."; });
    document.getElementById("settings-form").addEventListener("submit", event => { event.preventDefault(); this.save(); });
    this.controls.voice_volume.addEventListener("input", () => this.updateVolumeLabel());
    this.tabs = [...document.querySelectorAll("[data-settings-tab]")];
    this.pages = [...document.querySelectorAll("[data-settings-page]")];
    for (const tab of this.tabs) tab.addEventListener("click", () => {
      if (this.saving) return;
      for (const item of this.tabs) item.setAttribute("aria-pressed", String(item === tab));
      for (const page of this.pages) page.hidden = page.dataset.settingsPage !== tab.dataset.settingsTab;
      this.statisticsActive = tab.dataset.settingsTab === "statistics";
      this.saveButton.hidden = this.resetButton.hidden = this.status.hidden = this.statisticsActive;
      document.getElementById("settings-footer").hidden = this.statisticsActive;
      if (this.statisticsActive) this.openStatistics();
      else this.statisticsGeneration = (this.statisticsGeneration || 0) + 1;
    });
  }

  async initialize(session) {
    if (!session.settings_enabled) return;
    this.statisticsEnabled = Boolean(session.statistics_enabled);
    document.getElementById("statistics-tab").hidden = !this.statisticsEnabled;
    this.controls.voice_volume.disabled = !session.spoken_enabled;
    document.getElementById("setting-voice-availability").textContent = session.spoken_enabled ? "Голосовой модуль подключён." : "Голосовой модуль не подключён в текущем запуске.";
    try {
      this.accept(await this.api("/api/settings"));
      this.launch.hidden = false;
    } catch (error) {
      // Keep access to a retry and a visible error even if the initial read failed.
      this.status.textContent = error.message;
      this.launch.hidden = false;
      this.loaded = false;
    }
  }

  accept(result) {
    this.preferences = { ...result.preferences };
    this.loaded = true;
    document.getElementById("setting-model-name").textContent = result.model_name;
    document.getElementById("setting-context").textContent = `${result.context_tokens.toLocaleString("ru-RU")} токенов`;
    document.getElementById("setting-active-output").textContent = `Сейчас действует: ${result.active_output_tokens.toLocaleString("ru-RU")} токенов.`;
    this.restartRequired = result.restart_required;
    this.apply();
  }

  apply() {
    const p = this.preferences;
    const root = document.documentElement;
    root.dataset.theme = p.theme === "system" ? (this.systemTheme.matches ? "light" : "dark") : p.theme;
    root.dataset.accent = p.accent;
    root.style.setProperty("--chat-text-size", `${p.text_size}px`);
    this.onApply(p);
  }

  updateVolumeLabel() {
    document.getElementById("setting-volume-value").textContent = `${this.controls.voice_volume.value}%`;
  }

  fill(preferences) {
    for (const [key, control] of Object.entries(this.controls)) control.value = String(preferences[key]);
    this.updateVolumeLabel();
  }

  async open() {
    if (this.dialog.open) return;
    this.fill(this.preferences);
    this.status.textContent = this.restartRequired ? "Изменение лимита ожидает перезапуска." : "";
    this.dialog.showModal();
    if (this.statisticsActive) this.openStatistics();
    const generation = this.readGeneration = (this.readGeneration || 0) + 1;
    this.saveButton.disabled = true;
    this.fields.disabled = true;
    this.resetButton.disabled = true;
    try {
      const result = await this.api("/api/settings");
      if (generation !== this.readGeneration) return;
      this.accept(result);
      this.fill(this.preferences);
      this.status.textContent = this.restartRequired ? "Изменение лимита ожидает перезапуска." : "";
    } catch (error) { if (generation === this.readGeneration) this.status.textContent = error.message; }
    finally {
      if (generation === this.readGeneration) {
        this.saveButton.disabled = !this.loaded;
        this.fields.disabled = false;
        this.resetButton.disabled = false;
      }
    }
  }

  close() {
    if (!this.saving) { this.readGeneration = (this.readGeneration || 0) + 1; this.statisticsGeneration = (this.statisticsGeneration || 0) + 1; this.dialog.close(); }
  }

  async openStatistics() {
    if (!this.statisticsEnabled || !this.dialog.open || !this.statisticsActive) return;
    const generation = this.statisticsGeneration = (this.statisticsGeneration || 0) + 1;
    const status = document.getElementById("statistics-status");
    status.textContent = "Загружаю…";
    document.getElementById("statistics-results").hidden = true;
    try {
      const { conversations } = await this.api("/api/conversations");
      if (generation !== this.statisticsGeneration) return;
      const previous = this.statisticsScope.value;
      const current = this.getSelectedConversation();
      const option = (value, text) => { const node = document.createElement("option"); node.value = value; node.textContent = text; return node; };
      const ordered = [...conversations].sort((a, b) => Number(b.id === current) - Number(a.id === current));
      this.statisticsScope.replaceChildren(option("", "Все диалоги"), ...ordered.map(chat => option(chat.id, chat.title + (chat.id === current ? " · текущий" : ""))));
      this.statisticsScope.value = conversations.some(chat => chat.id === previous) ? previous : "";
      await this.loadStatistics();
    } catch (error) { if (generation === this.statisticsGeneration) status.textContent = error.message; }
  }

  async loadStatistics() {
    if (!this.statisticsEnabled || !this.dialog.open || !this.statisticsActive) return;
    const generation = this.statisticsGeneration = (this.statisticsGeneration || 0) + 1;
    const status = document.getElementById("statistics-status");
    const results = document.getElementById("statistics-results");
    status.textContent = "Загружаю…";
    results.hidden = true;
    try {
      const scope = this.statisticsScope.value;
      const data = await this.api("/api/statistics" + (scope ? `?conversation_id=${encodeURIComponent(scope)}` : ""));
      if (generation !== this.statisticsGeneration) return;
      const number = value => value.toLocaleString("ru-RU");
      const put = (id, text) => { document.getElementById(`statistics-${id}`).textContent = text; };
      for (const key of ["conversations", "user_messages", "assistant_messages", "regenerations", "generated_images", "failed_turns", "interrupted_turns", "pending_turns"]) put(key.replaceAll("_", "-"), number(data[key]));
      for (const [key, total] of [["input_tokens", data.user_messages], ["output_tokens", data.assistant_messages]]) put(key.replaceAll("_", "-"), data[key] === null ? (total === 0 ? "0" : "Нет данных") : number(data[key]));
      const missing = data.counted_user_messages < data.user_messages || data.counted_assistant_messages < data.assistant_messages;
      put("coverage", `Токены учтены для ${number(data.counted_user_messages)} из ${number(data.user_messages)} сообщений и ${number(data.counted_assistant_messages)} из ${number(data.assistant_messages)} ответов.` + (missing ? " Для остальных нет данных о токенах." : ""));
      const date = value => value ? new Date(value).toLocaleString("ru-RU", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
      put("first-message", date(data.first_message_at));
      put("last-message", date(data.last_message_at));
      put("unfinished", number(data.failed_turns + data.interrupted_turns + data.pending_turns));
      document.getElementById("statistics-empty").hidden = data.user_messages !== 0;
      status.textContent = "За всё время · обновлено " + new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
      results.hidden = false;
    } catch (error) { if (generation === this.statisticsGeneration) status.textContent = error.message; }
  }

  async save() {
    if (this.saving || !this.loaded || this.statisticsActive) return;
    const preferences = Object.fromEntries(Object.entries(this.controls).map(([key, control]) => [key, ["text_size", "voice_volume", "output_tokens"].includes(key) ? Number(control.value) : control.value]));
    this.saving = true;
    this.saveButton.disabled = true;
    this.fields.disabled = true;
    this.resetButton.disabled = true;
    this.status.textContent = "Сохраняю…";
    try {
      this.accept(await this.api("/api/settings", "POST", preferences));
      this.status.textContent = this.restartRequired ? "Сохранено. Перезапустите Jarvis для изменения лимита." : "Сохранено";
    } catch (error) { this.status.textContent = error.message; }
    finally { this.saving = false; this.saveButton.disabled = false; this.fields.disabled = false; this.resetButton.disabled = false; }
  }
};
