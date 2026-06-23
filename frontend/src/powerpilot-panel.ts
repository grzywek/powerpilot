import { LitElement, html, css, svg, nothing, type TemplateResult } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import ApexCharts from "apexcharts";

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
  type?: "plan" | "info" | "warning";
  module?: string;
  message?: string;
  extra?: Record<string, unknown>;
  // legacy / plan-event fields
  horizon_hours?: number;
  action?: string | null;
  ev_charge?: boolean | null;
  battery_soc?: number | null;
  errors?: string[];
}

type Matrix = Record<string, (number | null)[]>;

interface Profiles {
  price: Matrix;
  price_days: number;
  consumption: Matrix;
  consumption_days: number;
  devices: Record<string, Matrix>;
}

interface ForecastPoint {
  hour: number;
  buy: number | null;
  p10: number | null;
  p90: number | null;
}

interface Forecasts {
  date: string;
  horizons: Record<string, ForecastPoint[]>;
}

interface SeriesHour {
  start: string;
  is_past: boolean;
  buy_price: number | null;
  distribution_price_kwh: number | null;
  total_price_kwh: number | null;
  price_confirmed: boolean;
  consumption_real: number | null;
  consumption_forecast: number | null;
  base_consumption_forecast: number | null;
  soc: number | null;
  battery_soc_start: number | null;
  inverter_mode: string | null;
  battery_charge_kwh: number | null;
  battery_discharge_kwh: number | null;
  battery_energy_cost: number | null;
  grid_buy_kwh: number | null;
  ev_charge_kwh: number | null;
  hour_cost: number | null;
  energy_cost: number | null;
  distribution_cost: number | null;
  battery_use_cost: number | null;
  devices_real: Record<string, number | null>;
  devices_forecast: Record<string, number | null>;
}

interface Series {
  now: string;
  past_hours: number;
  start: string;
  end: string;
  device_ids: string[];
  hours: SeriesHour[];
}

type Tab = "overview" | "prices" | "status" | "profiles" | "logs";

const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const WEEKDAY_PL: Record<string, string> = {
  mon: "Pon",
  tue: "Wt",
  wed: "Śr",
  thu: "Czw",
  fri: "Pt",
  sat: "Sob",
  sun: "Nd",
};
const HORIZON_COLORS: Record<string, string> = {
  "D+1": "#2ec4b6",
  "D+2": "#7b6cf6",
  "D+3": "#c98a3a",
};

const DEVICE_PALETTE = [
  "#7b6cf6",
  "#43a047",
  "#e67e22",
  "#3498db",
  "#9b59b6",
  "#e74c3c",
  "#1abc9c",
  "#f1c40f",
];

/** Inverter operating mode → human label + background tint for the energy chart.
 *  Tints use mid-opacity colors that read on both light and dark HA themes. */
const INVERTER_MODE_META: Record<string, { label: string; fill: string }> = {
  charge: { label: "ładowanie", fill: "rgba(46, 196, 182, 0.16)" },
  discharge: { label: "rozładowanie", fill: "rgba(233, 138, 160, 0.18)" },
  passthrough: { label: "passthrough", fill: "rgba(128, 128, 128, 0.10)" },
};

type RangeMode = "24h" | "3d" | "7d";

const RANGE_HOURS: Record<RangeMode, number> = {
  "24h": 24,
  "3d": 72,
  "7d": 168,
};

@customElement("powerpilot-panel")
export class PowerPilotPanel extends LitElement {
  @property({ attribute: false }) hass: any;
  @property({ attribute: false }) narrow = false;

  @state() private _tab: Tab = "overview";
  @state() private _plan: Plan | null = null;
  @state() private _status: Status | null = null;
  @state() private _log: LogEvent[] = [];
  @state() private _profiles: Profiles | null = null;
  @state() private _forecasts: Forecasts | null = null;
  @state() private _series: Series | null = null;
  @state() private _error: string | null = null;

  /** Active range preset. */
  @state() private _rangeMode: RangeMode = "3d";
  /** Right edge of the visible window. Defaults to "live" (now + horizon). */
  @state() private _anchor: Date | null = null;
  /** Selected day on the Prices tab (ISO string YYYY-MM-DD). Null = today. */
  @state() private _pricesDay: string | null = null;

  private _timer?: number;
  private _energyChart?: ApexCharts;
  private _priceChart?: ApexCharts;
  /** Reference to the last Series payload mounted into the charts. Used to
   *  short-circuit Lit updates that don't actually change the data, so user
   *  interactions (zoom, tooltip) survive periodic refreshes. */
  private _lastMountedSeries?: Series;

  connectedCallback(): void {
    super.connectedCallback();
    this._refresh();
    this._timer = window.setInterval(() => this._refresh(), 60000);
  }

  disconnectedCallback(): void {
    if (this._timer) window.clearInterval(this._timer);
    this._energyChart?.destroy();
    this._priceChart?.destroy();
    this._energyChart = undefined;
    this._priceChart = undefined;
    super.disconnectedCallback();
  }

  /** Compute the start/end ISO strings for the current window. */
  private _computeWindow(): { start: Date; end: Date; pastHours: number } {
    const hours = RANGE_HOURS[this._rangeMode];
    // anchor = right edge of window. Null means "live" — extend the right
    // edge to the end of whatever forecast horizon the backend currently
    // has (up to 96h), capped to a sensible default if no plan loaded yet.
    const live = this._anchor === null;
    const end = live ? this._liveEdge() : new Date(this._anchor!);
    const start = new Date(end.getTime() - hours * 3600 * 1000);
    return { start, end, pastHours: hours };
  }

  /** Right edge of the "live" window: end of plan horizon or now+24h fallback. */
  private _liveEdge(): Date {
    const plan = this._plan;
    if (plan?.hours?.length) {
      const last = plan.hours[plan.hours.length - 1];
      const t = new Date(last.start);
      if (!isNaN(t.getTime())) return new Date(t.getTime() + 3600 * 1000);
    }
    if (plan?.forecast?.length) {
      const last = plan.forecast[plan.forecast.length - 1];
      const t = new Date(last.start);
      if (!isNaN(t.getTime())) return new Date(t.getTime() + 3600 * 1000);
    }
    return new Date(Date.now() + 24 * 3600 * 1000);
  }

