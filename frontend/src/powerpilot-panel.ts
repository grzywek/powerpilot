import { LitElement, html, css, svg, nothing, type TemplateResult } from "lit";
import { customElement, property, state } from "lit/decorators.js";

interface PlanHour {
  start: string;
  inverter_mode: string;
  charge_power: string;
  grid_connected: boolean;
  ev_charge: boolean;
  ev_charge_kwh: number;
  battery_soc: number;
  battery_energy_cost: number;
  battery_charge_kwh: number;
  battery_discharge_kwh: number;
  hour_cost: number;
}

interface ForecastHour {
  start: string;
  buy_price: number | null;
  sell_price: number | null;
  price_confirmed: boolean;
  consumption_kwh: number;
  temperature: number | null;
}

interface Plan {
  created_at: string | null;
  total_cost: number;
  hours: PlanHour[];
  forecast: ForecastHour[];
}

interface Status {
  last_update: string | null;
  horizon_hours: number;
  price_profile_days: number;
  consumption_days: number;
  consumption_devices: string[];
  ev_enabled: boolean;
  modules: { domain: string; error: string | null }[];
  checks: { key: string; label: string; ok: boolean }[];
}

interface LogEvent {
  time: string;
  horizon_hours: number;
  action: string | null;
  ev_charge: boolean | null;
  battery_soc: number | null;
  errors: string[];
}

type Tab = "overview" | "status" | "logs";

@customElement("powerpilot-panel")
export class PowerPilotPanel extends LitElement {
  @property({ attribute: false }) hass: any;
  @property({ attribute: false }) narrow = false;

  @state() private _tab: Tab = "overview";
  @state() private _plan: Plan | null = null;
  @state() private _status: Status | null = null;
  @state() private _log: LogEvent[] = [];
  @state() private _error: string | null = null;

  private _timer?: number;

  connectedCallback(): void {
    super.connectedCallback();
    this._refresh();
    this._timer = window.setInterval(() => this._refresh(), 60000);
  }

  disconnectedCallback(): void {
    if (this._timer) window.clearInterval(this._timer);
    super.disconnectedCallback();
  }

  private async _refresh(): Promise<void> {
    if (!this.hass) return;
    try {
      const [plan, status, log] = await Promise.all([
        this.hass.callWS({ type: "powerpilot/plan" }),
        this.hass.callWS({ type: "powerpilot/status" }),
        this.hass.callWS({ type: "powerpilot/log" }),
      ]);
      this._plan = plan;
      this._status = status;
      this._log = log?.events ?? [];
      this._error = null;
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    }
  }

  private _openConfig(): void {
    window.location.assign("/config/integrations/integration/powerpilot");
  }

  render(): TemplateResult {
    return html`
      <div class="header">
        <div class="title">PowerPilot</div>
        <div class="spacer"></div>
        <button class="cfg" @click=${this._openConfig}>⚙ Konfiguracja</button>
      </div>
      <div class="tabs">
        ${this._tabButton("overview", "Przegląd")}
        ${this._tabButton("status", "Status")}
        ${this._tabButton("logs", "Logi")}
      </div>
      ${this._error ? html`<div class="error">Błąd: ${this._error}</div>` : nothing}
      <div class="content">
        ${this._tab === "overview" ? this._renderOverview() : nothing}
        ${this._tab === "status" ? this._renderStatus() : nothing}
        ${this._tab === "logs" ? this._renderLogs() : nothing}
      </div>
    `;
  }

  private _tabButton(tab: Tab, label: string): TemplateResult {
    return html`<button
      class=${"tab" + (this._tab === tab ? " active" : "")}
      @click=${() => (this._tab = tab)}
    >
      ${label}
    </button>`;
  }

