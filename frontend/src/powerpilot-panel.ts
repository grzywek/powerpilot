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
  price_archive_hours: number;
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

type PriceType = "certain" | "forecast" | "estimated";

interface EstimateSample {
  weeks_ago: number;
  weight: number;
  date: string;
  value: number | null;
  type: PriceType | null;
}

interface PriceArchiveHour {
  start: string;
  type: PriceType | null;
  source: string | null;
  fetched_at: string | null;
  energy_price_kwh: number | null;
  distribution_price_kwh: number | null;
  total_price_kwh: number | null;
  p10: number | null;
  p90: number | null;
  estimate_breakdown: EstimateSample[] | null;
}

interface PriceArchive {
  date: string;
  hours: PriceArchiveHour[];
}

/** Price-type → badge label + color. Drives the "Typ" column on the Ceny tab. */
const PRICE_TYPE_META: Record<PriceType, { label: string; color: string }> = {
  certain: { label: "pewna", color: "#43a047" },
  forecast: { label: "prognoza", color: "#3498db" },
  estimated: { label: "szacowana", color: "#e67e22" },
};

const PRICE_SOURCE_LABEL: Record<string, string> = {
  pradcast: "prądcast.pl",
  sensor: "sensor HA",
  estimate: "szacowanie",
};

interface SnapshotRun {
  run_at: string;
  start: string | null;
  horizon_hours: number | null;
  total_cost: number | null;
}

interface SnapshotHour {
  start: string | null;
  buy_price: number | null;
  distribution_price_kwh: number | null;
  total_price_kwh: number | null;
  price_type: PriceType | null;
  consumption_forecast: number | null;
  base_consumption_forecast: number | null;
  inverter_mode: string | null;
  battery_soc: number | null;
  soc: number | null;
  grid_buy_kwh: number | null;
  hour_cost: number | null;
}

interface SnapshotPayload {
  run_at: string | null;
  start?: string;
  total_cost?: number | null;
  hours: SnapshotHour[];
}

interface AccuracyHour {
  start: string;
  predicted_cons: number | null;
  actual_cons: number | null;
  error: number | null;
  predicted_price: number | null;
  actual_price: number | null;
}

interface Accuracy {
  lead_hours: number;
  days: number;
  samples: number;
  mae: number | null;
  bias: number | null;
  bias_by_hour: (number | null)[];
  hours: AccuracyHour[];
}