  private async _refresh(): Promise<void> {
    if (!this.hass) return;
    try {
      const { start, end, pastHours } = this._computeWindow();
      const [plan, status, log, profiles, series] = await Promise.all([
        this.hass.callWS({ type: "powerpilot/plan" }),
        this.hass.callWS({ type: "powerpilot/status" }),
        this.hass.callWS({ type: "powerpilot/log" }),
        this.hass.callWS({ type: "powerpilot/profiles" }),
        this.hass.callWS({
          type: "powerpilot/series",
          past_hours: pastHours,
          start: start.toISOString(),
          end: end.toISOString(),
        }),
      ]);
      this._plan = plan;
      this._status = status;
      this._log = log?.events ?? [];
      this._profiles = profiles;
      this._series = series;
      this._error = null;
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    }
  }

  private _setRange(mode: RangeMode): void {
    this._rangeMode = mode;
    this._refresh();
  }

  private _shiftAnchor(deltaHours: number): void {
    const { end } = this._computeWindow();
    const next = new Date(end.getTime() + deltaHours * 3600 * 1000);
    // Snap back to live mode if user navigates past the available horizon edge.
    const liveEdge = this._liveEdge().getTime();
    this._anchor = next.getTime() >= liveEdge ? null : next;
    this._refresh();
  }

  private _goLive(): void {
    this._anchor = null;
    this._refresh();
  }

  private _onDatePick(ev: Event): void {
    const value = (ev.target as HTMLInputElement).value;
    if (!value) return;
    // Treat date picker value as end-of-day local time so users see that day's data.
    const d = new Date(value + "T23:59:59");
    this._anchor = d;
    this._refresh();
  }

  private async _loadForecasts(): Promise<void> {
    if (this._forecasts || !this.hass) return;
    try {
      this._forecasts = await this.hass.callWS({ type: "powerpilot/forecasts" });
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    }
  }

