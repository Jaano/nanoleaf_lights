const EFFECTS = ["fade", "random", "highlight"];

function validateConfig(config) {
  if (!config || typeof config.entity !== "string" || !/^light\.[a-z0-9_]+$/.test(config.entity)) {
    throw new Error("Configure a Nanoleaf light entity, for example light.nanoleaf_bulb.");
  }
  if (config.title !== undefined && typeof config.title !== "string") {
    throw new Error("title must be a string.");
  }
  if (config.scenes_url !== undefined && typeof config.scenes_url !== "string") {
    throw new Error("scenes_url must be a same-origin URL.");
  }
  const url = new URL(config.scenes_url ?? "/local/nanoleaf-lights/scenes.json", window.location.href);
  if (url.origin !== window.location.origin || !["http:", "https:"].includes(url.protocol)) {
    throw new Error("scenes_url must use the Home Assistant origin.");
  }
  return { ...config, scenes_url: url.href };
}

class NanoleafSceneCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._scenes = [];
    this._busy = false;
    this._loading = false;
    this._catalogError = "";
    this._actionError = "";
    this._message = "";
    this._generation = 0;
    this._build();
  }

  static getStubConfig(hass) {
    return {
      entity: Object.keys(hass.states).find((entity) => entity.startsWith("light.")) ?? "light.nanoleaf_bulb",
      scenes_url: "/local/nanoleaf-lights/scenes.json",
    };
  }

  static getConfigElement() {
    return document.createElement("nanoleaf-scene-card-editor");
  }

  setConfig(config) {
    const next = validateConfig(config);
    const previous = this._config;
    this._config = next;
    if (!previous || previous.entity !== next.entity || previous.scenes_url !== next.scenes_url) {
      this._generation += 1;
      this._abort?.abort();
      this._scenes = [];
      this._selected = undefined;
      this._catalogError = "";
      this._actionError = "";
      this._message = "";
      this._busy = false;
      this._query("#search").value = "";
      this._query("#activate").checked = false;
      this._updateOptions();
      if (this.isConnected) this._loadScenes();
    }
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  connectedCallback() {
    if (this._config && !this._scenes.length && !this._loading) this._loadScenes();
  }

  disconnectedCallback() {
    this._abort?.abort();
    this._loading = false;
    this._generation += 1;
    this._busy = false;
  }

  getCardSize() {
    return 7;
  }

  _query(selector) {
    return this.shadowRoot.querySelector(selector);
  }

  _build() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { display: block; padding: 20px; color: var(--primary-text-color, #222); }
        h2 { font-size: 20px; font-weight: 500; margin: 0 0 6px; }
        p { line-height: 1.4; }
        .muted { color: var(--secondary-text-color, #666); font-size: 13px; }
        .grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
        label { display: block; font-size: 14px; margin: 16px 0; }
        input:not([type=checkbox]), select {
          box-sizing: border-box; width: 100%; margin-top: 6px; padding: 10px;
          border: 1px solid var(--divider-color, #aaa); border-radius: 6px;
          font: inherit; color: var(--primary-text-color, #222);
          background: var(--card-background-color, #fff);
        }
        .check { display: flex; align-items: center; gap: 8px; }
        button { font: inherit; padding: 10px 16px; border-radius: 6px; cursor: pointer;
          border: 1px solid var(--primary-color, #03a9f4); background: transparent;
          color: var(--primary-text-color, #222); }
        button.primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
        button:disabled { opacity: .5; cursor: not-allowed; }
        :is(input, select, button):focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
        .buttons { display: flex; flex-wrap: wrap; gap: 8px; }
        #palette { display: flex; gap: 4px; margin: 8px 0; min-height: 16px; }
        .swatch { width: 24px; height: 16px; border-radius: 3px; border: 1px solid var(--divider-color, #aaa); }
        #warning, #error { font-size: 14px; overflow-wrap: anywhere; }
        #warning { color: var(--warning-color, #8a6000); }
        #error { color: var(--error-color, #b00020); }
        [hidden] { display: none !important; }
        @media (max-width: 340px) { .grid { grid-template-columns: minmax(0, 1fr); gap: 0; } }
      </style>
      <ha-card>
        <h2 id="title">Nanoleaf Scene Upload</h2>
        <div id="device" class="muted"></div>
        <label>Search scenes
          <input id="search" type="search" placeholder="Name or UUID" autocomplete="off">
        </label>
        <label>Scene
          <select id="scene"><option value="">Search and select a scene</option></select>
        </label>
        <div id="matches" class="muted"></div>
        <div id="palette" aria-label="Scene palette"></div>
        <div class="grid">
          <label>Animation
            <select id="effect">
              <option value="fade">Fade</option>
              <option value="random">Random</option>
              <option value="highlight">Highlight</option>
            </select>
          </label>
          <label>Transition (protocol value)
            <input id="transition" type="number" min="0" max="255" step="1" value="24">
          </label>
        </div>
        <label class="check"><input id="activate" type="checkbox">Activate after upload</label>
        <p class="muted">Uploads a bulb-compatible palette animation, not the complete cloud effect.
          NL45/NL67 limit: 12 saved scenes. NL67 requires transition &gt;= 1.
          Capacity is checked by the integration.</p>
        <p id="warning" role="status" hidden></p>
        <p id="error" role="alert" hidden></p>
        <p id="status" role="status" aria-live="polite"></p>
        <div class="buttons">
          <button id="preview" type="button">Preview</button>
          <button id="upload" class="primary" type="button">Upload</button>
          <button id="reload" type="button">Reload catalogue</button>
        </div>
        <label>Stored effects
          <select id="stored"><option value="">Select a stored effect</option></select>
        </label>
        <button id="play" type="button">Play stored effect</button>
      </ha-card>`;
    this._query("#search").addEventListener("input", () => this._updateOptions());
    this._query("#scene").addEventListener("change", () => {
      this._selected = this._scenes.find((scene) => scene.uuid === this._query("#scene").value);
      this._actionError = "";
      this._message = "";
      this._updatePalette();
      this._update();
    });
    this._query("#stored").addEventListener("change", () => this._update());
    this._query("#preview").addEventListener("click", () => this._run("preview_scene"));
    this._query("#upload").addEventListener("click", () => this._run("upload_scene"));
    this._query("#play").addEventListener("click", () => this._run("play"));
    this._query("#reload").addEventListener("click", () => this._loadScenes());
    this._update();
  }

  async _loadScenes() {
    this._abort?.abort();
    const controller = new AbortController();
    this._abort = controller;
    this._loading = true;
    this._catalogError = "";
    this._update();
    try {
      const response = await fetch(this._config.scenes_url, {
        signal: controller.signal,
        credentials: "same-origin",
        mode: "same-origin",
        redirect: "error",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data.effects)) throw new Error("Catalogue must contain an effects array.");
      const ids = new Set();
      const scenes = data.effects.filter((scene) => {
        if (!scene || typeof scene.uuid !== "string" || !scene.uuid.trim()
          || typeof scene.name !== "string" || !scene.name.trim()
          || typeof scene.palette !== "string" || !/^(?:[0-9a-fA-F]{6})+$/.test(scene.palette)
          || ids.has(scene.uuid)) return false;
        ids.add(scene.uuid);
        return true;
      }).sort((a, b) => a.name.localeCompare(b.name) || a.uuid.localeCompare(b.uuid));
      if (!scenes.length) throw new Error("Catalogue contains no scenes with a valid name, UUID and RGB palette.");
      if (controller.signal.aborted) return;
      this._scenes = scenes;
      this._updateOptions();
    } catch (error) {
      if (controller.signal.aborted) return;
      this._scenes = [];
      this._catalogError = `Cannot load scene catalogue: ${error.message}`;
      this._updateOptions();
    } finally {
      if (this._abort === controller) {
        this._loading = false;
        this._update();
      }
    }
  }

  _option(value, label) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }

  _updateOptions() {
    const query = this._query("#search").value.trim().toLocaleLowerCase();
    const matches = this._scenes.filter((scene) =>
      scene.name.toLocaleLowerCase().includes(query) || scene.uuid.toLocaleLowerCase().includes(query));
    const visible = matches.slice(0, 50);
    const select = this._query("#scene");
    select.replaceChildren(this._option("", "Select a scene"),
      ...visible.map((scene) => this._option(scene.uuid, `${scene.name} (${scene.uuid})`)));
    this._selected = visible.find((scene) => scene.uuid === this._selected?.uuid);
    select.value = this._selected?.uuid ?? "";
    this._query("#matches").textContent = matches.length > 50
      ? `Showing 50 of ${matches.length} matches. Refine your search.`
      : `${matches.length} matching scenes`;
    this._updatePalette();
    this._update();
  }

  _updatePalette() {
    const palette = this._query("#palette");
    palette.replaceChildren();
    for (const color of (this._selected?.palette.match(/.{6}/g) ?? []).slice(0, 7)) {
      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.backgroundColor = `#${color}`;
      swatch.title = `#${color}`;
      palette.append(swatch);
    }
  }

  _hasAction(action) {
    return Boolean(this._hass?.services?.nanoleaf_lights?.[action]);
  }

  _update() {
    const state = this._hass?.states?.[this._config?.entity];
    const available = Boolean(state && !["unavailable", "unknown"].includes(state.state));
    this._query("#title").textContent = this._config?.title ?? "Nanoleaf Scene Upload";
    this._query("#device").textContent = state?.attributes.friendly_name ?? this._config?.entity ?? "";
    const missing = ["preview_scene", "upload_scene"].filter((action) => !this._hasAction(action));
    const warning = this._hass && missing.length
      ? `Integration actions missing: ${missing.map((action) => `nanoleaf_lights.${action}`).join(", ")}. `
        + "This frontend card cannot upload by itself; the integration backend must implement these actions."
      : "";
    this._query("#warning").textContent = warning;
    this._query("#warning").hidden = !warning;
    const error = this._actionError || this._catalogError;
    this._query("#error").textContent = error;
    this._query("#error").hidden = !error;
    this._query("#status").textContent = this._busy ? "Sending action..."
      : this._loading ? "Loading scene catalogue..."
        : !this._hass ? "Waiting for Home Assistant..."
          : !available ? "Configured light is missing or unavailable." : this._message;
    for (const selector of ["#search", "#scene", "#effect", "#transition", "#activate"]) {
      this._query(selector).disabled = this._busy || (["#search", "#scene"].includes(selector) && this._loading);
    }
    this._query("#reload").disabled = this._busy || this._loading;
    this._query("#preview").disabled = this._busy || this._loading || !available || !this._selected || !this._hasAction("preview_scene");
    this._query("#upload").disabled = this._busy || this._loading || !available || !this._selected || !this._hasAction("upload_scene");
    const stored = this._query("#stored");
    const effects = Array.isArray(state?.attributes.effect_list) ? state.attributes.effect_list.filter((effect) => typeof effect === "string") : [];
    if (JSON.stringify(effects) !== this._storedSignature) {
      const selected = stored.value;
      stored.replaceChildren(this._option("", "Select a stored effect"),
        ...effects.map((effect) => this._option(effect, effect)));
      stored.value = effects.includes(selected) ? selected : "";
      this._storedSignature = JSON.stringify(effects);
    }
    stored.disabled = this._busy || !available;
    this._query("#play").disabled = this._busy || !available || !stored.value || !this._hass?.services?.light?.turn_on;
  }

  async _run(action) {
    if (this._busy) return;
    const generation = this._generation;
    this._actionError = "";
    this._message = "";
    try {
      const state = this._hass?.states?.[this._config?.entity];
      if (!state || ["unavailable", "unknown"].includes(state.state)) throw new Error("Configured light is unavailable.");
      let domain = "nanoleaf_lights";
      let service = action;
      let data;
      if (action === "play") {
        domain = "light";
        service = "turn_on";
        const effect = this._query("#stored").value;
        if (!effect || !state.attributes.effect_list?.includes(effect)) throw new Error("Select a stored effect.");
        if (!this._hass.services?.light?.turn_on) throw new Error("light.turn_on is unavailable.");
        data = { effect };
      } else {
        if (!this._hasAction(action)) throw new Error(`Integration action nanoleaf_lights.${action} is unavailable.`);
        if (!this._selected || this._loading) throw new Error("Select a scene from the catalogue.");
        const effect = this._query("#effect").value;
        const rawTransition = this._query("#transition").value;
        const transition = Number(rawTransition);
        if (!EFFECTS.includes(effect)) throw new Error("Select a supported animation.");
        if (rawTransition === "" || !Number.isInteger(transition) || transition < 0 || transition > 255) {
          throw new Error("Transition must be an integer from 0 to 255.");
        }
        data = { scene: this._selected.uuid, effect, transition };
        if (action === "upload_scene") data.activate = this._query("#activate").checked;
      }
      this._busy = true;
      this._update();
      await this._hass.callService(domain, service, data, { entity_id: this._config.entity });
      if (generation === this._generation) {
        this._message = action === "upload_scene" ? "Upload action completed. Check the bulb's stored effects."
          : action === "preview_scene" ? "Preview action completed. The scene was not saved."
            : "Stored effect activated.";
      }
    } catch (error) {
      if (generation === this._generation) this._actionError = `Action failed: ${error.message}`;
    } finally {
      if (generation === this._generation) {
        this._busy = false;
        this._update();
      }
    }
  }
}

class NanoleafSceneCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        label { display: block; margin: 12px 0; }
        input, select { display: block; box-sizing: border-box; width: 100%; padding: 8px;
          margin-top: 4px; font: inherit; color: var(--primary-text-color, #222);
          background: var(--card-background-color, #fff); border: 1px solid var(--divider-color, #aaa); }
      </style>
      <label>Nanoleaf light<select id="entity"></select></label>
      <label>Title<input id="title" placeholder="Nanoleaf Scene Upload"></label>
      <label>Scene catalogue URL<input id="scenes_url" placeholder="/local/nanoleaf-lights/scenes.json"></label>`;
    for (const input of this.shadowRoot.querySelectorAll("input, select")) {
      input.addEventListener("change", () => {
        const config = { ...this._config };
        if (input.value) config[input.id] = input.value;
        else delete config[input.id];
        this._config = config;
        this.dispatchEvent(new CustomEvent("config-changed", {
          detail: { config }, bubbles: true, composed: true,
        }));
      });
    }
  }

  setConfig(config) {
    this._config = { ...config };
    for (const id of ["title", "scenes_url"]) {
      this.shadowRoot.getElementById(id).value = config[id] ?? "";
    }
    this._updateEntities();
  }

  set hass(hass) {
    this._hass = hass;
    this._updateEntities();
  }

  _updateEntities() {
    const select = this.shadowRoot.getElementById("entity");
    const entities = Object.keys(this._hass?.states ?? {}).filter((entity) => entity.startsWith("light."));
    if (this._config?.entity && !entities.includes(this._config.entity)) entities.push(this._config.entity);
    select.replaceChildren();
    for (const entity of ["", ...entities.sort()]) {
      const option = document.createElement("option");
      option.value = entity;
      option.textContent = entity || "Select the light from the Nanoleaf Lights integration";
      select.append(option);
    }
    select.value = this._config?.entity ?? "";
  }
}

if (!customElements.get("nanoleaf-scene-card")) {
  customElements.define("nanoleaf-scene-card", NanoleafSceneCard);
}
if (!customElements.get("nanoleaf-scene-card-editor")) {
  customElements.define("nanoleaf-scene-card-editor", NanoleafSceneCardEditor);
}
window.customCards = window.customCards ?? [];
if (!window.customCards.some((card) => card.type === "nanoleaf-scene-card")) {
  window.customCards.push({
    type: "nanoleaf-scene-card",
    name: "Nanoleaf Scene Upload",
    description: "Search, preview and upload bulb-compatible scenes. Requires integration upload actions.",
  });
}
