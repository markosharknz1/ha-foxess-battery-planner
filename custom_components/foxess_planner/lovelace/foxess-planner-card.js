/*
 * FoxESS Battery Planner card
 * ---------------------------
 * Native Lovelace card for the foxess_planner integration. Finds each
 * planner from HA's entity registry (platform === "foxess_planner"), so the
 * common case needs no configuration:
 *
 *   type: custom:foxess-planner-card
 *
 * Optional:
 *   planners: [fox]      # limit to specific entity prefixes (slugs)
 *   title: Home battery  # heading above the card
 *
 * Writes go through HA's own button / number / switch services as the
 * logged-in user. Everything shown comes from the integration's entities;
 * nothing is polled by the card.
 */

const SETTINGS = [
  ["battery_capacity", "Battery capacity"],
  ["daily_usage", "Daytime usage"],
  ["pre_solar_load", "Pre-solar load"],
  ["solar_trust", "Solar forecast trust"],
  ["buffer", "Target buffer"],
  ["min_target_soc", "Minimum SoC (always charge to)"],
  ["max_target_soc", "Maximum target"],
  ["charge_power_kw", "Grid charge rate"],
];

const STATUS_LABEL = {
  idle: "Idle",
  disabled: "Planning off",
  not_needed: "Charge not needed",
  charging: "Charging from grid",
  complete: "Complete",
  dry_run: "Dry run",
  skipped: "Skipped",
  error: "Error",
};
const STATUS_COLOR = {
  charging: "var(--info-color, #039be5)",
  complete: "var(--success-color, #43a047)",
  not_needed: "var(--success-color, #43a047)",
  dry_run: "var(--warning-color, #ffa726)",
  skipped: "var(--warning-color, #ffa726)",
  error: "var(--error-color, #db4437)",
  disabled: "var(--disabled-text-color, #9e9e9e)",
  idle: "var(--secondary-text-color)",
};

class FoxessPlannerCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._open = this._open || {};
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 5;
  }

  static getStubConfig() {
    return {};
  }

  _slugs() {
    const wanted = this._config.planners;
    const ents = this._hass.entities || {};
    const slugs = [];
    for (const [id, e] of Object.entries(ents)) {
      if (e.platform !== "foxess_planner") continue;
      const m = id.match(/^sensor\.(.+)_status$/);
      if (m && (!wanted || wanted.includes(m[1]))) slugs.push(m[1]);
    }
    return slugs.sort();
  }

  _state(domain, slug, key) {
    return this._hass.states[`${domain}.${slug}_${key}`];
  }

  _call(domain, service, entity_id, data = {}) {
    this._hass.callService(domain, service, { entity_id, ...data });
  }

  _render() {
    if (!this._hass || !this.shadowRoot) return;
    // Don't rebuild while the user is dragging a slider.
    if (this._dragging) return;
    const slugs = this._slugs();
    const body = slugs.length
      ? slugs.map((s) => this._renderPlanner(s)).join("")
      : `<div class="empty">No FoxESS Battery Planner found. Add the integration first.</div>`;
    this.shadowRoot.innerHTML = `${this._style()}<ha-card>${
      this._config.title ? `<div class="title">${this._config.title}</div>` : ""
    }${body}</ha-card>`;
    this._wire();
  }

  _renderPlanner(slug) {
    const status = this._state("sensor", slug, "status");
    if (!status) return "";
    const a = status.attributes;
    const st = status.state;
    const soc = a.battery_soc;
    const target = a.target_soc;
    const forecast = this._state("sensor", slug, "forecast");
    const grid = this._state("sensor", slug, "grid_needed");
    const num = (v, d = 1) => (v === undefined || v === null || isNaN(Number(v)) ? "–" : Number(v).toFixed(d));
    const socPct = Math.max(0, Math.min(100, Number(soc) || 0));
    const tgtPct = target === null || target === undefined ? null : Math.max(0, Math.min(100, Number(target)));
    const enabled = this._state("switch", slug, "enabled");
    const dry = this._state("switch", slug, "dry_run");
    const fcAttr = (forecast && forecast.attributes) || {};
    const warn = fcAttr.missing
      ? "No solar forecast readable: planning as if there is no sun."
      : fcAttr.fallback
        ? `Using ${fcAttr.source}.`
        : fcAttr.sources_unavailable
          ? `${fcAttr.sources_unavailable} forecast sensor(s) unavailable; forecast is under-counted.`
          : "";
    const tz = a.time_zone_check || {};
    const tzWarn =
      tz.status === "mismatch"
        ? `Time zone mismatch: FoxESS site is ${tz.site_tz}, Home Assistant is ${tz.ha_tz}. Charging is blocked until fixed.`
        : tz.status === "unknown"
          ? `Could not confirm the inverter time zone (${tz.detail || "unknown"}). Check it in the FoxESS app.`
          : "";
    const open = !!this._open[slug];

    return `
    <div class="planner" data-slug="${slug}">
      <div class="head">
        <div class="name">${status.attributes.friendly_name ? status.attributes.friendly_name.replace(/ Status$/, "") : slug}</div>
        <div class="chip" style="background:${STATUS_COLOR[st] || STATUS_COLOR.idle}">${STATUS_LABEL[st] || st}</div>
      </div>
      <div class="msg">${a.message || ""}</div>
      ${a.dry_run ? `<div class="banner">Dry run is ON: plans are calculated but nothing is sent to FoxESS.</div>` : ""}
      ${st === "error" && a.last_error ? `<div class="banner err">${a.last_error}</div>` : ""}
      ${warn ? `<div class="banner">${warn}</div>` : ""}
      ${tzWarn ? `<div class="banner ${tz.status === "mismatch" ? "err" : ""}">${tzWarn}</div>` : ""}
      <div class="bar" title="Battery ${num(soc, 0)}%${tgtPct !== null ? `, target ${tgtPct}%` : ""}">
        <div class="fill" style="width:${socPct}%"></div>
        ${tgtPct !== null ? `<div class="target" style="left:${tgtPct}%"></div>` : ""}
      </div>
      <div class="stats">
        <div><span>Battery</span><b>${num(soc, 0)}%</b></div>
        <div><span>Target</span><b>${tgtPct !== null ? tgtPct + "%" : "–"}</b></div>
        <div><span>Forecast</span><b>${num(forecast && forecast.state)} kWh</b></div>
        <div><span>From grid</span><b>${num(grid && grid.state)} kWh</b></div>
        <div><span>Window${(a.charge_window || "").includes(",") ? "s" : ""}</span><b>${a.charge_window || "–"}</b></div>
        <div><span>Charge rate</span><b>${a.charge_power_kw ? a.charge_power_kw + " kW" : "inverter default"}</b></div>
      </div>
      <div class="actions">
        <button data-act="plan" data-slug="${slug}">Plan now</button>
        <button data-act="cancel" data-slug="${slug}" ${a.charging ? "" : "disabled"}>Cancel charge</button>
      </div>
      <div class="toggles">
        <label><input type="checkbox" data-act="toggle" data-key="enabled" data-slug="${slug}" ${enabled && enabled.state === "on" ? "checked" : ""}/> Automatic planning</label>
        <label><input type="checkbox" data-act="toggle" data-key="dry_run" data-slug="${slug}" ${dry && dry.state === "on" ? "checked" : ""}/> Dry run</label>
      </div>
      <details data-slug="${slug}" ${open ? "open" : ""}>
        <summary>Settings</summary>
        ${SETTINGS.map(([key, label]) => this._renderSetting(slug, key, label)).join("")}
      </details>
    </div>`;
  }

  _renderSetting(slug, key, label) {
    const s = this._state("number", slug, key);
    if (!s) return "";
    const at = s.attributes;
    return `<div class="set">
      <span>${label}</span>
      <input type="range" min="${at.min}" max="${at.max}" step="${at.step}" value="${s.state}"
        data-act="num" data-slug="${slug}" data-key="${key}" />
      <b>${Number(s.state)} ${at.unit_of_measurement || ""}</b>
    </div>`;
  }

  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll("button[data-act]").forEach((b) =>
      b.addEventListener("click", () => {
        const slug = b.dataset.slug;
        if (b.dataset.act === "plan") this._call("button", "press", `button.${slug}_plan_now`);
        if (b.dataset.act === "cancel") this._call("button", "press", `button.${slug}_cancel`);
      })
    );
    root.querySelectorAll("input[data-act=toggle]").forEach((i) =>
      i.addEventListener("change", () =>
        this._call("switch", i.checked ? "turn_on" : "turn_off", `switch.${i.dataset.slug}_${i.dataset.key}`)
      )
    );
    root.querySelectorAll("input[data-act=num]").forEach((i) => {
      i.addEventListener("input", () => {
        this._dragging = true;
        i.nextElementSibling.textContent = i.value;
      });
      i.addEventListener("change", () => {
        this._dragging = false;
        this._call("number", "set_value", `number.${i.dataset.slug}_${i.dataset.key}`, { value: Number(i.value) });
      });
    });
    root.querySelectorAll("details").forEach((d) =>
      d.addEventListener("toggle", () => {
        this._open[d.dataset.slug] = d.open;
      })
    );
  }

  _style() {
    return `<style>
      .title { padding: 16px 16px 0; font-size: 1.2em; font-weight: 500; }
      .planner { padding: 16px; }
      .planner + .planner { border-top: 1px solid var(--divider-color); }
      .head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
      .name { font-size: 1.1em; font-weight: 500; }
      .chip { color: #fff; border-radius: 12px; padding: 2px 10px; font-size: .8em; white-space: nowrap; }
      .msg { color: var(--secondary-text-color); margin: 4px 0 8px; font-size: .9em; }
      .banner { background: var(--secondary-background-color); border-left: 3px solid var(--warning-color, #ffa726);
        padding: 6px 10px; margin: 6px 0; font-size: .85em; border-radius: 4px; }
      .banner.err { border-left-color: var(--error-color, #db4437); }
      .bar { position: relative; height: 14px; border-radius: 7px; background: var(--secondary-background-color); margin: 10px 0; overflow: visible; }
      .fill { height: 100%; border-radius: 7px; background: var(--primary-color); }
      .target { position: absolute; top: -4px; width: 3px; height: 22px; background: var(--primary-text-color); border-radius: 2px; transform: translateX(-1px); }
      .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(80px, 1fr)); gap: 8px; margin: 8px 0; }
      .stats div { display: flex; flex-direction: column; }
      .stats span { font-size: .75em; color: var(--secondary-text-color); }
      .actions { display: flex; gap: 8px; margin: 10px 0 4px; }
      button { flex: 1; padding: 8px; border-radius: 8px; border: 1px solid var(--divider-color);
        background: var(--card-background-color); color: var(--primary-text-color); cursor: pointer; font: inherit; }
      button:hover:not([disabled]) { background: var(--secondary-background-color); }
      button[disabled] { opacity: .45; cursor: default; }
      .toggles { display: flex; gap: 16px; flex-wrap: wrap; margin: 8px 0; font-size: .9em; }
      details { margin-top: 8px; }
      summary { cursor: pointer; color: var(--secondary-text-color); }
      .set { display: grid; grid-template-columns: 1fr 1.4fr auto; gap: 8px; align-items: center; padding: 4px 0; font-size: .9em; }
      .set input { width: 100%; }
      .set b { min-width: 64px; text-align: right; }
      .empty { padding: 16px; color: var(--secondary-text-color); }
    </style>`;
  }
}

customElements.define("foxess-planner-card", FoxessPlannerCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "foxess-planner-card",
  name: "FoxESS Battery Planner",
  description: "Overnight grid-charge plan, status and settings for the FoxESS Battery Planner integration.",
});