type Tab = "overview" | "prices" | "simulations" | "status" | "profiles" | "logs" | "debug";

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

  /** Debug dump state (generated on demand from the Debug tab). */
  @state() private _debug: unknown = null;
  @state() private _debugLoading = false;
  @state() private _debugError: string | null = null;
  @state() private _debugCopied = false;

  /** Active range preset. */
  @state() private _rangeMode: RangeMode = "3d";
  /** Right edge of the visible window. Defaults to "live" (now + horizon). */
  @state() private _anchor: Date | null = null;
  /** Selected day on the Prices tab (ISO string YYYY-MM-DD). Null = today. */
  @state() private _pricesDay: string | null = null;
  /** Price archive payload for the selected day (independent of the chart window). */
  @state() private _pricesData: PriceArchive | null = null;
  @state() private _pricesLoading = false;

  /** Simulations tab: available vintages, the two compared snapshots, accuracy. */
  @state() private _snapshotRuns: SnapshotRun[] = [];
  @state() private _snapA: string | null = null;
  @state() private _snapB: string | null = null;
  @state() private _snapDataA: SnapshotPayload | null = null;
  @state() private _snapDataB: SnapshotPayload | null = null;
  @state() private _accuracy: Accuracy | null = null;
  @state() private _accuracyLead = 24;
  @state() private _simLoading = false;

  private _timer?: number;
  private _energyChart?: ApexCharts;
  private _priceChart?: ApexCharts;
  private _compareChart?: ApexCharts;
  private _accuracyChart?: ApexCharts;
  private _biasChart?: ApexCharts;
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
    this._destroySimCharts();
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
      // Keep the price archive fresh while it's on screen (today's rows pick up
      // newly confirmed / re-forecast prices between source fetches).
      if (this._tab === "prices") this._loadPrices();
      // Pick up newly recorded vintages + fresh actuals; A/B selection is kept.
      if (this._tab === "simulations") this._loadSimulations();
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
    if (tab === "prices") this._loadPrices();
    if (tab === "simulations") this._loadSimulations();
  }

  // ------------------------------------------------------------------
  // Simulations: snapshot list + A/B payloads + accuracy
  // ------------------------------------------------------------------
  private async _loadSimulations(): Promise<void> {
    if (!this.hass) return;
    this._simLoading = true;
    try {
      const list = await this.hass.callWS({ type: "powerpilot/snapshots" });
      const runs: SnapshotRun[] = list?.runs ?? [];
      this._snapshotRuns = runs;
      if (runs.length) {
        // A = newest vintage; B = one from ~24 h earlier (else the oldest).
        if (!this._snapA || !runs.some((r) => r.run_at === this._snapA)) {
          this._snapA = runs[0].run_at;
        }
        if (!this._snapB || !runs.some((r) => r.run_at === this._snapB)) {
          const aTime = new Date(this._snapA!).getTime();
          const target = aTime - 24 * 3600 * 1000;
          let best = runs[runs.length - 1];
          for (const r of runs) {
            if (r.run_at === this._snapA) continue;
            if (
              Math.abs(new Date(r.run_at).getTime() - target) <
              Math.abs(new Date(best.run_at).getTime() - target)
            )
              best = r;
          }
          this._snapB = best.run_at;
        }
        await Promise.all([
          this._loadSnapshotData("A"),
          this._loadSnapshotData("B"),
        ]);
      }
      await this._loadAccuracy();
      this._error = null;
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    } finally {
      this._simLoading = false;
    }
  }

  private async _loadSnapshotData(which: "A" | "B"): Promise<void> {
    if (!this.hass) return;
    const runAt = which === "A" ? this._snapA : this._snapB;
    if (!runAt) return;
    const data: SnapshotPayload = await this.hass.callWS({
      type: "powerpilot/snapshot",
      run_at: runAt,
    });
    if (which === "A") this._snapDataA = data;
    else this._snapDataB = data;
  }

  private async _loadAccuracy(): Promise<void> {
    if (!this.hass) return;
    this._accuracy = await this.hass.callWS({
      type: "powerpilot/accuracy",
      lead_hours: this._accuracyLead,
      days: 7,
    });
  }

  private _onSnapPick(which: "A" | "B", ev: Event): void {
    const value = (ev.target as HTMLSelectElement).value;
    if (which === "A") this._snapA = value;
    else this._snapB = value;
    this._loadSnapshotData(which);
  }

  private _setAccuracyLead(lead: number): void {
    this._accuracyLead = lead;
    this._loadAccuracy();
  }

  /** Local-time ISO date (YYYY-MM-DD) the prices tab currently shows. */
  private _pricesSelectedDay(): string {
    return this._pricesDay ?? this._localISODate(new Date());
  }

  private _localISODate(d: Date): string {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  private async _loadPrices(): Promise<void> {
    if (!this.hass) return;
    const day = this._pricesSelectedDay();
    this._pricesLoading = true;
    try {
      this._pricesData = await this.hass.callWS({
        type: "powerpilot/prices",
        date: day,
      });
      this._error = null;
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    } finally {
      this._pricesLoading = false;
    }
  }

  private _setPricesDay(day: string): void {
    this._pricesDay = day;
    this._loadPrices();
  }

  private _shiftPricesDay(deltaDays: number): void {
    const base = new Date(this._pricesSelectedDay() + "T12:00:00");
    base.setDate(base.getDate() + deltaDays);
    this._setPricesDay(this._localISODate(base));
  }

  private _onPricesDatePick(ev: Event): void {
    const value = (ev.target as HTMLInputElement).value;
    if (value) this._setPricesDay(value);
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
        ${this._tabButton("simulations", "Symulacje")}
        ${this._tabButton("status", "Status")}
        ${this._tabButton("profiles", "Profile")}
        ${this._tabButton("logs", "Logi")}
        ${this._tabButton("debug", "Debug")}
      </div>
      ${this._error ? html`<div class="error">Błąd: ${this._error}</div>` : nothing}
      <div class="content">
        ${this._tab === "overview" ? this._renderOverview() : nothing}
        ${this._tab === "prices" ? this._renderPrices() : nothing}
        ${this._tab === "simulations" ? this._renderSimulations() : nothing}
        ${this._tab === "status" ? this._renderStatus() : nothing}
        ${this._tab === "profiles" ? this._renderProfiles() : nothing}
        ${this._tab === "logs" ? this._renderLogs() : nothing}
        ${this._tab === "debug" ? this._renderDebug() : nothing}
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
    // Tear down each tab's charts when it isn't active, to free resources.
    if (this._tab !== "overview" && (this._energyChart || this._priceChart)) {
      this._energyChart?.destroy();
      this._priceChart?.destroy();
      this._energyChart = undefined;
      this._priceChart = undefined;
      this._lastMountedSeries = undefined;
    }
    if (this._tab !== "simulations") this._destroySimCharts();

    // _mountOrUpdateCharts short-circuits when the Series reference hasn't
    // changed, so unrelated state updates (legend hover, log polling) don't
    // trash zoom state.
    if (this._tab === "overview") this._mountOrUpdateCharts();
    else if (this._tab === "simulations") this._mountSimCharts();
  }

  private _destroySimCharts(): void {
    if (this._compareChart || this._accuracyChart || this._biasChart) {
      this._compareChart?.destroy();
      this._accuracyChart?.destroy();
      this._biasChart?.destroy();
      this._compareChart = undefined;
      this._accuracyChart = undefined;
      this._biasChart = undefined;
    }
  }

  private _mountSimCharts(): void {
    const compareEl = this.renderRoot.querySelector("#pp-chart-compare") as HTMLElement | null;
    if (compareEl && (this._snapDataA || this._snapDataB)) {
      const opts = this._buildCompareOptions(this._snapDataA, this._snapDataB);
      if (this._compareChart) this._compareChart.updateOptions(opts, false, false);
      else {
        this._compareChart = new ApexCharts(compareEl, opts);
        this._compareChart.render();
      }
    }

    const acc = this._accuracy;
    const accEl = this.renderRoot.querySelector("#pp-chart-accuracy") as HTMLElement | null;
    if (accEl && acc) {
      const opts = this._buildAccuracyOptions(acc);
      if (this._accuracyChart) this._accuracyChart.updateOptions(opts, false, false);
      else {
        this._accuracyChart = new ApexCharts(accEl, opts);
        this._accuracyChart.render();
      }
    }

    const biasEl = this.renderRoot.querySelector("#pp-chart-bias") as HTMLElement | null;
    if (biasEl && acc) {
      const opts = this._buildBiasOptions(acc);
      if (this._biasChart) this._biasChart.updateOptions(opts, false, false);
      else {
        this._biasChart = new ApexCharts(biasEl, opts);
        this._biasChart.render();
      }
    }
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

    // ApexCharts centres datetime columns on their x value, so a bar plotted at
    // the hour start would straddle the hour line. Plot bars at the hour
    // *midpoint* so the column spans [H, H+1] and visually starts on the hour
    // gridline. The SoC line stays on the boundaries (hour starts) via `pair`,
    // so its points land exactly on the bar edges.
    const HALF_HOUR = 1800 * 1000;
    const pairBar = (extract: (h: SeriesHour) => number | null) =>
      ts.map((t, i) => ({ x: t + HALF_HOUR, y: extract(hrs[i]) }));

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
      series.push({ name, type: "column", data: pairBar(signed), color });
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
      // Near-full width so each midpoint-plotted bar fills its hour [H, H+1]
      // and its left edge lands on the hour gridline.
      plotOptions: { bar: { columnWidth: "95%", borderRadius: 0 } },
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
    const selectedDay = this._pricesSelectedDay();
    const data = this._pricesData;

    const fmtHour = (iso: string) => {
      const d = new Date(iso);
      return d.toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
    };
    const fmtStamp = (iso: string | null) => {
      if (!iso) return "—";
      const d = new Date(iso);
      if (isNaN(d.getTime())) return "—";
      return d.toLocaleString("pl-PL", {
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    };
    const fmt3 = (v: number | null) => (v == null ? "—" : v.toFixed(3));
    const fmtDayLabel = (iso: string) => {
      const d = new Date(iso + "T12:00:00");
      return d.toLocaleDateString("pl-PL", {
        weekday: "long",
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
      });
    };

    const today = this._localISODate(new Date());
    const tomorrow = (() => {
      const d = new Date();
      d.setDate(d.getDate() + 1);
      return this._localISODate(d);
    })();

    const nav = html`
      <div class="prices-day-nav">
        <button class="nav-btn" @click=${() => this._shiftPricesDay(-1)} title="Poprzedni dzień">«</button>
        <input
          type="date"
          class="nav-date"
          .value=${selectedDay}
          @change=${this._onPricesDatePick}
        />
        <button class="nav-btn" @click=${() => this._shiftPricesDay(1)} title="Następny dzień">»</button>
        <button class="nav-btn ${selectedDay === today ? "active" : ""}" @click=${() => this._setPricesDay(today)}>dziś</button>
        <button class="nav-btn ${selectedDay === tomorrow ? "active" : ""}" @click=${() => this._setPricesDay(tomorrow)}>jutro</button>
        <div class="nav-spacer"></div>
        <span class="muted">${fmtDayLabel(selectedDay)}</span>
      </div>
    `;

    const rows = data?.hours ?? [];
    const hasAny = rows.some((h) => h.energy_price_kwh != null);

    const body = !data
      ? html`<div class="empty">${this._pricesLoading ? "Ładowanie…" : "Brak danych."}</div>`
      : !hasAny
      ? html`<div class="empty">Brak cen dla wybranego dnia — archiwum jeszcze nie sięga tak daleko.</div>`
      : html`
          <div class="prices-table-wrap">
            <table class="prices-table">
              <thead>
                <tr>
                  <th>Godzina</th>
                  <th>Typ</th>
                  <th>Źródło</th>
                  <th>Pobrano</th>
                  <th>Energia<br /><span class="muted">z VAT</span></th>
                  <th>Dystrybucja<br /><span class="muted">z VAT</span></th>
                  <th>Cena pełna<br /><span class="muted">z VAT</span></th>
                </tr>
              </thead>
              <tbody>
                ${rows.map((h) => this._renderPriceRow(h, fmtHour, fmtStamp, fmt3))}
              </tbody>
            </table>
          </div>
          <div class="prices-legend">
            ${(["certain", "forecast", "estimated"] as PriceType[]).map(
              (t) => html`<span class="badge" style=${"background:" + PRICE_TYPE_META[t].color}>${PRICE_TYPE_META[t].label}</span>`
            )}
            <span class="muted">Wszystkie ceny brutto (z VAT). „szacowana” = średnia ważona z 3 ostatnich tygodni — najedź na typ, by zobaczyć obliczenie.</span>
          </div>
        `;

    return html`
      <div class="card">
        <div class="card-title">Archiwum cen — podgląd danych optymalizatora</div>
        ${nav}
        ${body}
      </div>
    `;
  }

  private _renderPriceRow(
    h: PriceArchiveHour,
    fmtHour: (iso: string) => string,
    fmtStamp: (iso: string | null) => string,
    fmt3: (v: number | null) => string
  ): TemplateResult {
    const meta = h.type ? PRICE_TYPE_META[h.type] : null;
    const sourceLabel = h.source ? PRICE_SOURCE_LABEL[h.source] ?? h.source : "—";
    const badge = meta
      ? html`<span class="badge" style=${"background:" + meta.color} title=${this._priceTooltip(h)}>${meta.label}</span>`
      : html`<span class="muted">—</span>`;
    return html`
      <tr>
        <td>${fmtHour(h.start)}</td>
        <td>${badge}</td>
        <td class="muted">${sourceLabel}</td>
        <td class="muted">${fmtStamp(h.fetched_at)}</td>
        <td>${fmt3(h.energy_price_kwh)}</td>
        <td>${fmt3(h.distribution_price_kwh)}</td>
        <td class="bold">${fmt3(h.total_price_kwh)}</td>
      </tr>
    `;
  }

  /** Hover text explaining how a row's price was derived. */
  private _priceTooltip(h: PriceArchiveHour): string {
    if (h.type === "certain") {
      return "Cena pewna (wiążąca RDN) — nie zmienia się już.";
    }
    if (h.type === "forecast") {
      const band =
        h.p10 != null && h.p90 != null
          ? ` Przedział P10–P90: ${h.p10.toFixed(3)}–${h.p90.toFixed(3)} PLN/kWh.`
          : "";
      return `Prognoza ze źródła — odświeżana co kilka godzin.${band}`;
    }
    if (h.type === "estimated" && h.estimate_breakdown) {
      const lines = h.estimate_breakdown.map((s) => {
        const v = s.value == null ? "brak" : `${s.value.toFixed(3)} PLN/kWh`;
        return `• ${s.date} (−${s.weeks_ago} tyg., waga ${s.weight}): ${v}`;
      });
      return [
        "Cena szacowana = średnia ważona tej samej godziny w tym samym dniu tygodnia z ostatnich 3 tygodni:",
        ...lines,
        "Wagi są normalizowane do dostępnych próbek.",
      ].join("\n");
    }
    return "";
  }

  // ------------------------------------------------------------------
  // Simulations (snapshot compare + forecast accuracy)
  // ------------------------------------------------------------------
  private _fmtRun(iso: string | null): string {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString("pl-PL", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  private _renderSimulations(): TemplateResult {
    const runs = this._snapshotRuns;
    if (!runs.length) {
      return html`<div class="card empty">
        ${this._simLoading
          ? "Ładowanie…"
          : "Brak zapisanych wersji. Optymalizator zapisuje jeden snapshot na godzinę — wróć tu za jakiś czas."}
      </div>`;
    }

    const runOption = (sel: string | null) => (r: SnapshotRun) => html`
      <option value=${r.run_at} ?selected=${r.run_at === sel}>
        ${this._fmtRun(r.run_at)}${r.total_cost != null ? ` · ${r.total_cost.toFixed(2)} PLN` : ""}
      </option>
    `;

    const acc = this._accuracy;
    const fmt = (v: number | null, d = 3) => (v == null ? "—" : v.toFixed(d));

    return html`
      <div class="card">
        <div class="card-title">Porównanie wersji planu (A vs B)</div>
        <div class="sim-picker">
          <label class="sim-pick">
            <span class="sim-tag sim-a">A</span>
            <select @change=${(e: Event) => this._onSnapPick("A", e)}>
              ${runs.map(runOption(this._snapA))}
            </select>
          </label>
          <label class="sim-pick">
            <span class="sim-tag sim-b">B</span>
            <select @change=${(e: Event) => this._onSnapPick("B", e)}>
              ${runs.map(runOption(this._snapB))}
            </select>
          </label>
        </div>
        <div class="muted sim-hint">
          A pełna linia, B przerywana. Te same godziny docelowe — widać, jak prognoza zużycia i
          trajektoria SoC zmieniły się między przeliczeniami.
        </div>
        <div id="pp-chart-compare" class="apex-chart"></div>
      </div>

      <div class="card">
        <div class="card-title">Trafność prognozy — przewidywanie vs rzeczywistość</div>
        <div class="sim-lead">
          <span class="muted">Wyprzedzenie:</span>
          ${[24, 48, 72].map(
            (l) => html`<button
              class="nav-btn ${this._accuracyLead === l ? "active" : ""}"
              @click=${() => this._setAccuracyLead(l)}
            >
              ${l} h
            </button>`
          )}
          ${acc
            ? html`<span class="nav-spacer"></span>
                <span class="muted">próbki: <b>${acc.samples}</b></span>
                <span class="muted">MAE: <b>${fmt(acc.mae)}</b> kWh</span>
                <span class="muted">bias: <b>${fmt(acc.bias)}</b> kWh</span>`
            : nothing}
        </div>
        <div class="muted sim-hint">
          Co przewidywaliśmy ${this._accuracyLead} h wcześniej kontra realne zużycie. Ujemny bias =
          systematyczne <b>niedoszacowanie</b>.
        </div>
        <div id="pp-chart-accuracy" class="apex-chart"></div>
        <div class="card-title" style="margin-top:8px;">Błąd zużycia wg godziny doby (kWh)</div>
        <div id="pp-chart-bias" class="apex-chart apex-chart-short"></div>
      </div>
    `;
  }

  /** Map snapshot hours to {x: ms, y} points for a numeric field. */
  private _snapPoints(
    data: SnapshotPayload | null,
    get: (h: SnapshotHour) => number | null
  ): { x: number; y: number | null }[] {
    if (!data?.hours?.length) return [];
    return data.hours
      .filter((h) => h.start)
      .map((h) => ({ x: new Date(h.start as string).getTime(), y: get(h) }));
  }

  private _buildCompareOptions(a: SnapshotPayload | null, b: SnapshotPayload | null): any {
    const dark = this._isDark();
    const grid = dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)";
    const fg = dark ? "rgba(255,255,255,0.7)" : "rgba(0,0,0,0.7)";
    const series = [
      { name: "Zużycie A", type: "line", color: "#1d9e75", data: this._snapPoints(a, (h) => h.consumption_forecast) },
      { name: "Zużycie B", type: "line", color: "#1d9e75", data: this._snapPoints(b, (h) => h.consumption_forecast) },
      { name: "SoC A", type: "line", color: "#ba7517", data: this._snapPoints(a, (h) => h.soc) },
      { name: "SoC B", type: "line", color: "#ba7517", data: this._snapPoints(b, (h) => h.soc) },
    ];
    return {
      chart: { type: "line", height: 280, background: "transparent", toolbar: { show: false }, animations: { enabled: false } },
      theme: { mode: dark ? "dark" : "light" },
      series,
      stroke: { width: [2, 2, 2, 2], dashArray: [0, 5, 0, 5], curve: "smooth" },
      colors: ["#1d9e75", "#1d9e75", "#ba7517", "#ba7517"],
      xaxis: { type: "datetime", labels: { datetimeUTC: false, style: { colors: fg } } },
      yaxis: [
        { seriesName: "Zużycie A", title: { text: "kWh", style: { color: fg } }, labels: { style: { colors: fg } }, decimalsInFloat: 2 },
        { seriesName: "Zużycie A", show: false },
        { seriesName: "SoC A", opposite: true, min: 0, max: 100, title: { text: "SoC %", style: { color: fg } }, labels: { style: { colors: fg } } },
        { seriesName: "SoC A", show: false, opposite: true, min: 0, max: 100 },
      ],
      legend: { labels: { colors: fg } },
      grid: { borderColor: grid },
      tooltip: { theme: dark ? "dark" : "light", x: { format: "dd.MM HH:mm" } },
    };
  }

  private _buildAccuracyOptions(acc: Accuracy): any {
    const dark = this._isDark();
    const grid = dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)";
    const fg = dark ? "rgba(255,255,255,0.7)" : "rgba(0,0,0,0.7)";
    const pts = (get: (h: AccuracyHour) => number | null) =>
      acc.hours.map((h) => ({ x: new Date(h.start).getTime(), y: get(h) }));
    return {
      chart: { type: "line", height: 280, background: "transparent", toolbar: { show: false }, animations: { enabled: false } },
      theme: { mode: dark ? "dark" : "light" },
      series: [
        { name: "Zużycie — prognoza", color: "#3498db", data: pts((h) => h.predicted_cons) },
        { name: "Zużycie — rzeczywiste", color: "#e24b4a", data: pts((h) => h.actual_cons) },
      ],
      stroke: { width: [2, 2], dashArray: [5, 0], curve: "smooth" },
      colors: ["#3498db", "#e24b4a"],
      xaxis: { type: "datetime", labels: { datetimeUTC: false, style: { colors: fg } } },
      yaxis: { title: { text: "kWh", style: { color: fg } }, labels: { style: { colors: fg } }, decimalsInFloat: 2 },
      legend: { labels: { colors: fg } },
      grid: { borderColor: grid },
      tooltip: { theme: dark ? "dark" : "light", x: { format: "dd.MM HH:mm" } },
    };
  }

  private _buildBiasOptions(acc: Accuracy): any {
    const dark = this._isDark();
    const grid = dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)";
    const fg = dark ? "rgba(255,255,255,0.7)" : "rgba(0,0,0,0.7)";
    const data = acc.bias_by_hour.map((v, i) => ({ x: `${i}`, y: v }));
    return {
      chart: { type: "bar", height: 200, background: "transparent", toolbar: { show: false }, animations: { enabled: false } },
      theme: { mode: dark ? "dark" : "light" },
      series: [{ name: "Bias zużycia", data }],
      plotOptions: { bar: { colors: { ranges: [
        { from: -1000, to: 0, color: "#e24b4a" },
        { from: 0, to: 1000, color: "#3498db" },
      ] } } },
      dataLabels: { enabled: false },
      xaxis: { title: { text: "godzina doby", style: { color: fg } }, labels: { style: { colors: fg } } },
      yaxis: { title: { text: "kWh", style: { color: fg } }, labels: { style: { colors: fg } }, decimalsInFloat: 2 },
      grid: { borderColor: grid },
      tooltip: { theme: dark ? "dark" : "light" },
    };
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
        <div class="check">Archiwum cen: <b>${s.price_archive_hours}</b> godz.</div>
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

  private async _generateDebug(): Promise<void> {
    this._debugLoading = true;
    this._debugError = null;
    this._debugCopied = false;
    try {
      this._debug = await this.hass.callWS({ type: "powerpilot/debug" });
    } catch (err: unknown) {
      this._debugError = err instanceof Error ? err.message : String(err);
      this._debug = null;
    } finally {
      this._debugLoading = false;
    }
  }

  private _debugJson(): string {
    return JSON.stringify(this._debug, null, 2);
  }

  private _downloadDebug(): void {
    const json = this._debugJson();
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    const blob = new Blob([json], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `powerpilot-debug-${stamp}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  private async _copyDebug(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this._debugJson());
      this._debugCopied = true;
      window.setTimeout(() => {
        this._debugCopied = false;
      }, 2000);
    } catch {
      this._debugError = "Nie udało się skopiować do schowka.";
    }
  }

  private _renderDebug(): TemplateResult {
    const json = this._debug != null ? this._debugJson() : "";
    const sizeKb = json ? (new Blob([json]).size / 1024).toFixed(1) : "0";
    return html`
      <div class="card">
        <div class="card-title">Zrzut diagnostyczny</div>
        <p class="debug-intro">
          Generuje pełny zrzut JSON: konfiguracja (bez sekretów), bieżący plan
          z uzasadnieniem każdej decyzji (<code>trace</code>: progi taniej/drogiej,
          stan baterii przed godziną, powód trybu), status, profile zużycia,
          serię (48 h wstecz + horyzont) oraz log. Pobierz lub skopiuj i wklej do
          analizy.
        </p>
        <div class="debug-actions">
          <button class="debug-btn primary" @click=${this._generateDebug} ?disabled=${this._debugLoading}>
            ${this._debugLoading ? "Generowanie…" : "Generuj zrzut"}
          </button>
          ${this._debug != null
            ? html`
                <button class="debug-btn" @click=${this._downloadDebug}>⬇ Pobierz JSON (${sizeKb} kB)</button>
                <button class="debug-btn" @click=${this._copyDebug}>
                  ${this._debugCopied ? "✓ Skopiowano" : "⧉ Kopiuj do schowka"}
                </button>
              `
            : nothing}
        </div>
        ${this._debugError ? html`<div class="error">Błąd: ${this._debugError}</div>` : nothing}
        ${this._debug != null
          ? html`<pre class="debug-json">${json}</pre>`
          : html`<div class="empty">Kliknij „Generuj zrzut", aby pobrać dane.</div>`}
      </div>
    `;
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
    .debug-intro {
      color: var(--secondary-text-color);
      font-size: 13px;
      line-height: 1.5;
      margin: 0 0 12px;
    }
    .debug-intro code {
      font-family: var(--code-font-family, monospace);
      font-size: 12px;
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.15));
      padding: 1px 4px;
      border-radius: 4px;
    }
    .debug-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }
    .debug-btn {
      background: var(--card-background-color, #fff);
      color: var(--primary-text-color);
      border: 1px solid var(--divider-color, rgba(127, 127, 127, 0.3));
      border-radius: 6px;
      padding: 7px 14px;
      font-size: 13px;
      cursor: pointer;
    }
    .debug-btn:hover {
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.12));
    }
    .debug-btn.primary {
      background: var(--primary-color, #03a9f4);
      color: var(--text-primary-color, #fff);
      border-color: transparent;
    }
    .debug-btn[disabled] {
      opacity: 0.6;
      cursor: default;
    }
    .debug-json {
      max-height: 420px;
      overflow: auto;
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.1));
      border: 1px solid var(--divider-color, rgba(127, 127, 127, 0.3));
      border-radius: 6px;
      padding: 10px;
      font-family: var(--code-font-family, monospace);
      font-size: 11px;
      line-height: 1.4;
      white-space: pre;
      margin: 0;
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
    .prices-table th:nth-child(2),
    .prices-table td:nth-child(2),
    .prices-table th:nth-child(3),
    .prices-table td:nth-child(3),
    .prices-table th:nth-child(4),
    .prices-table td:nth-child(4) {
      text-align: left;
    }
    .prices-day-nav {
      align-items: center;
    }
    .badge {
      display: inline-block;
      padding: 1px 8px;
      border-radius: 10px;
      color: #fff;
      font-size: 11px;
      font-weight: 600;
      white-space: nowrap;
      cursor: help;
    }
    .prices-legend {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      font-size: 12px;
    }
    .sim-picker {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 8px;
    }
    .sim-pick {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .sim-pick select {
      padding: 5px 8px;
      border-radius: 6px;
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.12));
      border: 1px solid var(--divider-color, rgba(127, 127, 127, 0.3));
      color: inherit;
      font-size: 13px;
    }
    .sim-tag {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 20px;
      height: 20px;
      border-radius: 50%;
      color: #fff;
      font-size: 12px;
      font-weight: 600;
    }
    .sim-a {
      background: #1d9e75;
    }
    .sim-b {
      background: #7b6cf6;
    }
    .sim-hint {
      font-size: 12px;
      margin-bottom: 8px;
    }
    .sim-lead {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin-bottom: 8px;
      font-size: 12px;
    }
    .apex-chart-short {
      min-height: 200px;
    }
  `;
}