  // ------------------------------------------------------------------
  // Overview
  // ------------------------------------------------------------------
  private _renderOverview(): TemplateResult {
    const plan = this._plan;
    if (!plan || !plan.hours?.length) {
      return html`<div class="card empty">Brak danych planu. Poczekaj na pierwsze przeliczenie.</div>`;
    }
    const current = plan.hours[0];
    return html`
      <div class="card">
        <div class="stat-row">
          ${this._stat("Tryb falownika", current.inverter_mode)}
          ${this._stat("Moc", current.charge_power)}
          ${this._stat("SoC", current.battery_soc.toFixed(0) + " %")}
          ${this._stat("Cena w baterii", current.battery_energy_cost.toFixed(3))}
          ${this._stat("Sieć", current.grid_connected ? "tak" : "nie")}
          ${this._stat("EV", current.ev_charge ? "ładuje" : "—")}
          ${this._stat("Koszt horyzontu", plan.total_cost.toFixed(2) + " PLN")}
        </div>
      </div>
      <div class="card">
        <div class="card-title">Bateria (SoC %) i zużycie</div>
        ${this._socChart(plan)}
      </div>
      <div class="card">
        <div class="card-title">Ceny (PLN/kWh) — zakup, sprzedaż, cena w baterii</div>
        ${this._priceChart(plan)}
      </div>
    `;
  }

  private _stat(label: string, value: string): TemplateResult {
    return html`<div class="stat"><span class="k">${label}</span><span class="v">${value}</span></div>`;
  }

  private _socChart(plan: Plan): TemplateResult {
    const soc = plan.hours.map((h) => h.battery_soc);
    const cons = plan.forecast.map((f) => f.consumption_kwh);
    const w = 760;
    const h = 180;
    const socPath = this._linePath(soc, 0, 100, w, h);
    const maxC = Math.max(0.1, ...cons);
    return svg`
      <svg viewBox="0 0 ${w} ${h}" class="chart">
        ${this._bars(cons, 0, maxC, w, h, "var(--error-color, #b5475d)")}
        <path d=${socPath} fill="none" stroke="var(--primary-color, #2ec4b6)" stroke-width="2" />
      </svg>`;
  }

  private _priceChart(plan: Plan): TemplateResult {
    const buy = plan.forecast.map((f) => f.buy_price ?? NaN);
    const sell = plan.forecast.map((f) => f.sell_price ?? NaN);
    const cost = plan.hours.map((h) => h.battery_energy_cost);
    const all = [...buy, ...sell, ...cost].filter((v) => !isNaN(v));
    const min = Math.min(0, ...all);
    const max = Math.max(0.1, ...all);
    const w = 760;
    const h = 180;
    return svg`
      <svg viewBox="0 0 ${w} ${h}" class="chart">
        <path d=${this._linePath(buy, min, max, w, h)} fill="none" stroke="var(--primary-color, #2ec4b6)" stroke-width="2" />
        <path d=${this._linePath(sell, min, max, w, h)} fill="none" stroke="#7b6cf6" stroke-width="2" />
        <path d=${this._linePath(cost, min, max, w, h)} fill="none" stroke="var(--secondary-text-color, #9e9e9e)" stroke-width="2" stroke-dasharray="4 3" />
      </svg>`;
  }

  private _linePath(values: number[], min: number, max: number, w: number, h: number): string {
    const n = values.length;
    if (n < 2) return "";
    const span = max - min || 1;
    const pad = 6;
    const innerH = h - pad * 2;
    let d = "";
    let started = false;
    values.forEach((v, i) => {
      if (isNaN(v)) {
        started = false;
        return;
      }
      const x = (i / (n - 1)) * w;
      const y = pad + innerH - ((v - min) / span) * innerH;
      d += `${started ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)} `;
      started = true;
    });
    return d.trim();
  }

  private _bars(values: number[], min: number, max: number, w: number, h: number, color: string) {
    const n = values.length;
    if (!n) return nothing;
    const span = max - min || 1;
    const pad = 6;
    const innerH = h - pad * 2;
    const bw = (w / n) * 0.7;
    return values.map((v, i) => {
      const x = (i / n) * w;
      const bh = ((v - min) / span) * innerH;
      return svg`<rect x=${x.toFixed(1)} y=${(pad + innerH - bh).toFixed(1)} width=${bw.toFixed(1)} height=${Math.max(0, bh).toFixed(1)} fill=${color} opacity="0.35" />`;
    });
  }