  private _selectTab(tab: Tab): void {
    this._tab = tab;
    if (tab === "profiles") this._loadForecasts();
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
        ${this._tabButton("prices", "Ceny")}
        ${this._tabButton("status", "Status")}
        ${this._tabButton("profiles", "Profile")}
        ${this._tabButton("logs", "Logi")}
      </div>
      ${this._error ? html`<div class="error">Błąd: ${this._error}</div>` : nothing}
      <div class="content">
        ${this._tab === "overview" ? this._renderOverview() : nothing}
        ${this._tab === "prices" ? this._renderPrices() : nothing}
        ${this._tab === "status" ? this._renderStatus() : nothing}
        ${this._tab === "profiles" ? this._renderProfiles() : nothing}
        ${this._tab === "logs" ? this._renderLogs() : nothing}
      </div>
    `;
  }

  private _tabButton(tab: Tab, label: string): TemplateResult {
    return html`<button
      class=${"tab" + (this._tab === tab ? " active" : "")}
      @click=${() => this._selectTab(tab)}
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
      ${this._renderNavBar()}
      <div class="card">
        <div class="card-title">Energia: ↑ sieć/bateria · ↓ zużycie (stack) + tryb falownika + SoC</div>
        <div id="pp-chart-energy" class="apex-chart"></div>
      </div>
      <div class="card">
        <div class="card-title">Koszty: cena zakupu (PLN/kWh) + koszt godziny (PLN)</div>
        <div id="pp-chart-prices" class="apex-chart"></div>
      </div>
    `;
  }

  private _renderNavBar(): TemplateResult {
    const { start, end } = this._computeWindow();
    const isLive = this._anchor === null;
    const fmtDay = (d: Date) =>
      d.toLocaleDateString("pl-PL", { day: "2-digit", month: "2-digit", year: "numeric" });
    const fmtHour = (d: Date) =>
      d.toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
    const datePickerValue = (() => {
      const d = isLive ? new Date() : new Date(this._anchor!);
      return d.toISOString().slice(0, 10);
    })();
    const stepHours = this._rangeMode === "24h" ? 24 : this._rangeMode === "3d" ? 24 : 24;
    return html`
      <div class="card nav-card">
        <div class="nav-row">
          <button class="nav-btn" @click=${() => this._shiftAnchor(-stepHours)} title="Cofnij o dzień">«</button>
          <input
            type="date"
            class="nav-date"
            .value=${datePickerValue}
            @change=${this._onDatePick}
          />
          <button class="nav-btn" @click=${() => this._shiftAnchor(stepHours)} title="Następny dzień">»</button>
          <button class="nav-btn ${isLive ? "active" : ""}" @click=${this._goLive} title="Na żywo">● teraz</button>
          <div class="nav-spacer"></div>
          ${(["24h", "3d", "7d"] as RangeMode[]).map(
            (m) => html`
              <button
                class="nav-btn ${this._rangeMode === m ? "active" : ""}"
                @click=${() => this._setRange(m)}
              >
                ${m}
              </button>
            `
          )}
        </div>
        <div class="nav-info">
          Okno: <strong>${fmtDay(start)} ${fmtHour(start)}</strong> →
          <strong>${fmtDay(end)} ${fmtHour(end)}</strong>
          ${isLive ? html`<span class="muted"> · tryb live</span>` : nothing}
        </div>
      </div>
    `;
  }

  private _stat(label: string, value: string): TemplateResult {
    return html`<div class="stat"><span class="k">${label}</span><span class="v">${value}</span></div>`;
  }

  // ------------------------------------------------------------------
  // ApexCharts integration
  // ------------------------------------------------------------------
  protected updated(_changed: Map<string, unknown>): void {
    if (this._tab !== "overview") {
      // Tear down charts when switching away to free resources.
      if (this._energyChart || this._priceChart) {
        this._energyChart?.destroy();
        this._priceChart?.destroy();
        this._energyChart = undefined;
        this._priceChart = undefined;
        this._lastMountedSeries = undefined;
      }
      return;
    }
    // Mount or update on overview tab. _mountOrUpdateCharts short-circuits
    // when the Series reference hasn't changed, so unrelated state updates
    // (legend hover, tooltip show/hide, log polling) don't trash zoom state.
    this._mountOrUpdateCharts();
  }

  private _mountOrUpdateCharts(): void {
    const s = this._series;
    if (!s || !s.hours?.length) return;
    const energyEl = this.renderRoot.querySelector("#pp-chart-energy") as HTMLElement | null;
    const priceEl = this.renderRoot.querySelector("#pp-chart-prices") as HTMLElement | null;
    if (!energyEl || !priceEl) return;

    // If the Series reference hasn't changed since the last mount AND both
    // charts already exist, skip — this prevents the periodic 60s refresh
    // (and any unrelated Lit update) from resetting zoom/tooltip state.
    if (s === this._lastMountedSeries && this._energyChart && this._priceChart) {
      return;
    }

    const energyOpts = this._buildEnergyOptions(s);
    const priceOpts = this._buildPriceOptions(s);

    if (this._energyChart) {
      // `redrawPaths=false, animate=false` keeps zoom + tooltip state alive
      // through the data refresh; ApexCharts patches the SVG in place.
      this._energyChart.updateOptions(energyOpts, false, false);
    } else {
      this._energyChart = new ApexCharts(energyEl, energyOpts);
      this._energyChart.render();
    }
    if (this._priceChart) {
      this._priceChart.updateOptions(priceOpts, false, false);
    } else {
      this._priceChart = new ApexCharts(priceEl, priceOpts);
      this._priceChart.render();
    }
    this._lastMountedSeries = s;
  }

  /** Whether Home Assistant is currently in dark mode (drives chart theme). */
  private _isDark(): boolean {
    return !!this.hass?.themes?.darkMode;
  }

  /** Generate xaxis annotations for midnight boundaries within the visible series. */
  private _dayBoundaryAnnotations(s: Series): any[] {
    const DAY_PL = ["niedz.", "pon.", "wt.", "śr.", "czw.", "pt.", "sob."];
    const dark = this._isDark();
    const borderColor = dark ? "rgba(255,255,255,0.25)" : "rgba(0,0,0,0.18)";
    const textColor = dark ? "rgba(255,255,255,0.5)" : "rgba(0,0,0,0.45)";
    const annotations: any[] = [];
    const seen = new Set<string>();
    for (const h of s.hours) {
      const day = h.start.slice(0, 10);
      if (seen.has(day)) continue;
      seen.add(day);
      const midnight = new Date(day + "T00:00:00").getTime();
      // Skip if midnight is before the first hour in series.
      const firstTs = new Date(s.hours[0].start).getTime();
      if (midnight <= firstTs) continue;
      const d = new Date(midnight);
      annotations.push({
        x: midnight,
        borderColor,
        strokeDashArray: 0,
        label: {
          borderColor: "transparent",
          style: { background: "transparent", color: textColor, fontSize: "10px" },
          text: `${DAY_PL[d.getDay()]} ${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}`,
          orientation: "horizontal",
          position: "top",
        },
      });
    }
    return annotations;
  }

  /**
   * Colored background bands showing the inverter operating mode
   * (charge / discharge / passthrough). Consecutive hours sharing the same
   * mode are merged into a single region so the chart stays readable.
   */
  private _inverterModeAnnotations(s: Series): any[] {
    const regions: any[] = [];
    const labelColor = this._isDark() ? "rgba(255,255,255,0.6)" : "rgba(0,0,0,0.55)";
    let runStart: number | null = null;
    let runMode: string | null = null;

    const flush = (endTs: number) => {
      if (runStart == null || runMode == null) return;
      const meta = INVERTER_MODE_META[runMode];
      if (meta) {
        regions.push({
          x: runStart,
          x2: endTs,
          fillColor: meta.fill,
          opacity: 1,
          borderColor: "transparent",
          label: {
            text: meta.label,
            orientation: "horizontal",
            position: "bottom",
            offsetY: 14,
            borderColor: "transparent",
            style: {
              background: "transparent",
              color: labelColor,
              fontSize: "9px",
            },
          },
        });
      }
    };

    for (const h of s.hours) {
      const startTs = new Date(h.start).getTime();
      const mode = h.inverter_mode;
      if (mode !== runMode) {
        // Close the previous run where this hour begins, then open a new one.
        flush(startTs);
        runStart = mode ? startTs : null;
        runMode = mode;
      }
    }
    // Close the trailing run at the last hour's end.
    if (runStart != null && runMode != null) {
      const last = s.hours[s.hours.length - 1];
      flush(new Date(last.start).getTime() + 3600 * 1000);
    }
    return regions;
  }

  /**
   * Build ApexCharts options for the energy chart.
   *
   * Diverging stacked columns + SoC line:
   *   - UP (positive)   = energy supply, stacked into one bar:
   *                       grid import (charging the battery *or* passthrough)
   *                       + battery discharge.
   *   - DOWN (negative) = consumption, stacked into one bar:
   *                       base household load + per-device + EV + battery charge.
   * Background bands show the inverter mode (charge / discharge / passthrough).
   */
  private _buildEnergyOptions(s: Series): any {
    const hrs = s.hours;
    const ts = hrs.map((h) => new Date(h.start).getTime());

    const pair = (extract: (h: SeriesHour) => number | null) =>
      ts.map((t, i) => ({ x: t, y: extract(hrs[i]) }));

    // Sum of sub-metered devices for an hour (real preferred, forecast fallback).
    const deviceSum = (h: SeriesHour): number =>
      Object.values(h.devices_real ?? {}).reduce<number>((a, v) => a + (v ?? 0), 0) ||
      Object.values(h.devices_forecast ?? {}).reduce<number>((a, v) => a + (v ?? 0), 0);

    // Base household load = total consumption minus the sub-metered devices,
    // so stacking base + devices does not double-count.
    const baseConsumption = (h: SeriesHour): number | null => {
      if (h.is_past) {
        if (h.consumption_real == null) return null;
        return Math.max(0, h.consumption_real - deviceSum(h));
      }
      if (h.consumption_forecast == null) return h.base_consumption_forecast;
      return h.base_consumption_forecast ?? Math.max(0, h.consumption_forecast - deviceSum(h));
    };

    const device = (eid: string) => (h: SeriesHour): number | null => {
      const r = h.devices_real?.[eid];
      if (r != null) return r;
      const f = h.devices_forecast?.[eid];
      return f != null ? f : null;
    };

    // Stack component definitions — the single source of truth for both the
    // chart series and the custom tooltip breakdown.
    type Row = { label: string; color: string; get: (h: SeriesHour) => number | null };
    const deviceIds = s.device_ids ?? [];
    const upRows: Row[] = [
      { label: "Import z sieci", color: "#8e44ad", get: (h) => h.grid_buy_kwh },
      { label: "Bateria — rozładowanie", color: "#b0a14f", get: (h) => h.battery_discharge_kwh },
    ];
    const downRows: Row[] = [
      { label: "Zużycie bazowe", color: "#b5475d", get: baseConsumption },
      ...deviceIds.map((eid, idx) => ({
        label: `Urz: ${eid.split(".").slice(-1)[0]}`,
        color: DEVICE_PALETTE[idx % DEVICE_PALETTE.length],
        get: device(eid),
      })),
      { label: "EV ładowanie", color: "#3498db", get: (h) => h.ev_charge_kwh },
      { label: "Bateria — ładowanie", color: "#c98a3a", get: (h) => h.battery_charge_kwh },
    ];

    const series: any[] = [];
    const kwhNames: string[] = [];
    // sign = +1 for supply (up), -1 for consumption (down). Consumption values
    // are negated so they stack below zero on the shared diverging axis.
    const pushKwh = (
      name: string,
      color: string,
      sign: 1 | -1,
      getter: (h: SeriesHour) => number | null,
    ) => {
      const signed = (h: SeriesHour) => {
        const v = getter(h);
        return v == null ? null : sign * v;
      };
      series.push({ name, type: "column", data: pair(signed), color });
      kwhNames.push(name);
    };

    upRows.forEach((r) => pushKwh(r.label, r.color, 1, r.get));
    downRows.forEach((r) => pushKwh(r.label, r.color, -1, r.get));

    // Shared symmetric-ish scale so every per-series axis aligns and the
    // stacked bars line up. Compute the largest up-stack and down-stack.
    let posMax = 0;
    let negMax = 0;
    for (const h of hrs) {
      const up = (h.grid_buy_kwh ?? 0) + (h.battery_discharge_kwh ?? 0);
      const down =
        (baseConsumption(h) ?? 0) +
        deviceSum(h) +
        (h.ev_charge_kwh ?? 0) +
        (h.battery_charge_kwh ?? 0);
      posMax = Math.max(posMax, up);
      negMax = Math.max(negMax, down);
    }
    const axMax = posMax > 0 ? posMax * 1.1 : 1;
    const axMin = negMax > 0 ? -negMax * 1.1 : -1;

    // SoC line on the right axis. `soc` is the END-of-hour state; plotting it
    // at the hour start would move the line one hour too early (a 17:00
    // discharge would render its drop in the 16→17 segment). The backend also
    // provides `battery_soc_start` — the SoC the battery *enters* each hour
    // with — so the rise/fall lines up with the bar and inverter-mode band of
    // the hour that caused it, including the very first hour of the window.
    series.push({
      name: "SoC %",
      type: "line",
      data: pair((h) => h.battery_soc_start),
      color: "#2ec4b6",
    });

    const nowTs = Date.now();
    const dark = this._isDark();
    const nowColor = dark ? "#ffffff" : "#333333";
    const nowBg = dark ? "rgba(255,255,255,0.15)" : "rgba(0,0,0,0.08)";
    return {
      chart: {
        type: "line",
        height: 460,
        stacked: true,
        animations: { enabled: false },
        toolbar: {
          show: true,
          tools: { download: false, zoom: true, zoomin: true, zoomout: true, pan: true, reset: true },
        },
        zoom: { enabled: true, type: "x" },
        background: "transparent",
      },
      theme: { mode: dark ? "dark" : "light" },
      stroke: { width: series.map((sx: any) => (sx.type === "line" ? 2.5 : 0)), curve: "straight" },
      plotOptions: { bar: { columnWidth: "70%", borderRadius: 0 } },
      dataLabels: { enabled: false },
      fill: { opacity: 0.85 },
      series,
      xaxis: {
        type: "datetime",
        labels: {
          datetimeUTC: false,
          format: this._rangeMode === "24h" ? "HH:mm" : "dd.MM HH:mm",
        },
      },
      yaxis: [
        // ALL kWh column series share ONE physical axis — this is what makes
        // them stack into a single up/down bar per hour. `seriesName` is the
        // full list of column names so every series is explicitly mapped
        // (avoids the ApexCharts `setSeriesYAxisMappings` crash) while staying
        // on the same axis (mapping each to its own axis would break stacking).
        {
          seriesName: kwhNames,
          min: axMin,
          max: axMax,
          forceNiceScale: false,
          decimalsInFloat: 2,
          title: { text: "kWh  (↑ sieć/bateria · ↓ zużycie)" },
          labels: { formatter: (v: number) => (v != null ? Math.abs(v).toFixed(2) : "") },
        },
        {
          seriesName: "SoC %",
          opposite: true,
          min: 0,
          max: 100,
          title: { text: "SoC (%)" },
          labels: { formatter: (v: number) => (v != null ? v.toFixed(0) + " %" : "") },
        },
      ],
      tooltip: {
        shared: true,
        intersect: false,
        followCursor: false,
        // Custom HTML: show the total of the up-bar (supply) and down-bar
        // (consumption) plus the components that make up each sum — mirrors the
        // cost chart's tooltip style.
        custom: ({ dataPointIndex }: { dataPointIndex: number }) => {
          const h = hrs[dataPointIndex];
          if (!h) return "";
          const tt = dark
            ? { bg: "#1f2937", fg: "#f3f4f6", border: "#374151" }
            : { bg: "#ffffff", fg: "#1f2937", border: "#d1d5db" };
          const fmt = (v: number) => v.toFixed(2);
          const start = new Date(h.start);
          const date = start.toLocaleString("pl-PL", {
            weekday: "short",
            day: "2-digit",
            month: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          });
          const modeMeta = h.inverter_mode ? INVERTER_MODE_META[h.inverter_mode] : null;
          const modeStr = modeMeta ? `  •  falownik: ${modeMeta.label}` : "";

          // SoC entering vs leaving this hour (entering = start-of-hour state
          // from the backend, leaving = this hour's end-of-hour value).
          const socStart = h.battery_soc_start;
          const socEnd = h.soc;

          const dot = (c: string) =>
            `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${c};margin-right:5px;vertical-align:middle"></span>`;
          const compRows = (rows: Row[]) =>
            rows
              .map((r) => ({ label: r.label, color: r.color, v: r.get(h) ?? 0 }))
              .filter((r) => Math.abs(r.v) >= 0.005)
              .map(
                (r) =>
                  `<tr><td style="padding:1px 0 1px 12px;opacity:0.85">${dot(r.color)}${r.label}</td>` +
                  `<td style="text-align:right;font-variant-numeric:tabular-nums;opacity:0.85">${fmt(r.v)} kWh</td></tr>`,
              )
              .join("");
          const sum = (rows: Row[]) =>
            rows.reduce((acc, r) => acc + Math.abs(r.get(h) ?? 0), 0);
          const upTotal = sum(upRows);
          const downTotal = sum(downRows);
          const sep = `<tr><td colspan="2" style="padding:4px 0 2px"><div style="border-top:1px solid ${tt.border}"></div></td></tr>`;
          const socRow =
            socStart != null || socEnd != null
              ? `${sep}<tr><td style="padding:1px 0">SoC (pocz. → kon.)</td>` +
                `<td style="text-align:right;font-variant-numeric:tabular-nums">` +
                `${socStart != null ? socStart.toFixed(0) : "—"}% → ${socEnd != null ? socEnd.toFixed(0) : "—"}%</td></tr>`
              : "";

          return `
            <div style="padding:8px 10px;background:${tt.bg};color:${tt.fg};border:1px solid ${tt.border};border-radius:6px;font-size:12px;line-height:1.4;min-width:260px">
              <div style="font-weight:600;margin-bottom:6px;border-bottom:1px solid ${tt.border};padding-bottom:4px">${date}${modeStr}</div>
              <table style="border-collapse:collapse;width:100%">
                <tr><td style="padding:1px 0;font-weight:600">↑ Źródła energii</td><td style="text-align:right;font-weight:600;font-variant-numeric:tabular-nums">${fmt(upTotal)} kWh</td></tr>
                ${compRows(upRows)}
                ${sep}
                <tr><td style="padding:1px 0;font-weight:600">↓ Zużycie</td><td style="text-align:right;font-weight:600;font-variant-numeric:tabular-nums">${fmt(downTotal)} kWh</td></tr>
                ${compRows(downRows)}
                ${socRow}
              </table>
            </div>
          `;
        },
      },
      legend: {
        position: "bottom",
        horizontalAlign: "center",
        itemMargin: { horizontal: 14, vertical: 2 },
        fontSize: "12px",
        showForSingleSeries: true,
        showForZeroSeries: false,
        showForNullSeries: false,
      },
      annotations: {
        xaxis: [
          ...this._inverterModeAnnotations(s),
          ...this._dayBoundaryAnnotations(s),
          {
            x: nowTs,
            borderColor: nowColor,
            strokeDashArray: 4,
            label: {
              borderColor: nowColor,
              style: { background: nowBg, color: nowColor },
              text: "teraz",
            },
          },
        ],
      },
    };
  }

  /** Build ApexCharts options for the price chart (PLN/kWh line + PLN/h bars). */
  private _buildPriceOptions(s: Series): any {
    const hrs = s.hours;
    const ts = hrs.map((h) => new Date(h.start).getTime());

    // Single continuous line for total price (energy + distribution).
    // Tooltip shows the breakdown + confirmed/forecast indicator.
    const priceData = ts.map((t, i) => ({ x: t, y: hrs[i].total_price_kwh }));
    const batCostData = ts.map((t, i) => ({ x: t, y: hrs[i].battery_energy_cost }));
    // Two PLN/h stacked columns: cost served from the grid vs cost served
    // from the battery. Sum = total cost of meeting demand this hour.
    const gridCostData = ts.map((t, i) => ({ x: t, y: hrs[i].hour_cost }));
    const batUseCostData = ts.map((t, i) => ({ x: t, y: hrs[i].battery_use_cost }));

    const series: any[] = [
      { name: "Cena pełna", type: "line", data: priceData, color: "#facc15" },
      { name: "Cena w baterii", type: "line", data: batCostData, color: "#9e9e9e" },
      { name: "Koszt energii - sieć", type: "column", data: gridCostData, color: "#e67e22" },
      { name: "Koszt energii - bateria", type: "column", data: batUseCostData, color: "#3b82f6" },
    ];

    const nowTs = Date.now();
    const dark = this._isDark();
    const nowColor = dark ? "#ffffff" : "#333333";
    const nowBg = dark ? "rgba(255,255,255,0.15)" : "rgba(0,0,0,0.08)";
    return {
      chart: {
        type: "line",
        height: 380,
        stacked: true,
        animations: { enabled: false },
        toolbar: {
          show: true,
          tools: { download: false, zoom: true, zoomin: true, zoomout: true, pan: true, reset: true },
        },
        zoom: { enabled: true, type: "x" },
        background: "transparent",
      },
      theme: { mode: dark ? "dark" : "light" },
      stroke: {
        // 2 lines + 2 columns = 4 series total.
        width: [3, 2, 0, 0],
        curve: "straight",
        dashArray: [0, 3, 0, 0],
      },
      plotOptions: { bar: { columnWidth: "55%", borderRadius: 1 } },
      dataLabels: { enabled: false },
      fill: { opacity: [1, 1, 0.75, 0.7] },
      series,
      xaxis: {
        type: "datetime",
        labels: {
          datetimeUTC: false,
          format: this._rangeMode === "24h" ? "HH:mm" : "dd.MM HH:mm",
        },
      },
      yaxis: [
        {
          seriesName: "Cena pełna",
          title: { text: "PLN/kWh" },
          labels: { formatter: (v: number) => (v != null ? v.toFixed(3) : "") },
          forceNiceScale: true,
          decimalsInFloat: 3,
        },
        { seriesName: "Cena w baterii", show: false, forceNiceScale: true },
        {
          seriesName: "Koszt energii - sieć",
          opposite: true,
          title: { text: "PLN/h" },
          labels: { formatter: (v: number) => (v != null ? v.toFixed(2) : "") },
          forceNiceScale: true,
          min: 0,
        },
        { seriesName: "Koszt energii - bateria", opposite: true, show: false, forceNiceScale: true, min: 0 },
      ],
      tooltip: {
        shared: true,
        intersect: false,
        followCursor: false,
        x: { format: "EEEE dd.MM HH:mm" },
        // Custom HTML so price lines can show the energy/distribution split
        // that's encoded in the total. ApexCharts passes the data index of
        // the hovered point; we use it to look the slot back up.
        custom: ({ dataPointIndex }: { dataPointIndex: number }) => {
          const row = hrs[dataPointIndex];
          if (!row) return "";
          const fmt3 = (v: number | null) => (v == null ? "—" : v.toFixed(3));
          const fmt2 = (v: number | null) => (v == null ? "—" : v.toFixed(2));
          const start = new Date(row.start);
          const date = start.toLocaleString("pl-PL", {
            weekday: "short",
            day: "2-digit",
            month: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          });
          const confirmed = row.price_confirmed ? "(pewne)" : "(prognoza)";
          const tt = this._isDark()
            ? { bg: "#1f2937", fg: "#f3f4f6", border: "#374151" }
            : { bg: "#ffffff", fg: "#1f2937", border: "#d1d5db" };
          return `
            <div style="padding:8px 10px;background:${tt.bg};color:${tt.fg};border:1px solid ${tt.border};border-radius:6px;font-size:12px;line-height:1.4;min-width:240px">
              <div style="font-weight:600;margin-bottom:6px;border-bottom:1px solid ${tt.border};padding-bottom:4px">${date}</div>
              <table style="border-collapse:collapse;width:100%">
                <tr><td style="padding:1px 0">Cena całkowita ${confirmed}</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmt3(row.total_price_kwh)} PLN/kWh</td></tr>
                <tr><td style="padding:1px 0 1px 10px;opacity:0.8">· energia</td><td style="text-align:right;opacity:0.8;font-variant-numeric:tabular-nums">${fmt3(row.buy_price)} PLN/kWh</td></tr>
                <tr><td style="padding:1px 0 1px 10px;opacity:0.8">· dystrybucja (z VAT)</td><td style="text-align:right;opacity:0.8;font-variant-numeric:tabular-nums">${fmt3(row.distribution_price_kwh)} PLN/kWh</td></tr>
                <tr><td style="padding:1px 0">Cena w baterii</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmt3(row.battery_energy_cost)} PLN/kWh</td></tr>
                <tr><td colspan="2" style="padding:4px 0 2px"><div style="border-top:1px solid ${tt.border}"></div></td></tr>
                <tr><td style="padding:1px 0">Koszt z sieci</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmt2(row.hour_cost)} PLN</td></tr>
                <tr><td style="padding:1px 0">Koszt z baterii</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmt2(row.battery_use_cost)} PLN</td></tr>
              </table>
            </div>
          `;
        },
      },
      legend: {
        position: "bottom",
        horizontalAlign: "center",
        itemMargin: { horizontal: 14, vertical: 2 },
        fontSize: "12px",
      },
      annotations: {
        xaxis: [
          ...this._dayBoundaryAnnotations(s),
          {
            x: nowTs,
            borderColor: nowColor,
            strokeDashArray: 4,
            label: {
              borderColor: nowColor,
              style: { background: nowBg, color: nowColor },
              text: "teraz",
            },
          },
        ],
      },
    };
  }

  // ------------------------------------------------------------------
  // Chart engine (for Profiles tab overlay — legacy SVG)
  // ------------------------------------------------------------------
  /** Simple index-based polyline used by the forecast overlay. */
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
      const yy = pad + innerH - ((v - min) / span) * innerH;
      d += `${started ? "L" : "M"}${x.toFixed(1)},${yy.toFixed(1)} `;
      started = true;
    });
    return d.trim();
  }

  // ------------------------------------------------------------------
  // Prices tab (table + day switcher)
  // ------------------------------------------------------------------
  private _renderPrices(): TemplateResult {
    const s = this._series;
    if (!s || !s.hours?.length) {
      return html`<div class="card empty">Brak danych cenowych. Poczekaj na pierwsze przeliczenie.</div>`;
    }

    // Determine which days are available in the series data.
    const daySet = new Set<string>();
    for (const h of s.hours) {
      daySet.add(h.start.slice(0, 10));
    }
    const days = [...daySet].sort();
    const today = new Date().toISOString().slice(0, 10);
    const selectedDay = this._pricesDay && days.includes(this._pricesDay) ? this._pricesDay : (days.includes(today) ? today : days[0]);

    const filtered = s.hours.filter((h) => h.start.slice(0, 10) === selectedDay);

    const fmtHour = (iso: string) => {
      const d = new Date(iso);
      return d.toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
    };
    const fmt3 = (v: number | null) => (v == null ? "—" : v.toFixed(3));
    const fmt2 = (v: number | null) => (v == null ? "—" : v.toFixed(2));
    const fmtDayLabel = (iso: string) => {
      const d = new Date(iso + "T12:00:00");
      return d.toLocaleDateString("pl-PL", { weekday: "short", day: "2-digit", month: "2-digit" });
    };

    return html`
      <div class="card">
        <div class="card-title">Ceny godzinowe</div>
        <div class="prices-day-nav">
          ${days.map(
            (d) => html`
              <button
                class="nav-btn ${d === selectedDay ? "active" : ""}"
                @click=${() => { this._pricesDay = d; }}
              >
                ${fmtDayLabel(d)}
              </button>
            `
          )}
        </div>
        <div class="prices-table-wrap">
          <table class="prices-table">
            <thead>
              <tr>
                <th>Godzina</th>
                <th>Energia</th>
                <th>Dystrybucja</th>
                <th>Cena pełna</th>
                <th>Bateria</th>
                <th>Koszt/h</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              ${filtered.map(
                (h) => html`
                  <tr class="${h.is_past ? "past" : ""}">
                    <td>${fmtHour(h.start)}</td>
                    <td>${fmt3(h.buy_price)}</td>
                    <td>${fmt3(h.distribution_price_kwh)}</td>
                    <td class="bold">${fmt3(h.total_price_kwh)}</td>
                    <td>${fmt3(h.battery_energy_cost)}</td>
                    <td>${fmt2(h.hour_cost)}</td>
                    <td class="muted">${h.price_confirmed ? "✓" : "~"}</td>
                  </tr>
                `
              )}
            </tbody>
          </table>
        </div>
      </div>
    `;
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
  // Profiles (7×24 heatmaps + D+1..D+3 overlay)
  // ------------------------------------------------------------------
  private _renderProfiles(): TemplateResult {
    const p = this._profiles;
    return html`
      ${p
        ? html`
            <div class="card">
              <div class="card-title">Profil cen — 7×24 (${p.price_days} dni)</div>
              ${this._heatmap(p.price, "PLN/kWh")}
            </div>
            <div class="card">
              <div class="card-title">Profil zużycia (bazowy) — 7×24 (${p.consumption_days} dni)</div>
              ${this._heatmap(p.consumption, "kWh")}
            </div>
          `
        : html`<div class="card empty">Ładowanie profili…</div>`}
      <div class="card">
        <div class="card-title">
          Prognozy D+1..D+3 ${this._forecasts ? "— " + this._forecasts.date : ""}
        </div>
        ${this._renderForecastOverlay()}
      </div>
    `;
  }

  private _heatmap(matrix: Matrix, unit: string): TemplateResult {
    const values: number[] = [];
    WEEKDAYS.forEach((d) =>
      (matrix[d] ?? []).forEach((v) => {
        if (v !== null && v !== undefined) values.push(v);
      })
    );
    if (!values.length) return html`<div class="empty">Brak danych — profil jeszcze się uczy.</div>`;
    const min = Math.min(...values);
    const max = Math.max(...values);
    return html`
      <div class="heatmap">
        <div class="hm-row hm-head">
          <div class="hm-label"></div>
          ${Array.from({ length: 24 }, (_, h) => html`<div class="hm-h">${h}</div>`)}
        </div>
        ${WEEKDAYS.map(
          (d) => html`
            <div class="hm-row">
              <div class="hm-label">${WEEKDAY_PL[d]}</div>
              ${(matrix[d] ?? []).map((v) => {
                const color = v === null || v === undefined ? "transparent" : this._heatColor(v, min, max);
                const title = v === null || v === undefined ? "—" : `${v.toFixed(3)} ${unit}`;
                return html`<div class="hm-cell" style=${"background:" + color} title=${title}></div>`;
              })}
            </div>
          `
        )}
      </div>
      <div class="legend">
        <span>${min.toFixed(2)}</span>
        <div class="legend-bar"></div>
        <span>${max.toFixed(2)} ${unit}</span>
      </div>
    `;
  }

  private _heatColor(v: number, min: number, max: number): string {
    const t = max > min ? (v - min) / (max - min) : 0.5;
    const hue = (1 - t) * 160; // teal (low) → red (high)
    return `hsl(${hue}, 70%, 45%)`;
  }

  private _renderForecastOverlay(): TemplateResult {
    const f = this._forecasts;
    if (!f) return html`<div class="empty">Ładowanie prognoz…</div>`;
    const horizons = Object.keys(f.horizons || {});
    if (!horizons.length)
      return html`<div class="empty">Brak prognoz (wymaga źródła Pradcast z kluczem API).</div>`;

    const toArray = (pts: ForecastPoint[]): number[] => {
      const arr = new Array(24).fill(NaN);
      pts.forEach((p) => {
        if (p.buy !== null && p.hour >= 0 && p.hour < 24) arr[p.hour] = p.buy;
      });
      return arr;
    };
    const series = horizons.map((h) => ({ h, vals: toArray(f.horizons[h]) }));
    const all = series.flatMap((s) => s.vals).filter((v) => !isNaN(v));
    const min = Math.min(0, ...all);
    const max = Math.max(0.1, ...all);
    const w = 760;
    const ht = 180;
    return html`
      <svg viewBox="0 0 ${w} ${ht}" class="chart">
        ${series.map(
          (s) =>
            svg`<path d=${this._linePath(s.vals, min, max, w, ht)} fill="none"
              stroke=${HORIZON_COLORS[s.h] ?? "#888"} stroke-width="2" />`
        )}
      </svg>
      <div class="fc-legend">
        ${series.map(
          (s) => html`<span class="fc-key">
            <span class="swatch" style=${"background:" + (HORIZON_COLORS[s.h] ?? "#888")}></span>${s.h}
          </span>`
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
      <div class="card-title">Ostatnie zdarzenia</div>
      <table class="log">
        <thead>
          <tr>
            <th>Czas</th>
            <th>Typ</th>
            <th>Moduł</th>
            <th>Wiadomość</th>
            <th>Szczegóły</th>
          </tr>
        </thead>
        <tbody>
          ${this._log.map((e) => {
            const type = e.type ?? "plan";
            const moduleName = e.module ?? "coordinator";
            const message = e.message ?? this._planMessage(e);
            const details = this._eventDetails(e);
            return html`<tr class=${"log-row log-" + type}>
              <td class="log-time">${this._time(e.time)}</td>
              <td><span class=${"log-badge log-badge-" + type}>${this._typeLabel(type)}</span></td>
              <td class="log-module">${moduleName}</td>
              <td>${message}</td>
              <td class="log-extra">${details}</td>
            </tr>`;
          })}
        </tbody>
      </table>
    </div>`;
  }

  private _typeLabel(type: string): string {
    switch (type) {
      case "info":
        return "INFO";
      case "warning":
        return "WARN";
      case "plan":
        return "PLAN";
      default:
        return type.toUpperCase();
    }
  }

  private _planMessage(e: LogEvent): string {
    const parts: string[] = [];
    if (e.action) parts.push(`akcja=${e.action}`);
    if (e.battery_soc != null) parts.push(`SoC=${e.battery_soc}%`);
    if (e.ev_charge) parts.push("EV ładowanie");
    if (e.horizon_hours != null) parts.push(`horyzont ${e.horizon_hours}h`);
    return parts.join(", ") || "—";
  }

  private _eventDetails(e: LogEvent): string {
    const bits: string[] = [];
    if (e.errors && e.errors.length) bits.push("⚠ " + e.errors.join("; "));
    if (e.extra) {
      for (const [k, v] of Object.entries(e.extra)) {
        if (v == null) continue;
        const s = Array.isArray(v)
          ? `[${v.length}]`
          : typeof v === "object"
          ? JSON.stringify(v)
          : String(v);
        bits.push(`${k}=${s}`);
      }
    }
    return bits.join(" · ") || "—";
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
    .ax {
      fill: var(--secondary-text-color);
      font-size: 10px;
    }
    .ax.unit {
      font-weight: 600;
    }
    .ax.day {
      font-weight: 600;
    }
    .ax.now {
      fill: var(--primary-text-color);
      font-weight: 600;
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
      vertical-align: top;
    }
    td.err {
      color: var(--error-color, #d33);
    }
    .log-time {
      white-space: nowrap;
      color: var(--secondary-text-color);
      font-variant-numeric: tabular-nums;
    }
    .log-module {
      font-weight: 600;
      color: var(--secondary-text-color);
    }
    .log-extra {
      color: var(--secondary-text-color);
      font-family: var(--code-font-family, ui-monospace, monospace);
      font-size: 12px;
    }
    .log-badge {
      display: inline-block;
      padding: 1px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .log-badge-info {
      background: rgba(33, 150, 243, 0.15);
      color: #2196f3;
    }
    .log-badge-plan {
      background: rgba(76, 175, 80, 0.18);
      color: #66bb6a;
    }
    .log-badge-warning {
      background: rgba(255, 152, 0, 0.18);
      color: #ffa726;
    }
    .log-warning .log-extra {
      color: var(--warning-color, #ffa726);
    }
    .heatmap {
      display: flex;
      flex-direction: column;
      gap: 2px;
      overflow-x: auto;
    }
    .hm-row {
      display: flex;
      gap: 2px;
      align-items: center;
    }
    .hm-label {
      width: 34px;
      font-size: 12px;
      color: var(--secondary-text-color);
      flex: 0 0 auto;
    }
    .hm-h {
      width: 22px;
      text-align: center;
      font-size: 10px;
      color: var(--secondary-text-color);
      flex: 0 0 auto;
    }
    .hm-cell {
      width: 22px;
      height: 18px;
      border-radius: 2px;
      flex: 0 0 auto;
    }
    .legend {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      font-size: 12px;
      color: var(--secondary-text-color);
    }
    .legend-bar {
      flex: 1;
      max-width: 240px;
      height: 10px;
      border-radius: 5px;
      background: linear-gradient(
        90deg,
        hsl(160, 70%, 45%),
        hsl(80, 70%, 45%),
        hsl(0, 70%, 45%)
      );
    }
    .fc-legend {
      display: flex;
      gap: 16px;
      margin-top: 8px;
      font-size: 13px;
    }
    .fc-key {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .swatch {
      width: 12px;
      height: 12px;
      border-radius: 3px;
      display: inline-block;
    }
    /* Date navigation bar */
    .nav-card {
      padding: 10px 14px;
    }
    .nav-row {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .nav-btn {
      border: 1px solid var(--divider-color, #444);
      background: var(--card-background-color);
      color: var(--primary-text-color);
      border-radius: 8px;
      padding: 6px 12px;
      cursor: pointer;
      font-size: 13px;
    }
    .nav-btn:hover {
      background: var(--secondary-background-color, #2a2a2a);
    }
    .nav-btn.active {
      background: var(--primary-color);
      color: var(--text-primary-color, #fff);
      border-color: var(--primary-color);
    }
    .nav-date {
      background: var(--card-background-color);
      color: var(--primary-text-color);
      border: 1px solid var(--divider-color, #444);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 13px;
      color-scheme: dark;
    }
    .nav-spacer {
      flex: 1;
    }
    .nav-info {
      margin-top: 8px;
      font-size: 12px;
      color: var(--secondary-text-color);
    }
    /* ApexCharts container */
    .apex-chart {
      width: 100%;
      min-height: 380px;
    }
    /* Tooltip flicker workaround for ApexCharts inside Shadow DOM:
       the tooltip element itself catches mouse events and re-triggers
       enter/leave loops. Disabling pointer events keeps it stable. */
    .apexcharts-tooltip,
    .apexcharts-xaxistooltip,
    .apexcharts-yaxistooltip {
      pointer-events: none !important;
      background: var(--card-background-color, #2a2a2a) !important;
      color: var(--primary-text-color) !important;
      border: 1px solid var(--divider-color, #444) !important;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4) !important;
    }
    .apexcharts-tooltip-title {
      background: var(--secondary-background-color, #1f1f1f) !important;
      border-bottom: 1px solid var(--divider-color, #444) !important;
    }
    /* Force horizontal legend layout even when many series. */
    .apexcharts-legend {
      flex-wrap: wrap !important;
      justify-content: center !important;
    }
    .apexcharts-legend-series {
      display: inline-flex !important;
      align-items: center !important;
      margin: 2px 8px !important;
    }
    /* Prices tab */
    .prices-day-nav {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 12px;
    }
    .prices-table-wrap {
      overflow-x: auto;
    }
    .prices-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      font-variant-numeric: tabular-nums;
    }
    .prices-table th,
    .prices-table td {
      padding: 5px 8px;
      text-align: right;
      border-bottom: 1px solid var(--divider-color, #333);
    }
    .prices-table th {
      font-weight: 600;
      text-align: right;
      opacity: 0.7;
      font-size: 11px;
      text-transform: uppercase;
    }
    .prices-table th:first-child,
    .prices-table td:first-child {
      text-align: left;
    }
    .prices-table tr.past td {
      opacity: 0.5;
    }
    .prices-table .bold {
      font-weight: 600;
    }
  `;
}
