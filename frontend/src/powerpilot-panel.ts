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

type Tab = "overview" | "status" | "profiles" | "logs";

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
        ${this._tabButton("status", "Status")}
        ${this._tabButton("profiles", "Profile")}
        ${this._tabButton("logs", "Logi")}
      </div>
      ${this._error ? html`<div class="error">Błąd: ${this._error}</div>` : nothing}
      <div class="content">
        ${this._tab === "overview" ? this._renderOverview() : nothing}
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
        <div class="card-title">Energia: zużycie, bateria, EV, urządzenia + SoC</div>
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

  /** Build ApexCharts options for the energy chart (kWh bars + SoC %, line). */
  private _buildEnergyOptions(s: Series): any {
    const hrs = s.hours;
    const ts = hrs.map((h) => new Date(h.start).getTime());

    const pair = (extract: (h: SeriesHour) => number | null) =>
      ts.map((t, i) => ({ x: t, y: extract(hrs[i]) }));

    const series: any[] = [];
    const kwhNames: string[] = [];
    const pushKwh = (name: string, color: string, getter: (h: SeriesHour) => number | null) => {
      series.push({ name, type: "column", data: pair(getter), color });
      kwhNames.push(name);
    };

    pushKwh("Zużycie real", "#b5475d", (h) => (h.is_past ? h.consumption_real : null));
    pushKwh("Zużycie prog.", "#e08aa0", (h) => (!h.is_past ? h.consumption_forecast : null));
    pushKwh("Bateria — ładowanie", "#c98a3a", (h) => h.battery_charge_kwh);
    pushKwh("Bateria — rozładowanie", "#b0a14f", (h) => h.battery_discharge_kwh);
    pushKwh("Import z sieci", "#8e44ad", (h) => h.grid_buy_kwh);
    pushKwh("EV ładowanie", "#3498db", (h) => h.ev_charge_kwh);

    // Per-device.
    const deviceIds = s.device_ids ?? [];
    deviceIds.forEach((eid, idx) => {
      const color = DEVICE_PALETTE[idx % DEVICE_PALETTE.length];
      const friendly = eid.split(".").slice(-1)[0];
      const name = `Urz: ${friendly}`;
      pushKwh(name, color, (h) => {
        const r = h.devices_real?.[eid];
        if (r != null) return r;
        const f = h.devices_forecast?.[eid];
        return f != null ? f : null;
      });
    });

    // SoC line on the right axis.
    series.push({
      name: "SoC %",
      type: "line",
      data: pair((h) => h.soc),
      color: "#2ec4b6",
    });

    const nowTs = Date.now();
    return {
      chart: {
        type: "line",
        height: 460,
        stacked: false,
        animations: { enabled: false },
        toolbar: {
          show: true,
          tools: { download: false, zoom: true, zoomin: true, zoomout: true, pan: true, reset: true },
        },
        zoom: { enabled: true, type: "x" },
        background: "transparent",
      },
      theme: { mode: "dark" },
      stroke: { width: series.map((sx: any) => (sx.type === "line" ? 2.5 : 0)), curve: "straight" },
      plotOptions: { bar: { columnWidth: "75%", borderRadius: 1 } },
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
        // Each kWh series gets its OWN yaxis entry pointing at the same shared
        // physical axis (only the first carries title/labels; the rest are
        // hidden duplicates). This avoids the ApexCharts
        // `setSeriesYAxisMappings` crash that fires when some series have no
        // explicit yaxis mapping.
        ...kwhNames.map((name, idx) => ({
          seriesName: name,
          show: idx === 0,
          showAlways: idx === 0,
          title: idx === 0 ? { text: "kWh" } : undefined,
          min: 0,
          forceNiceScale: true,
          decimalsInFloat: 2,
          labels: idx === 0
            ? { formatter: (v: number) => (v != null ? v.toFixed(2) : "") }
            : { show: false },
        })),
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
        x: { format: "EEEE dd.MM HH:mm" },
        y: {
          formatter: (val: number, opts: any) => {
            if (val == null) return "—";
            const name = opts?.w?.config?.series?.[opts.seriesIndex]?.name ?? "";
            if (name === "SoC %") return val.toFixed(0) + " %";
            return val.toFixed(3) + " kWh";
          },
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
          {
            x: nowTs,
            borderColor: "#ffffff",
            strokeDashArray: 4,
            label: {
              borderColor: "#ffffff",
              style: { background: "rgba(255,255,255,0.15)", color: "#fff" },
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

    // Single price line per slot = TOTAL gross price (energy + distribution).
    // Split into "confirmed" (solid) and "forecast" (dashed) so the user can
    // tell where real RDN ends and the projection begins. The breakdown into
    // energy vs distribution is shown in the tooltip.
    const confirmedData = ts.map((t, i) => ({
      x: t,
      y: hrs[i].price_confirmed ? hrs[i].total_price_kwh : null,
    }));
    const forecastData = ts.map((t, i) => ({
      x: t,
      y: !hrs[i].price_confirmed ? hrs[i].total_price_kwh : null,
    }));
    const batCostData = ts.map((t, i) => ({ x: t, y: hrs[i].battery_energy_cost }));
    // Two PLN/h stacked columns: cost served from the grid vs cost served
    // from the battery. Sum = total cost of meeting demand this hour.
    const gridCostData = ts.map((t, i) => ({ x: t, y: hrs[i].hour_cost }));
    const batUseCostData = ts.map((t, i) => ({ x: t, y: hrs[i].battery_use_cost }));

    const series: any[] = [
      { name: "Cena zakupu (pewne)", type: "line", data: confirmedData, color: "#facc15" },
      { name: "Cena zakupu (prognoza)", type: "line", data: forecastData, color: "#fde68a" },
      { name: "Cena w baterii", type: "line", data: batCostData, color: "#9e9e9e" },
      { name: "Koszt energii - sieć", type: "column", data: gridCostData, color: "#e67e22" },
      { name: "Koszt energii - bateria", type: "column", data: batUseCostData, color: "#3b82f6" },
    ];

    const nowTs = Date.now();
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
      theme: { mode: "dark" },
      stroke: {
        // 3 lines + 2 columns = 5 series total.
        width: [3, 3, 2, 0, 0],
        curve: "straight",
        dashArray: [0, 5, 3, 0, 0],
      },
      plotOptions: { bar: { columnWidth: "55%", borderRadius: 1 } },
      dataLabels: { enabled: false },
      fill: { opacity: [1, 1, 1, 0.75, 0.7] },
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
          seriesName: "Cena zakupu (pewne)",
          title: { text: "PLN/kWh" },
          labels: { formatter: (v: number) => (v != null ? v.toFixed(3) : "") },
          forceNiceScale: true,
          decimalsInFloat: 3,
        },
        { seriesName: "Cena zakupu (prognoza)", show: false, forceNiceScale: true },
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
          return `
            <div style="padding:8px 10px;background:#1f2937;color:#f3f4f6;border:1px solid #374151;border-radius:6px;font-size:12px;line-height:1.4;min-width:240px">
              <div style="font-weight:600;margin-bottom:6px;border-bottom:1px solid #374151;padding-bottom:4px">${date}</div>
              <table style="border-collapse:collapse;width:100%">
                <tr><td style="padding:1px 0">Cena całkowita ${confirmed}</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmt3(row.total_price_kwh)} PLN/kWh</td></tr>
                <tr><td style="padding:1px 0 1px 10px;opacity:0.8">· energia</td><td style="text-align:right;opacity:0.8;font-variant-numeric:tabular-nums">${fmt3(row.buy_price)} PLN/kWh</td></tr>
                <tr><td style="padding:1px 0 1px 10px;opacity:0.8">· dystrybucja (z VAT)</td><td style="text-align:right;opacity:0.8;font-variant-numeric:tabular-nums">${fmt3(row.distribution_price_kwh)} PLN/kWh</td></tr>
                <tr><td style="padding:1px 0">Cena w baterii</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmt3(row.battery_energy_cost)} PLN/kWh</td></tr>
                <tr><td colspan="2" style="padding:4px 0 2px"><div style="border-top:1px solid #374151"></div></td></tr>
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
          {
            x: nowTs,
            borderColor: "#ffffff",
            strokeDashArray: 4,
            label: {
              borderColor: "#ffffff",
              style: { background: "rgba(255,255,255,0.15)", color: "#fff" },
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
  `;
}