  // ------------------------------------------------------------------
  // Status
  // ------------------------------------------------------------------
  private _renderStatus(): TemplateResult {
    const s = this._status;
    if (!s) return html`<div class="card empty">Brak statusu.</div>`;
    return html`
      <div class="card">
        <div class="card-title">Co działa / czego brakuje</div>
        ${s.checks.map(
          (c) => html`<div class="check">
            <span class=${"dot " + (c.ok ? "ok" : "bad")}></span>${c.label}
            <span class="muted">${c.ok ? "OK" : "brak konfiguracji"}</span>
          </div>`
        )}
      </div>
      <div class="card">
        <div class="card-title">Uczenie</div>
        <div class="check">Profil cen: <b>${s.price_profile_days}</b> dni</div>
        <div class="check">Profil zużycia: <b>${s.consumption_days}</b> dni</div>
        <div class="check">
          Urządzenia rozdzielone:
          <b>${s.consumption_devices.length ? s.consumption_devices.join(", ") : "brak"}</b>
        </div>
        <div class="check">EV: <b>${s.ev_enabled ? "włączone" : "wyłączone"}</b></div>
        <div class="check">Horyzont planu: <b>${s.horizon_hours}</b> h</div>
      </div>
      <div class="card">
        <div class="card-title">Moduły</div>
        ${s.modules.map(
          (m) => html`<div class="check">
            <span class=${"dot " + (m.error ? "bad" : "ok")}></span>${m.domain}
            ${m.error ? html`<span class="muted">${m.error}</span>` : nothing}
          </div>`
        )}
      </div>
    `;
  }

  // ------------------------------------------------------------------
  // Logs
  // ------------------------------------------------------------------
  private _renderLogs(): TemplateResult {
    if (!this._log.length) return html`<div class="card empty">Brak zdarzeń.</div>`;
    return html`<div class="card">
      <div class="card-title">Ostatnie przeliczenia</div>
      <table class="log">
        <thead>
          <tr><th>Czas</th><th>Akcja</th><th>SoC</th><th>EV</th><th>Horyzont</th><th>Błędy</th></tr>
        </thead>
        <tbody>
          ${this._log.map(
            (e) => html`<tr>
              <td>${this._time(e.time)}</td>
              <td>${e.action ?? "—"}</td>
              <td>${e.battery_soc ?? "—"}</td>
              <td>${e.ev_charge ? "tak" : "—"}</td>
              <td>${e.horizon_hours} h</td>
              <td class=${e.errors.length ? "err" : ""}>${e.errors.join("; ") || "—"}</td>
            </tr>`
          )}
        </tbody>
      </table>
    </div>`;
  }

  private _time(iso: string): string {
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  }

  static styles = css`
    :host {
      display: block;
      padding: 16px;
      color: var(--primary-text-color);
      background: var(--primary-background-color);
      min-height: 100vh;
      box-sizing: border-box;
    }
    .header {
      display: flex;
      align-items: center;
      margin-bottom: 12px;
    }
    .title {
      font-size: 22px;
      font-weight: 600;
    }
    .spacer {
      flex: 1;
    }
    .cfg {
      cursor: pointer;
      border: 1px solid var(--divider-color);
      background: var(--card-background-color);
      color: var(--primary-text-color);
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 14px;
    }
    .tabs {
      display: flex;
      gap: 8px;
      margin-bottom: 12px;
    }
    .tab {
      cursor: pointer;
      border: none;
      background: var(--card-background-color);
      color: var(--secondary-text-color);
      border-radius: 8px;
      padding: 8px 14px;
      font-size: 14px;
    }
    .tab.active {
      color: var(--text-primary-color, #fff);
      background: var(--primary-color);
    }
    .content {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .card {
      background: var(--card-background-color, #1c1c1c);
      border-radius: 12px;
      padding: 16px;
      box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0, 0, 0, 0.2));
    }
    .card-title {
      font-weight: 600;
      margin-bottom: 10px;
    }
    .empty {
      color: var(--secondary-text-color);
    }
    .error {
      color: var(--error-color, #d33);
      margin-bottom: 12px;
    }
    .chart {
      width: 100%;
      height: auto;
      display: block;
    }
    .stat-row {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
    }
    .stat {
      display: flex;
      flex-direction: column;
    }
    .stat .k {
      font-size: 12px;
      color: var(--secondary-text-color);
    }
    .stat .v {
      font-size: 18px;
      font-weight: 600;
    }
    .check {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 4px 0;
    }
    .muted {
      color: var(--secondary-text-color);
      font-size: 13px;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
    }
    .dot.ok {
      background: var(--success-color, #43a047);
    }
    .dot.bad {
      background: var(--error-color, #d33);
    }
    table.log {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    table.log th,
    table.log td {
      text-align: left;
      padding: 6px 8px;
      border-bottom: 1px solid var(--divider-color);
    }
    td.err {
      color: var(--error-color, #d33);
    }
  `;
}
