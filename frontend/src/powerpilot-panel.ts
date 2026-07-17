import { LitElement, html, css, svg, nothing, type TemplateResult } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import ApexCharts from "apexcharts";

interface PlanHour {
  start: string;
  inverter_mode: string;
  charge_power: string;
  charge_power_kw: number;
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

interface EVTrip {
  label: string;
  location: string;
  event_start: string;
  event_end: string;
  depart: string;
  return_end: string;
  distance_km: number | null;
  duration_min: number | null;
  energy_kwh: number | null;
}

interface EVPlan {
  enabled: boolean;
  available: boolean;
  home: boolean | null;
  soc: number | null;
  target_soc: number | null;
  energy_added_kwh: number | null;
  charging: boolean | null;
  soc_limit: number | null;
  charger_power_kw: number | null;
  capacity_kwh: number | null;
  capacity_source: string | null;
  capacity_sessions: number;
  capacity_ready: boolean;
  min_capacity_sessions: number;
  kwh_per_km: number | null;
  drain_days: number;
  drain_next24_kwh: number | null;
  min_soc: number | null;
  current_control: boolean;
  min_current_a: number;
  max_current_a: number;
  targets: {
    deadline: string;
    target_soc: number;
    label: string;
    source?: string;
    // Trip targets: the reserve (EV min-SoC entity) + round-trip split.
    reserve_soc?: number | null;
    trip_soc?: number | null;
  }[];
  forced_hours: string[];
  trips: EVTrip[];
  planned_hours: { start: string; kwh: number }[];
  control: {
    connect_charger: boolean;
    charging_now: boolean;
    charge_start: string | null;
    soc_limit: number | null;
    charge_amps: number | null;
  };
}

interface Status {
  version: string;
  last_update: string | null;
  horizon_hours: number;
  price_archive_hours: number;
  consumption_days: number;
  consumption_devices: string[];
  ev_enabled: boolean;
  ev?: EVPlan;
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

/** One temperature bin of a climate profile: 24 hourly kWh averages. */
interface ClimateProfileRow {
  temp_from: number;
  temp_to: number;
  samples: number;
  values: (number | null)[];
}

interface ClimateProfileInfo {
  observed_days: number;
  samples: number;
  ready: boolean;
  min_learn_days: number;
  matrix: ClimateProfileRow[];
}

interface Profiles {
  consumption: Matrix;
  consumption_days: number;
  devices: Record<string, Matrix>;
  /** Temperature profiles of the weather-dependent loads, keyed by sensor. */
  climate?: Record<string, ClimateProfileInfo>;
}

interface StatProfile {
  key: string;
  name: string;
  icon: string;
  daily: { date: string; kwh: number; partial: boolean }[];
  avg_daily: number;
  week_total: number;
  week_change_pct: number | null;
  month_total: number;
  month_change_pct: number | null;
}

interface ConsumptionStats {
  generated_at: string;
  window_days: number;
  learned_days: number;
  profiles: StatProfile[];
}

interface SeriesHour {
  start: string;
  is_past: boolean;
  buy_price: number | null;
  distribution_price_kwh: number | null;
  total_price_kwh: number | null;
  price_confirmed: boolean;
  price_type: PriceType | null;
  consumption_real: number | null;
  consumption_forecast: number | null;
  base_consumption_forecast: number | null;
  soc: number | null;
  ev_soc: number | null;
  battery_soc_start: number | null;
  inverter_mode: string | null;
  partial?: boolean;
  partial_until?: string;
  realized: TipSide | null;
  forecast: TipSide | null;
  // When the forecast side was made (ISO), so the tooltip can show the vintage.
  forecast_origin?: string | null;
  battery_charge_kwh: number | null;
  battery_discharge_kwh: number | null;
  charge_power_kw: number | null;
  battery_energy_cost: number | null;
  grid_buy_kwh: number | null;
  ev_charge_kwh: number | null;
  hour_cost: number | null;
  energy_cost: number | null;
  distribution_cost: number | null;
  battery_use_cost: number | null;
  fixed_cost: number | null;
  devices_real: Record<string, number | null>;
  devices_forecast: Record<string, number | null>;
}

interface TipSide {
  grid: number | null;
  discharge: number | null;
  base: number | null;
  ev: number | null;
  charge: number | null;
  devices: Record<string, number>;
  soc_start: number | null;
  soc_end: number | null;
  ev_soc_start: number | null;
  ev_soc_end: number | null;
}

interface Series {
  now: string;
  past_hours: number;
  start: string;
  end: string;
  device_ids: string[];
  hours: SeriesHour[];
  /** Trips for the window: live calendar trips + history harvested from
   *  snapshot vintages (past events stay visible after leaving the calendar);
   *  at a stale forecast lead, only the pinned vintage's trips. */
  trips?: EVTrip[];
  /** When the prognoza is pinned (lead N or an exact run_at): the pinned
   *  vintage and the span it covered — hours inside carry realne + prognoza,
   *  hours outside only realne. */
  forecast_pin?: { run_at: string; start: string; end: string } | null;
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
  fixed_cost_hourly: number | null;
  p10: number | null;
  p90: number | null;
  estimate_breakdown: EstimateSample[] | null;
  // Accuracy comparators (energy side, gross PLN/kWh): the last source
  // forecast before the hour settled, and the weekly-model estimate.
  forecast_energy_kwh: number | null;
  forecast_fetched_at: string | null;
  estimate_energy_kwh: number | null;
  // Seller-style net breakdown (present only for hours fetched under the new
  // pricing model; legacy/estimated rows leave these null).
  tge_kwh?: number | null;
  markup_kwh?: number | null;
  distribution_net_kwh?: number | null;
  excise_kwh?: number | null;
  taxes_kwh?: number | null;
  vat_rate?: number | null;
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

interface AccuracyHour {
  start: string;
  predicted_cons: number | null;
  actual_cons: number | null;
  error: number | null;
  predicted_price: number | null;
  actual_price: number | null;
  price_error: number | null;
}

interface Accuracy {
  lead_hours: number;
  days: number;
  samples: number;
  mae: number | null;
  bias: number | null;
  price_samples: number;
  price_mae: number | null;
  price_bias: number | null;
  bias_by_hour: (number | null)[];
  hours: AccuracyHour[];
}

type Tab =
  | "overview"
  | "prices"
  | "simulations"
  | "flow"
  | "efficiency"
  | "status"
  | "diagnostics"
  | "profiles"
  | "logs"
  | "debug";

/** One power bucket of the measured EV charging efficiency. */
interface EfficiencyBucket {
  power_kw: number;
  hours: number;
  grid_kwh: number | null;
  added_kwh: number | null;
  measured_eff: number | null;
  configured_eff: number | null;
}

/** Payload of powerpilot/efficiency — measured vs configured efficiencies. */
interface EfficiencyData {
  generated_at: string;
  window_days: number;
  ev: {
    available: boolean;
    grid_sensor: string | null;
    added_sensor: string | null;
    hours: number;
    grid_kwh: number | null;
    added_kwh: number | null;
    measured_eff: number | null;
    configured_curve: { kw: number; eff: number }[];
    buckets: EfficiencyBucket[];
  };
  battery: {
    available: boolean;
    charge_sensor: string | null;
    discharge_sensor: string | null;
    charge_kwh: number | null;
    discharge_kwh: number | null;
    measured_roundtrip: number | null;
    configured_charge_eff: number | null;
    configured_discharge_eff: number | null;
    configured_roundtrip: number | null;
    charge_curve_points: number;
  };
}

/** One live-read input entity on the "Przepływ" tab. */
interface FlowEntity {
  entity_id: string;
  value: string | null;
  unit: string | null;
  available: boolean;
}

/** Payload of powerpilot/flow — a live snapshot of the computation pipeline. */
interface FlowData {
  now: string;
  inputs: {
    consumption: FlowEntity | null;
    device_sensors: { entity_id: string }[];
    battery_soc: FlowEntity | null;
    battery_charge: FlowEntity | null;
    battery_discharge: FlowEntity | null;
    grid_import: FlowEntity | null;
    buy_price_sensor: FlowEntity | null;
    weather: FlowEntity | null;
    ev_soc: FlowEntity | null;
    ev_energy_added: FlowEntity | null;
    calendars: string[];
    price_source: string | null;
  };
  pricing: {
    markup: number;
    vat: number;
    excise_kwh: number;
    buy_price_now: number | null;
    distribution_now: number | null;
    fixed_hourly: number | null;
    total_now: number | null;
    confirmed: boolean;
  };
  consumption_model: {
    observed_days: number;
    base_now_kwh: number | null;
    device_profiles: number;
  };
  battery: {
    capacity_kwh: number;
    soc: number | null;
    charge_efficiency: number;
    discharge_efficiency: number;
    wear_cost: number;
    reservoir_cost: number;
    delivered_cost: number | null;
    store_cost_now: number | null;
    efficiency_curve_points?: number;
  };
  ev: {
    enabled: boolean;
    soc: number | null;
    capacity_kwh: number | null;
    charger_power_kw: number | null;
    targets: number;
    trips: number;
  };
  optimizer: {
    created_at: string | null;
    horizon_hours: number;
    total_cost: number | null;
    current: {
      inverter_mode: string;
      battery_charge_kwh: number;
      battery_discharge_kwh: number;
      grid_buy_kwh: number;
      ev_charge_kwh: number;
      battery_soc_end: number;
      hour_cost: number;
      battery_use_cost: number;
    } | null;
  };
}

interface DiagSensorDetail {
  entity_id: string;
  available: boolean;
  state: string | null;
  unit_of_measurement: string | null;
  state_class: string | null;
  detected_kind: string | null;
  stat_rows_sum: number;
  stat_rows_mean: number;
  series_hours: number;
  samples: { hour: string; kwh: number }[];
}

interface DiagItem {
  key: string;
  label: string;
  required: boolean;
  entity_id: string | null;
  status: "ok" | "warn" | "error" | "skip";
  message: string;
  detail: (Partial<DiagSensorDetail> & Record<string, unknown>) | null;
}

interface Diagnostics {
  generated_at: string;
  ready: boolean;
  summary: { ok: number; warn: number; error: number; skip: number };
  groups: { title: string; items: DiagItem[] }[];
}

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

/** Inverter operating mode → human label + solid dot color. The mode is drawn
 *  as a row of colored dots along the bottom of the energy chart (background
 *  bands collided with the trip away-window shading and became unreadable). */
const INVERTER_MODE_META: Record<string, { label: string; dot: string }> = {
  charge: { label: "ładowanie baterii", dot: "#22c55e" },
  discharge: { label: "z baterii", dot: "#e11d48" },
  passthrough: { label: "passthrough", dot: "#94a3b8" },
};

/** Trip away-window shading: travel legs (dojazd/powrót + margins) vs the
 *  event itself, so both read separately on the chart. Grays on purpose —
 *  the chart already carries a lot of color in the bars and lines. */
const TRIP_FILL = { travel: "#9ca3af", event: "#4b5563" };

type RangeMode = "24h" | "3d" | "7d";

const RANGE_HOURS: Record<RangeMode, number> = {
  "24h": 24,
  "3d": 72,
  "7d": 168,
};

/** Forecast lead presets for the past "prognoza" comparison, in hours.
 *  0 = the freshest plan (made as each hour began). */
const FORECAST_LEADS: { label: string; hours: number }[] = [
  { label: "ostatnia", hours: 0 },
  { label: "−3h", hours: 3 },
  { label: "−6h", hours: 6 },
  { label: "−12h", hours: 12 },
  { label: "−24h", hours: 24 },
];

@customElement("powerpilot-panel")
export class PowerPilotPanel extends LitElement {
  @property({ attribute: false }) hass: any;
  @property({ attribute: false }) narrow = false;

  @state() private _tab: Tab = "overview";
  @state() private _plan: Plan | null = null;
  @state() private _status: Status | null = null;
  @state() private _log: LogEvent[] = [];
  @state() private _profiles: Profiles | null = null;
  @state() private _stats: ConsumptionStats | null = null;
  @state() private _statsLoading = false;
  @state() private _statsKey: string | null = null;
  @state() private _series: Series | null = null;
  @state() private _error: string | null = null;

  /** Debug dump state (generated on demand from the Debug tab). */
  @state() private _diagnostics: Diagnostics | null = null;
  @state() private _diagnosticsLoading = false;

  /** "Przepływ" tab: live pipeline snapshot. */
  @state() private _flow: FlowData | null = null;
  @state() private _flowLoading = false;

  /** "Sprawność" tab: measured vs configured efficiencies. */
  @state() private _efficiency: EfficiencyData | null = null;
  @state() private _efficiencyLoading = false;

  @state() private _debug: unknown = null;
  @state() private _debugLoading = false;
  @state() private _debugError: string | null = null;
  @state() private _debugCopied = false;

  /** Active range preset. */
  @state() private _rangeMode: RangeMode = "3d";
  /** Selected day. Null = today (live). When set (to a past day) it is the
   *  window's start; the chart shows that day's realized data plus, on the
   *  right, the current plan's forecast horizon. */
  @state() private _anchor: Date | null = null;
  /** Selected forecast lead (hours) for the past "prognoza" comparison. */
  @state() private _forecastLead = 0;
  /** Exact prognoza pin: show the plan in force at this local datetime
   *  (ISO, hour precision). Takes precedence over the lead buttons. */
  @state() private _forecastRunAt: string | null = null;
  /** Selected day on the Prices tab (ISO string YYYY-MM-DD). Null = today. */
  @state() private _pricesDay: string | null = null;
  /** Price archive payload for the selected day (independent of the chart window). */
  @state() private _pricesData: PriceArchive | null = null;
  @state() private _pricesLoading = false;

  /** Simulations tab: forecast accuracy (consumption + price) at a lead. */
  @state() private _accuracy: Accuracy | null = null;
  @state() private _accuracyLead = 24;
  @state() private _simLoading = false;

  private _timer?: number;
  private _energyChart?: ApexCharts;
  private _priceChart?: ApexCharts;
  private _accuracyChart?: ApexCharts;
  private _priceAccuracyChart?: ApexCharts;
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

  /** Local midnight (00:00) of the given date. */
  private _midnight(d: Date): Date {
    const m = new Date(d);
    m.setHours(0, 0, 0, 0);
    return m;
  }

  /** Compute the start/end of the current window.
   *
   * The window starts at the selected anchor — local midnight when picked from
   * the date field, and possibly noon after « / » stepping (they move in 12 h
   * half-days) — and spans 24h / 3d / 7d from there. `_anchor` holds that
   * start; `null` means "today from midnight" (live). The selected moment is
   * therefore always the left edge of the chart. */
  private _computeWindow(): { start: Date; end: Date; pastHours: number } {
    const hours = RANGE_HOURS[this._rangeMode];
    const start = this._anchor ? new Date(this._anchor) : this._midnight(new Date());
    start.setMinutes(0, 0, 0);
    const end = new Date(start.getTime() + hours * 3600 * 1000);
    return { start, end, pastHours: hours };
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
          // `forecast_lead` picks how far out the past "prognoza" comparison is
          // read from (0 = the freshest plan made as each hour began).
          forecast_lead: this._forecastLead,
          // Exact pin beats the lead: show the plan in force at that moment.
          ...(this._forecastRunAt ? { forecast_run_at: this._forecastRunAt } : {}),
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
      // Pick up newly recorded vintages + fresh actuals for the accuracy view.
      if (this._tab === "simulations") this._loadSimulations();
      // Keep the pipeline snapshot live while it's on screen.
      if (this._tab === "flow") this._loadFlow();
      // Refresh the measured efficiencies while on screen (new settled hours).
      if (this._tab === "efficiency") this._loadEfficiency();
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    }
  }

  private _setRange(mode: RangeMode): void {
    this._rangeMode = mode;
    this._refresh();
  }

  /** Move the window start by ±12 h (the « / » buttons) — half-day hops make
   *  it easy to visualise the chart sliding. Reaching today (or later) returns
   *  to live mode. */
  private _shiftDay(delta: number): void {
    const base = new Date(this._anchor ?? this._midnight(new Date()));
    base.setHours(base.getHours() + delta * 12, 0, 0, 0);
    this._anchor = base.getTime() >= this._midnight(new Date()).getTime() ? null : base;
    this._refresh();
  }

  private _goLive(): void {
    this._anchor = null;
    this._refresh();
  }

  private _onDatePick(ev: Event): void {
    const value = (ev.target as HTMLInputElement).value;
    if (!value) return;
    // <input type="date"> yields "YYYY-MM-DD"; read it as local midnight.
    const picked = new Date(value + "T00:00");
    if (isNaN(picked.getTime())) return;
    this._anchor =
      picked.getTime() >= this._midnight(new Date()).getTime() ? null : picked;
    this._refresh();
  }

  private _setForecastLead(hours: number): void {
    this._forecastLead = hours;
    this._forecastRunAt = null; // the preset buttons drop an exact pin
    this._refresh();
  }

  /** Pin the prognoza to the plan in force at the picked local datetime. */
  private _onForecastPinPick(ev: Event): void {
    const value = (ev.target as HTMLInputElement).value; // "YYYY-MM-DDTHH:mm"
    if (!value) {
      this._forecastRunAt = null;
      this._refresh();
      return;
    }
    const picked = new Date(value);
    if (isNaN(picked.getTime())) return;
    picked.setMinutes(0, 0, 0); // vintages are hourly
    this._forecastRunAt = picked.toISOString();
    this._ensurePinVisible(picked);
    this._refresh();
  }

  /** The datetime the prognoza is effectively pinned to: the explicit pick, or
   *  the vintage the backend resolved a lead preset (−3h, −6h…) to. Null when
   *  nothing is pinned ("ostatnia" = freshest plan per hour). */
  private _effectivePinDate(): Date | null {
    const iso = this._forecastRunAt ?? this._series?.forecast_pin?.run_at ?? null;
    if (!iso) return null;
    const d = new Date(iso);
    return isNaN(d.getTime()) ? null : d;
  }

  /** Step the prognoza pin ±1 h (the ‹ / › buttons). Starts from the effective
   *  pin (explicit or preset-resolved), or from the current hour when nothing
   *  is pinned yet. Clamped to the current hour — there are no future plans. */
  private _shiftForecastPin(deltaHours: number): void {
    const base = this._effectivePinDate() ?? new Date();
    base.setMinutes(0, 0, 0);
    base.setHours(base.getHours() + deltaHours);
    const nowHour = new Date();
    nowHour.setMinutes(0, 0, 0);
    if (base.getTime() > nowHour.getTime()) base.setTime(nowHour.getTime());
    this._forecastRunAt = base.toISOString();
    this._ensurePinVisible(base);
    this._refresh();
  }

  /** A pin outside the visible window would change nothing on screen — jump
   *  the window (half-day aligned, matching the « / » stepping) so the pinned
   *  moment and its coverage band are in view. */
  private _ensurePinVisible(pin: Date): void {
    const { start, end } = this._computeWindow();
    if (pin.getTime() >= start.getTime() && pin.getTime() < end.getTime()) return;
    const base = this._midnight(pin);
    if (pin.getHours() >= 12) base.setHours(12);
    this._anchor =
      base.getTime() >= this._midnight(new Date()).getTime() ? null : base;
  }

  /** The exact-pin input value ("YYYY-MM-DDTHH:00") for the current state.
   *  A lead preset shows the vintage it resolved to, so the field always
   *  carries the hour the shown prognoza was made at. */
  private _forecastPinInputValue(): string {
    const d = this._effectivePinDate();
    if (!d) return "";
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:00`;
  }

  /** date / datetime-local inputs only open their native picker from the tiny
   *  calendar icon — open it from a click anywhere in the field instead. */
  private _openPicker(ev: Event): void {
    const input = ev.currentTarget as HTMLInputElement & { showPicker?: () => void };
    try {
      input.showPicker?.();
    } catch {
      // Not allowed outside a user gesture / unsupported browser — the icon
      // and keyboard entry still work.
    }
  }

  private async _loadStats(): Promise<void> {
    if (!this.hass) return;
    this._statsLoading = true;
    try {
      this._stats = await this.hass.callWS({ type: "powerpilot/consumption_stats" });
      // Default selection: whole-house profile, else the first one.
      if (this._stats?.profiles?.length && !this._statsKey) {
        this._statsKey = this._stats.profiles[0].key;
      }
      this._error = null;
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    } finally {
      this._statsLoading = false;
    }
  }

  private _selectTab(tab: Tab): void {
    this._tab = tab;
    if (tab === "profiles") {
      this._loadStats();
    }
    if (tab === "prices") this._loadPrices();
    if (tab === "simulations") this._loadSimulations();
    if (tab === "diagnostics") this._loadDiagnostics();
    if (tab === "flow") this._loadFlow();
    if (tab === "efficiency") this._loadEfficiency();
  }

  private async _loadFlow(): Promise<void> {
    if (!this.hass) return;
    this._flowLoading = true;
    try {
      this._flow = await this.hass.callWS({ type: "powerpilot/flow" });
      this._error = null;
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    } finally {
      this._flowLoading = false;
    }
  }

  private async _loadEfficiency(): Promise<void> {
    if (!this.hass) return;
    this._efficiencyLoading = true;
    try {
      this._efficiency = await this.hass.callWS({
        type: "powerpilot/efficiency",
        days: 30,
      });
      this._error = null;
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    } finally {
      this._efficiencyLoading = false;
    }
  }

  private async _loadDiagnostics(): Promise<void> {
    if (!this.hass) return;
    this._diagnosticsLoading = true;
    try {
      this._diagnostics = await this.hass.callWS({ type: "powerpilot/diagnostics" });
      this._error = null;
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    } finally {
      this._diagnosticsLoading = false;
    }
  }

  // ------------------------------------------------------------------
  // Simulations: forecast accuracy (consumption + price)
  // ------------------------------------------------------------------
  private async _loadSimulations(): Promise<void> {
    if (!this.hass) return;
    this._simLoading = true;
    try {
      await this._loadAccuracy();
      this._error = null;
    } catch (err: any) {
      this._error = err?.message ?? String(err);
    } finally {
      this._simLoading = false;
    }
  }

  private async _loadAccuracy(): Promise<void> {
    if (!this.hass) return;
    this._accuracy = await this.hass.callWS({
      type: "powerpilot/accuracy",
      lead_hours: this._accuracyLead,
      days: 7,
    });
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
        ${this._tabButton("flow", "Przepływ")}
        ${this._tabButton("efficiency", "Sprawność")}
        ${this._tabButton("status", "Status")}
        ${this._tabButton("diagnostics", "Diagnostyka")}
        ${this._tabButton("profiles", "Profile")}
        ${this._tabButton("logs", "Logi")}
        ${this._tabButton("debug", "Debug")}
      </div>
      ${this._error ? html`<div class="error">Błąd: ${this._error}</div>` : nothing}
      <div class="content">
        ${this._tab === "overview" ? this._renderOverview() : nothing}
        ${this._tab === "prices" ? this._renderPrices() : nothing}
        ${this._tab === "simulations" ? this._renderSimulations() : nothing}
        ${this._tab === "flow" ? this._renderFlow() : nothing}
        ${this._tab === "efficiency" ? this._renderEfficiency() : nothing}
        ${this._tab === "status" ? this._renderStatus() : nothing}
        ${this._tab === "diagnostics" ? this._renderDiagnostics() : nothing}
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
  private _currentPlanHour(plan: Plan): PlanHour | null {
    const now = Date.now();
    return (
      plan.hours.find((hour) => {
        const start = new Date(hour.start).getTime();
        return start <= now && now < start + 3600_000;
      }) ?? null
    );
  }

  private _renderOverview(): TemplateResult {
    const plan = this._plan;
    if (!plan || !plan.hours?.length) {
      return html`<div class="card empty">Brak danych planu. Poczekaj na pierwsze przeliczenie.</div>`;
    }
    const current = this._currentPlanHour(plan);
    if (!current) {
      return html`<div class="card empty">Brak danych dla bieżącej godziny.</div>`;
    }
    return html`
      <div class="card">
        <div class="stat-row">
          ${this._stat(
            "Tryb falownika",
            INVERTER_MODE_META[current.inverter_mode]?.label ?? current.inverter_mode
          )}
          ${this._stat("Moc ładowania", (current.charge_power_kw ?? 0).toFixed(2) + " kW")}
          ${this._stat("SoC", current.battery_soc.toFixed(0) + " %")}
          ${this._stat("Cena w baterii", current.battery_energy_cost.toFixed(2))}
          ${this._stat("EV", current.ev_charge ? "ładuje" : "—")}
          ${this._stat("Koszt horyzontu", plan.total_cost.toFixed(2) + " PLN")}
        </div>
      </div>
      ${this._renderNavBar()}
      <div class="card">
        <div class="card-title">
          Energia: ↑ sieć/bateria · ↓ zużycie (stack) + tryb falownika + SoC · poniżej: ceny i koszty
        </div>
        <div id="pp-chart-energy" class="apex-chart"></div>
        <div id="pp-chart-prices" class="apex-chart apex-chart-short"></div>
        <div class="legend-row">
          <span>Tryb falownika (znaczniki na dole):</span>
          ${Object.values(INVERTER_MODE_META).map(
            (m) => html`<span><span class="dot-sq" style=${"background:" + m.dot}></span>${m.label}</span>`,
          )}
          <span style="margin-left:12px">Wyjazd EV:</span>
          <span><span class="dot-sq" style=${"background:" + TRIP_FILL.travel}></span>dojazd/powrót (+margines)</span>
          <span><span class="dot-sq" style=${"background:" + TRIP_FILL.event}></span>zdarzenie</span>
        </div>
        ${this._series && !this._series.hours?.length
          ? html`<div class="empty">Brak danych dla wybranego okna (brak prognozy / poza horyzontem).</div>`
          : nothing}
      </div>
    `;
  }

  private _renderNavBar(): TemplateResult {
    const { start, end } = this._computeWindow();
    const isLive = this._anchor === null;
    // `end` is the exclusive midnight after the window; the last day shown is
    // the day before it.
    const lastDay = new Date(end.getTime() - 24 * 3600 * 1000);
    const fmtDay = (d: Date) =>
      d.toLocaleDateString("pl-PL", { day: "2-digit", month: "2-digit", year: "numeric" });
    // Picker shows the selected day (or today when live) as <input type="date">
    // expects: "YYYY-MM-DD" in local time.
    const pad = (n: number) => String(n).padStart(2, "0");
    const dayValue = (() => {
      const d = this._anchor ?? new Date();
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    })();
    // Upper bound: today (the future has no realized data).
    const todayMax = (() => {
      const d = new Date();
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    })();
    return html`
      <div class="card nav-card">
        <div class="nav-row">
          <button class="nav-btn" @click=${() => this._shiftDay(-1)} title="−12 h">«</button>
          <input
            type="date"
            class="nav-date"
            .value=${dayValue}
            max=${todayMax}
            @click=${this._openPicker}
            @change=${this._onDatePick}
          />
          <button class="nav-btn" @click=${() => this._shiftDay(1)} title="+12 h">»</button>
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
        <div class="nav-row nav-row-secondary">
          <span class="nav-label">Prognoza:</span>
          ${FORECAST_LEADS.map(
            (l) => html`
              <button
                class="nav-btn ${!this._forecastRunAt && this._forecastLead === l.hours
                  ? "active"
                  : ""}"
                @click=${() => this._setForecastLead(l.hours)}
                title="Porównaj z prognozą sprzed ${l.hours} h"
              >
                ${l.label}
              </button>
            `
          )}
          <button
            class="nav-btn"
            @click=${() => this._shiftForecastPin(-1)}
            title="Prognoza godzinę wcześniej"
          >
            ‹
          </button>
          <input
            type="datetime-local"
            class="nav-date ${this._forecastRunAt ? "active" : ""}"
            step="3600"
            .value=${this._forecastPinInputValue()}
            @click=${this._openPicker}
            @change=${this._onForecastPinPick}
            title="Pokaż prognozę obowiązującą o wskazanej godzinie (na wykresie zaznaczony jej zakres)"
          />
          <button
            class="nav-btn"
            @click=${() => this._shiftForecastPin(1)}
            title="Prognoza godzinę później"
          >
            ›
          </button>
          ${this._forecastRunAt
            ? html`<button
                class="nav-btn"
                @click=${() => {
                  this._forecastRunAt = null;
                  this._refresh();
                }}
                title="Wróć do prognozy wybranej przyciskami"
              >
                ✕
              </button>`
            : nothing}
        </div>
        <div class="nav-info">
          Okno:
          ${start.getHours() === 0
            ? html`<strong>${fmtDay(start)}</strong> → <strong>${fmtDay(lastDay)}</strong>`
            : html`<strong>${fmtDay(start)} ${pad(start.getHours())}:00</strong> →
                <strong>${fmtDay(end)} ${pad(end.getHours())}:00</strong>`}
          ${isLive
            ? html`<span class="muted"> · tryb live</span>`
            : html`<span class="muted"> · wybrany dzień</span>`}
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
    if (this._accuracyChart || this._priceAccuracyChart || this._biasChart) {
      this._accuracyChart?.destroy();
      this._priceAccuracyChart?.destroy();
      this._biasChart?.destroy();
      this._accuracyChart = undefined;
      this._priceAccuracyChart = undefined;
      this._biasChart = undefined;
    }
  }

  private _mountSimCharts(): void {
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

    const priceEl = this.renderRoot.querySelector("#pp-chart-price-accuracy") as HTMLElement | null;
    if (priceEl && acc) {
      const opts = this._buildPriceAccuracyOptions(acc);
      if (this._priceAccuracyChart) this._priceAccuracyChart.updateOptions(opts, false, false);
      else {
        this._priceAccuracyChart = new ApexCharts(priceEl, opts);
        this._priceAccuracyChart.render();
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
    if (!s || !s.hours?.length) {
      // No data for this window (e.g. a future day beyond the plan horizon):
      // tear down any previously rendered charts so the last window's data
      // doesn't linger on screen.
      if (this._energyChart || this._priceChart) {
        this._energyChart?.destroy();
        this._priceChart?.destroy();
        this._energyChart = undefined;
        this._priceChart = undefined;
        this._lastMountedSeries = undefined;
      }
      return;
    }
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
      // `updateSyncedCharts=false` is CRITICAL: both charts share a sync
      // group, and the default (true) copies these options onto the sibling
      // chart — the energy panel would inherit the price panel's axes (and
      // vice versa) on every refresh or range change.
      this._energyChart.updateOptions(energyOpts, false, false, false);
    } else {
      this._energyChart = new ApexCharts(energyEl, energyOpts);
      this._energyChart.render();
    }
    if (this._priceChart) {
      this._priceChart.updateOptions(priceOpts, false, false, false);
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

  /** Generate xaxis annotations for midnight boundaries within the visible series.
   *  `withLabels=false` draws only the boundary line — used on the lower (price)
   *  panel so the day name isn't printed twice in the merged view. */
  private _dayBoundaryAnnotations(s: Series, withLabels = true): any[] {
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
        ...(withLabels
          ? {
              label: {
                borderColor: "transparent",
                style: { background: "transparent", color: textColor, fontSize: "10px" },
                text: `${DAY_PL[d.getDay()]} ${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}`,
                orientation: "horizontal",
                position: "top",
              },
            }
          : {}),
      });
    }
    return annotations;
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
    const { start: winStartD, end: winEndD } = this._computeWindow();
    const winStart = winStartD.getTime();
    const winEnd = winEndD.getTime();

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
      // Realized when present; otherwise the (possibly vintage-pinned)
      // forecast — a stale forecast lead blanks realized fields so the bars
      // draw the pinned plan instead of reality.
      if (h.is_past && h.consumption_real != null) {
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
    type Row = {
      label: string;
      color: string;
      key: string; // breakdown key on TipSide ("grid"/"base"/… or "dev:<eid>")
      get: (h: SeriesHour) => number | null;
    };
    const deviceIds = s.device_ids ?? [];
    const upRows: Row[] = [
      { label: "Import z sieci", color: "#8e44ad", key: "grid", get: (h) => h.grid_buy_kwh },
      { label: "Bateria — rozładowanie", color: "#b0a14f", key: "discharge", get: (h) => h.battery_discharge_kwh },
    ];
    const downRows: Row[] = [
      { label: "Zużycie bazowe", color: "#b5475d", key: "base", get: baseConsumption },
      ...deviceIds.map((eid, idx) => ({
        label: `Urz: ${eid.split(".").slice(-1)[0]}`,
        color: DEVICE_PALETTE[idx % DEVICE_PALETTE.length],
        key: `dev:${eid}`,
        get: device(eid),
      })),
      { label: "EV ładowanie", color: "#3498db", key: "ev", get: (h) => h.ev_charge_kwh },
      { label: "Bateria — ładowanie", color: "#c98a3a", key: "charge", get: (h) => h.battery_charge_kwh },
    ];

    // √-compressed bar heights: an EV-charge hour (10+ kWh) would otherwise
    // dwarf ordinary consumption (~0.3 kWh) into invisibility. Each stack is
    // drawn with total height √(total) while every component keeps its true
    // share of the bar, so within-bar proportions stay honest. Axis labels
    // are squared back to real kWh and the tooltip reads raw values.
    const upTotalOf = (h: SeriesHour): number =>
      (h.grid_buy_kwh ?? 0) + (h.battery_discharge_kwh ?? 0);
    const downTotalOf = (h: SeriesHour): number =>
      (baseConsumption(h) ?? 0) +
      deviceSum(h) +
      (h.ev_charge_kwh ?? 0) +
      (h.battery_charge_kwh ?? 0);
    const sqrtFactor = (total: number): number =>
      total > 1e-9 ? Math.sqrt(total) / total : 0;

    const series: any[] = [];
    const kwhNames: string[] = [];
    // sign = +1 for supply (up), -1 for consumption (down). Consumption values
    // are negated so they stack below zero on the shared diverging axis.
    const pushKwh = (
      name: string,
      color: string,
      sign: 1 | -1,
      getter: (h: SeriesHour) => number | null,
      totalOf: (h: SeriesHour) => number,
    ) => {
      const signed = (h: SeriesHour) => {
        const v = getter(h);
        return v == null ? null : sign * v * sqrtFactor(totalOf(h));
      };
      series.push({ name, type: "column", data: pairBar(signed), color });
      kwhNames.push(name);
    };

    upRows.forEach((r) => pushKwh(r.label, r.color, 1, r.get, upTotalOf));
    downRows.forEach((r) => pushKwh(r.label, r.color, -1, r.get, downTotalOf));

    // Shared symmetric-ish scale so every per-series axis aligns and the
    // stacked bars line up. Compute the largest up-stack and down-stack.
    // Limits live in √-space to match the compressed bar heights.
    let posMax = 0;
    let negMax = 0;
    for (const h of hrs) {
      posMax = Math.max(posMax, upTotalOf(h));
      negMax = Math.max(negMax, downTotalOf(h));
    }
    const axMax = posMax > 0 ? Math.sqrt(posMax) * 1.08 : 1;
    const axMin = negMax > 0 ? -Math.sqrt(negMax) * 1.08 : -1;

    // SoC line on the right axis. `soc` is the END-of-hour state; plotting it
    // at the hour start would move the line one hour too early (a 17:00
    // discharge would render its drop in the 16→17 segment). The backend also
    // provides `battery_soc_start` — the SoC the battery *enters* each hour
    // with — so the rise/fall lines up with the bar and inverter-mode band of
    // the hour that caused it, including the very first hour of the window.
    const HOUR = 3600 * 1000;
    // The real SoC line is plotted only for past/current hours and stays on
    // regular hour boundaries. Do not add a partial_until point here: ApexCharts
    // uses the closest datetime point when sizing columns, and an in-hour SoC
    // point would make the hourly bars look thin.
    const socData: { x: number; y: number | null }[] = [];
    let lastCompletedSocIdx = -1;
    hrs.forEach((h, i) => {
      if (!h.is_past) return;
      socData.push({ x: ts[i], y: h.battery_soc_start });
      if (!h.partial && h.soc != null) {
        lastCompletedSocIdx = i;
      }
    });
    if (lastCompletedSocIdx >= 0) {
      socData.push({
        x: ts[lastCompletedSocIdx] + HOUR,
        y: hrs[lastCompletedSocIdx].soc,
      });
    }
    if (socData.length) {
      series.push({
        name: "SoC %",
        type: "line",
        data: socData,
        color: "#22c55e",
      });
    }

    // Planned (forecast) battery SoC as a dashed line, so the plan can be read
    // against what really happened. `forecast.soc_end` is the END-of-hour planned
    // state, plotted on the hour-end boundary (t + 1h). Only drawn when some hour
    // carries a forecast SoC (blank for vintages predating flow capture).
    const hasFcSoc = hrs.some((h) => h.forecast?.soc_end != null);
    if (hasFcSoc) {
      series.push({
        name: "SoC prognoza %",
        type: "line",
        data: ts.map((t, i) => ({ x: t + HOUR, y: hrs[i].forecast?.soc_end ?? null })),
        color: "#22c55e",
      });
    }

    // EV SoC on the same right axis. `ev_soc` is the END-of-hour state, plotted
    // on the hour-end boundary (t + 1h) to line up with the right edge of the
    // EV-charge bar. The solid line is REAL, so — like the battery SoC line —
    // it covers only *completed* hours: the in-progress hour is excluded, so
    // both realized lines end on the same boundary (the start of the current
    // hour) instead of the EV line reaching into the not-yet-finished hour.
    // (An exact stop at "teraz" would need an in-hour point, which breaks
    // ApexCharts' column sizing — see the battery SoC note above.)
    const hasEvSoc = hrs.some((h) => h.is_past && h.ev_soc != null);
    if (hasEvSoc) {
      series.push({
        name: "EV SoC %",
        type: "line",
        data: ts.map((t, i) => ({
          x: t + HOUR,
          y: hrs[i].is_past && !hrs[i].partial ? hrs[i].ev_soc : null,
        })),
        color: "#3498db",
      });
    }
    const hasFcEvSoc = hrs.some((h) => h.forecast?.ev_soc_end != null);
    if (hasFcEvSoc) {
      series.push({
        name: "EV SoC prognoza %",
        type: "line",
        data: ts.map((t, i) => ({ x: t + HOUR, y: hrs[i].forecast?.ev_soc_end ?? null })),
        color: "#3498db",
      });
    }

    // Inverter mode as a dot lane along the bottom edge (replaces the old
    // full-height background bands, which collided with the trip shading).
    // One dot per hour at the hour midpoint, pinned low on the SoC axis;
    // per-point colors come from markers.discrete below.
    const MODE_SERIES = "Tryb falownika";
    series.push({
      name: MODE_SERIES,
      type: "line",
      data: ts.map((t, i) => ({
        x: t + HALF_HOUR,
        y: hrs[i].inverter_mode ? 1.5 : null,
      })),
      color: "#94a3b8",
    });
    const modeSeriesIndex = series.length - 1;
    const modeMarkers = hrs.map((h, i) => ({
      seriesIndex: modeSeriesIndex,
      dataPointIndex: i,
      fillColor: h.inverter_mode ? INVERTER_MODE_META[h.inverter_mode]?.dot ?? "transparent" : "transparent",
      strokeColor: "transparent",
      size: h.inverter_mode ? 5 : 0,
      shape: "square",
    }));

    const nowTs = s.now ? new Date(s.now).getTime() : Date.now();
    const dark = this._isDark();
    const nowColor = dark ? "#ffffff" : "#333333";
    const nowBg = dark ? "rgba(255,255,255,0.15)" : "rgba(0,0,0,0.08)";
    return {
      chart: {
        // `group` syncs cursor, tooltip and zoom with the price panel below,
        // so both read as one chart with a shared time axis.
        id: "pp-energy",
        group: "pp-overview",
        type: "line",
        height: 598, // ~30% taller than the original 460
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
        // Mode lane has no line — only its per-hour dots are visible.
        width: series.map((sx: any) =>
          sx.name === MODE_SERIES ? 0 : sx.type === "line" ? 2.5 : 0,
        ),
        // Forecast (planned) SoC / EV SoC dashed so they read apart from the
        // solid realized lines they track.
        dashArray: series.map((sx: any) =>
          typeof sx.name === "string" && sx.name.includes("prognoza") ? 6 : 0,
        ),
        curve: "straight",
      },
      markers: {
        size: series.map((sx: any) => (sx.name === MODE_SERIES ? 5 : 0)),
        shape: "square",
        strokeWidth: 0,
        discrete: modeMarkers,
        hover: { sizeOffset: 1 },
      },
      // Near-full width so each midpoint-plotted bar fills its hour [H, H+1]
      // and its left edge lands on the hour gridline.
      plotOptions: { bar: { columnWidth: "95%", borderRadius: 0 } },
      dataLabels: { enabled: false },
      fill: { opacity: 0.85 },
      series,
      xaxis: {
        type: "datetime",
        // Pin the axis to the whole requested window so the chart always spans
        // midnight→end-of-window, even when data is missing at an edge.
        min: winStart,
        max: winEnd,
        // Time labels live on the price panel directly below — hiding them
        // here glues the two panels into one visual chart. The hover box on
        // this axis goes too (it floated mid-view between the panels).
        labels: { show: false, datetimeUTC: false },
        axisTicks: { show: false },
        tooltip: { enabled: false },
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
          title: { text: "kWh, skala √  (↑ sieć/bateria · ↓ zużycie)" },
          // Ticks are placed in √-space (bar height = √kWh); square them back
          // so the axis reads in real kWh.
          labels: {
            minWidth: 48,
            formatter: (v: number) => (v != null ? (v * v).toFixed(2) : ""),
          },
        },
        {
          seriesName: ["SoC %", "SoC prognoza %", "EV SoC %", "EV SoC prognoza %", MODE_SERIES],
          opposite: true,
          min: 0,
          max: 100,
          title: { text: "SoC (%)" },
          labels: {
            minWidth: 48,
            formatter: (v: number) => (v != null ? v.toFixed(0) + " %" : ""),
          },
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

          // Trip away-windows covering this hour: the on-chart label is
          // truncated, so the tooltip carries the full event name + details
          // (event slot, departure/return incl. margins, distance, energy).
          const esc = (x: string) =>
            x.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] ?? c);
          const fmtHM = (iso: string) => {
            const d = new Date(iso);
            return isNaN(d.getTime())
              ? "—"
              : d.toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
          };
          const hourTs = new Date(h.start).getTime();
          const tripRows = this._chartTrips()
            .filter((t) => {
              const a = new Date(t.depart).getTime();
              const b = new Date(t.return_end).getTime();
              return !isNaN(a) && !isNaN(b) && hourTs >= a && hourTs < b;
            })
            .map((t) => {
              const facts = [
                t.distance_km != null ? `${t.distance_km.toFixed(0)} km` : null,
                t.duration_min != null ? `dojazd ${Math.round(t.duration_min)} min` : null,
                t.energy_kwh != null ? `~${t.energy_kwh.toFixed(1)} kWh` : null,
              ]
                .filter(Boolean)
                .join(" · ");
              return (
                `<div style="font-weight:500;font-size:11px;opacity:0.85;margin-top:3px">🚗 ${esc(t.label)}</div>` +
                `<div style="font-weight:400;font-size:11px;opacity:0.75">` +
                `zdarzenie ${fmtHM(t.event_start)}–${fmtHM(t.event_end)} · wyjazd ${fmtHM(t.depart)} · powrót do ${fmtHM(t.return_end)}` +
                `</div>` +
                (facts
                  ? `<div style="font-weight:400;font-size:11px;opacity:0.75">${facts}</div>`
                  : "")
              );
            })
            .join("");

          // Split breakdown: realized vs forecast side by side, each with the
          // full colored per-position breakdown. Settled past hours carry both
          // (actual vs what was planned); future hours only forecast; the current
          // hour realized-so-far + the plan's forecast for the whole hour.
          const sides: { label: string; g: TipSide }[] = [];
          if (h.realized)
            sides.push({ label: h.partial ? "realne (do teraz)" : "realne", g: h.realized });
          if (h.forecast) {
            // Surface the vintage the forecast came from (date + hour) so the
            // forecast-lead view shows which plan each prognoza line reflects.
            const originStr = h.forecast_origin
              ? new Date(h.forecast_origin).toLocaleString("pl-PL", {
                  day: "2-digit",
                  month: "2-digit",
                  hour: "2-digit",
                  minute: "2-digit",
                })
              : null;
            const fcLabel =
              (h.partial ? "prognoza (godz.)" : "prognoza") +
              (originStr
                ? `<div style="font-weight:400;opacity:0.6;font-size:11px">z ${originStr}</div>`
                : "");
            sides.push({ label: fcLabel, g: h.forecast });
          }
          if (!sides.length) return "";

          const sideVal = (g: TipSide, key: string): number | null => {
            if (key.startsWith("dev:")) {
              const v = g.devices?.[key.slice(4)];
              return v == null ? null : v;
            }
            const v = (g as unknown as Record<string, number | null>)[key];
            return v == null ? null : v;
          };
          const dot = (c: string) =>
            `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${c};margin-right:5px;vertical-align:middle"></span>`;
          // Δ between the realized and forecast columns (only when both are
          // shown). Arrow points at the bigger side: ← forecast was higher,
          // → realized was higher. Neutral hues (blue/amber) on purpose —
          // higher-than-planned isn't inherently "bad" like red would imply.
          const hasDelta = sides.length === 2;
          const diffCell = (real: number | null, fc: number | null): string => {
            if (!hasDelta) return "";
            if (real == null || fc == null)
              return `<td style="padding-left:10px"></td>`;
            const d = real - fc;
            if (Math.abs(d) < 0.005)
              return `<td style="text-align:right;padding-left:10px;opacity:0.45">=</td>`;
            const arrow = d > 0 ? "→" : "←";
            const color = d > 0 ? "#f59e0b" : "#3b82f6";
            return `<td style="text-align:right;padding-left:10px;font-variant-numeric:tabular-nums;color:${color}">${arrow}${fmt(Math.abs(d))}</td>`;
          };
          const valCells = (fn: (g: TipSide) => string) =>
            sides
              .map(
                (s) =>
                  `<td style="text-align:right;font-variant-numeric:tabular-nums;padding-left:14px">${fn(s.g)}</td>`,
              )
              .join("");
          // One component row across the sides; hidden when every side is ~0.
          const compRow = (r: Row) => {
            const vals = sides.map((s) => sideVal(s.g, r.key));
            if (vals.every((v) => v == null || Math.abs(v) < 0.005)) return "";
            return (
              `<tr><td style="padding:1px 0 1px 12px;opacity:0.85">${dot(r.color)}${r.label}</td>` +
              vals
                .map(
                  (v) =>
                    `<td style="text-align:right;font-variant-numeric:tabular-nums;opacity:0.85;padding-left:14px">${
                      v == null ? "—" : fmt(v)
                    }</td>`,
                )
                .join("") +
              diffCell(vals[0] ?? null, vals[1] ?? null) +
              `</tr>`
            );
          };
          const totalRow = (label: string, rows: Row[]) => {
            const totals = sides.map((s) =>
              rows.reduce((a, r) => a + Math.abs(sideVal(s.g, r.key) ?? 0), 0),
            );
            return (
              `<tr><td style="padding:1px 0;font-weight:600">${label}</td>` +
              totals
                .map(
                  (v) =>
                    `<td style="text-align:right;font-variant-numeric:tabular-nums;padding-left:14px;font-weight:600">${fmt(v)} kWh</td>`,
                )
                .join("") +
              diffCell(totals[0] ?? null, totals[1] ?? null) +
              `</tr>`
            );
          };
          // Colored delta arrow so the direction reads at a glance:
          // "31 → 38% ↑7" (green up / red down / dimmed "=" for no change).
          // The delta sits in a fixed-width box so the "%" values line up
          // across rows regardless of how wide each delta is.
          const socDelta = (a: number | null | undefined, b: number | null | undefined) => {
            if (a == null || b == null) return "";
            const d = Math.round(b) - Math.round(a);
            if (d > 0) return `<span style="color:#16a34a;font-weight:600">↑${d}</span>`;
            if (d < 0) return `<span style="color:#dc2626;font-weight:600">↓${-d}</span>`;
            return `<span style="opacity:0.55">=</span>`;
          };
          const socPair = (a: number | null | undefined, b: number | null | undefined) =>
            a == null && b == null
              ? "—"
              : `${a != null ? a.toFixed(0) : "—"} → ${b != null ? b.toFixed(0) : "—"}%` +
                `<span style="display:inline-block;min-width:28px;text-align:right">${socDelta(a, b)}</span>`;
          const socFn = (g: TipSide) => socPair(g.soc_start, g.soc_end);
          const evSocFn = (g: TipSide) => socPair(g.ev_soc_start, g.ev_soc_end);
          const hasEvSoc = sides.some((s) => s.g.ev_soc_start != null || s.g.ev_soc_end != null);
          const sep = `<tr><td colspan="${sides.length + 1 + (hasDelta ? 1 : 0)}" style="padding:4px 0 2px"><div style="border-top:1px solid ${tt.border}"></div></td></tr>`;
          const header =
            sides.length > 1
              ? `<tr><td></td>${sides
                  .map(
                    (s) =>
                      `<td style="text-align:right;opacity:0.7;padding-left:14px">${s.label}</td>`,
                  )
                  .join("")}${hasDelta ? `<td style="text-align:right;opacity:0.7;padding-left:10px">Δ</td>` : ""}</tr>`
              : `<tr><td></td><td style="text-align:right;opacity:0.7">${sides[0].label}</td></tr>`;

          return `
            <div style="padding:8px 10px;color:${tt.fg};font-size:12px;line-height:1.45;min-width:240px">
              <div style="font-weight:600;margin-bottom:6px;border-bottom:1px solid ${tt.border};padding-bottom:4px">${date}${modeStr}${tripRows}</div>
              <table style="border-collapse:collapse;width:100%">
                ${header}
                ${totalRow("↑ Źródła energii", upRows)}
                ${upRows.map(compRow).join("")}
                ${sep}
                ${totalRow("↓ Zużycie", downRows)}
                ${downRows.map(compRow).join("")}
                ${sep}
                <tr><td style="padding:1px 0">SoC</td>${valCells(socFn)}</tr>
                ${hasEvSoc ? `<tr><td style="padding:1px 0">SoC EV</td>${valCells(evSocFn)}</tr>` : ""}
                ${
                  h.charge_power_kw != null && h.charge_power_kw > 0.005
                    ? `<tr><td style="padding:1px 0">Moc ładowania (sieć)</td><td colspan="${sides.length}" style="text-align:right;font-variant-numeric:tabular-nums;padding-left:14px">${fmt(h.charge_power_kw)} kW</td></tr>`
                    : ""
                }
              </table>
            </div>
          `;
        },
      },
      legend: {
        // Top, so the bottom edge of this panel can sit flush against the
        // price panel below.
        position: "top",
        horizontalAlign: "center",
        itemMargin: { horizontal: 14, vertical: 2 },
        fontSize: "12px",
        showForSingleSeries: true,
        showForZeroSeries: false,
        showForNullSeries: false,
      },
      annotations: {
        xaxis: [
          ...this._dayBoundaryAnnotations(s),
          ...this._evForcedAnnotations(),
          ...this._evAwayAnnotations(),
          ...this._forecastPinAnnotations(s),
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
        points: this._evDeadlineAnnotations(),
      },
    };
  }

  /** Coverage of the pinned prognoza vintage: a shaded band over the hours
   *  that plan actually covered (inside it the tooltip carries realne AND
   *  prognoza; outside only realne) plus a line at the moment the plan was
   *  made — so it's readable at a glance which forecast is on screen and
   *  from when to when it applied. */
  private _forecastPinAnnotations(s: Series): any[] {
    const pin = s.forecast_pin;
    if (!pin) return [];
    const from = new Date(pin.start).getTime();
    const to = new Date(pin.end).getTime();
    const made = new Date(pin.run_at).getTime();
    if (isNaN(from) || isNaN(to) || to <= from) return [];
    const color = "#8e44ad";
    const d = new Date(isNaN(made) ? from : made);
    const pad = (n: number) => String(n).padStart(2, "0");
    const madeTxt = `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:00`;
    return [
      { x: from, x2: to, fillColor: color, opacity: 0.06 },
      {
        x: isNaN(made) ? from : made,
        borderColor: color,
        strokeDashArray: 6,
        label: {
          borderColor: color,
          style: { background: color, color: "#ffffff", fontSize: "10px" },
          text: `prognoza z ${madeTxt}`,
          position: "top",
        },
      },
    ];
  }

  /** Calendar deadline targets ("Kotek 100%") and automatic pre-trip minimums
   *  as points on the SoC axis, drawn at (deadline, target SoC%) so the chart
   *  shows where the car must reach a given charge level. Trip targets (a
   *  floor, not a ceiling) are drawn in the away-window red. */
  private _evDeadlineAnnotations(): any[] {
    const targets = this._status?.ev?.targets ?? [];
    return targets
      .map((t) => ({
        ts: new Date(t.deadline).getTime(),
        soc: t.target_soc,
        trip: t.source === "trip",
      }))
      .filter((t) => !isNaN(t.ts))
      .map((t) => {
        // Trip minimums in the event gray (they belong to the away window);
        // manual calendar targets keep the EV blue.
        const color = t.trip ? TRIP_FILL.event : "#3498db";
        return {
          x: t.ts,
          y: t.soc,
          yAxisIndex: 1,
          marker: { size: 6, fillColor: color, strokeColor: "#ffffff", strokeWidth: 2 },
          label: {
            text: t.trip ? `🚗 min ${Math.round(t.soc)}%` : `🚗 ${t.soc}%`,
            borderColor: color,
            style: { background: color, color: "#ffffff", fontSize: "11px" },
          },
        };
      });
  }

  /** Trip away-windows as shaded x-axis regions — the car is not home,
   *  charging can't happen. Each trip splits into up to three bands: travel
   *  out (depart→event start, incl. margin), the event itself, and travel
   *  back (event end→return, incl. margin), so the calendar slot reads apart
   *  from the drive-time padding around it. */
  /** Trips backing the chart: the series payload carries live + snapshot
   *  history (and, at a stale lead, the pinned vintage's view); the status
   *  list is only a fallback while the series is still loading. */
  private _chartTrips(): EVTrip[] {
    return this._series?.trips ?? this._status?.ev?.trips ?? [];
  }

  private _evAwayAnnotations(): any[] {
    const regions: any[] = [];
    for (const t of this._chartTrips()) {
      const depart = new Date(t.depart).getTime();
      const ret = new Date(t.return_end).getTime();
      if (isNaN(depart) || isNaN(ret) || ret <= depart) continue;
      // Clamp event bounds into the away window; fall back to one event-colored
      // band when the calendar times are missing/degenerate.
      let evStart = new Date(t.event_start).getTime();
      let evEnd = new Date(t.event_end).getTime();
      if (isNaN(evStart) || isNaN(evEnd) || evEnd <= evStart) {
        evStart = depart;
        evEnd = ret;
      }
      evStart = Math.max(depart, Math.min(evStart, ret));
      evEnd = Math.max(evStart, Math.min(evEnd, ret));

      // Truncated horizontal label on the event band only; the tooltip shows
      // the full event name + details for every hour inside the window.
      const label = {
        text: `🚗 ${t.label.length > 22 ? t.label.slice(0, 21) + "…" : t.label}`,
        orientation: "horizontal",
        position: "top",
        offsetY: -6,
        style: { background: TRIP_FILL.event, color: "#ffffff", fontSize: "10px" },
      };
      if (evStart > depart)
        regions.push({ x: depart, x2: evStart, fillColor: TRIP_FILL.travel, opacity: 0.16 });
      regions.push({ x: evStart, x2: evEnd, fillColor: TRIP_FILL.event, opacity: 0.16, label });
      if (ret > evEnd)
        regions.push({ x: evEnd, x2: ret, fillColor: TRIP_FILL.travel, opacity: 0.16 });
    }
    return regions;
  }

  /** Manual ("forced") calendar windows as shaded x-axis regions. */
  private _evForcedAnnotations(): any[] {
    const HOUR = 3600 * 1000;
    const hours = (this._status?.ev?.forced_hours ?? [])
      .map((iso) => new Date(iso).getTime())
      .filter((t) => !isNaN(t))
      .sort((a, b) => a - b);
    if (!hours.length) return [];
    // Merge consecutive 1-hour slots into one region.
    const regions: { x: number; x2: number }[] = [];
    let start = hours[0];
    let prev = hours[0];
    for (let i = 1; i <= hours.length; i++) {
      const cur = hours[i];
      if (cur !== prev + HOUR) {
        regions.push({ x: start, x2: prev + HOUR });
        if (cur != null) start = cur;
      }
      prev = cur;
    }
    return regions.map((r) => ({
      x: r.x,
      x2: r.x2,
      fillColor: "#3498db",
      opacity: 0.1,
      label: {
        text: "EV",
        position: "top",
        style: { background: "#3498db", color: "#ffffff", fontSize: "10px" },
      },
    }));
  }

  /** Build ApexCharts options for the price chart (PLN/kWh line + PLN/h bars). */
  private _buildPriceOptions(s: Series): any {
    const hrs = s.hours;
    const ts = hrs.map((h) => new Date(h.start).getTime());
    const { start: winStartD, end: winEndD } = this._computeWindow();
    const winStart = winStartD.getTime();
    const winEnd = winEndD.getTime();

    const HOUR = 3600 * 1000;
    const HALF_HOUR = 1800 * 1000;

    // The PLN/kWh axis is √-compressed (like the kWh axis on the energy panel):
    // the decisive differences live in the 0.2–0.8 range where the price lines
    // cross "Cena w baterii", while rare 2–3 PLN spikes would flatten them.
    // Signed so occasional negative RDN prices keep working; axis labels square
    // back to real PLN/kWh and the tooltip reads raw row values.
    const sqrtP = (v: number | null): number | null =>
      v == null ? null : Math.sign(v) * Math.sqrt(Math.abs(v));

    // The total-price stepline is split into 3 series by price provenance
    // (pewna/prognoza/szacowana — same colors as the Ceny tab). Each hour's
    // point lands on the matching series; a segment's last hour also bridges
    // its value onto the boundary point of the next (differently-typed) hour,
    // so every hour keeps a full-width plateau and segments touch.
    const typeOf = (h: SeriesHour): PriceType =>
      h.price_type ?? (h.price_confirmed ? "certain" : "forecast");
    const typedStep = (type: PriceType) => {
      const data: { x: number; y: number | null }[] = [];
      hrs.forEach((h, i) => {
        const cur = typeOf(h) === type ? h.total_price_kwh : null;
        const prev =
          i > 0 && typeOf(hrs[i - 1]) === type ? hrs[i - 1].total_price_kwh : null;
        data.push({ x: ts[i], y: sqrtP(cur ?? prev) });
      });
      if (hrs.length) {
        const last = hrs[hrs.length - 1];
        data.push({
          x: ts[ts.length - 1] + HOUR,
          y: typeOf(last) === type ? sqrtP(last.total_price_kwh) : null,
        });
      }
      return data;
    };
    // Battery-cost stepline: the hour's value holds flat across the whole hour,
    // with an extra point extending the last step to the end of its hour.
    const batCostData = (() => {
      const data = ts.map((t, i) => ({ x: t, y: sqrtP(hrs[i].battery_energy_cost) }));
      if (hrs.length)
        data.push({
          x: ts[ts.length - 1] + HOUR,
          y: sqrtP(hrs[hrs.length - 1].battery_energy_cost),
        });
      return data;
    })();

    // Two PLN/h stacked columns: cost served from the grid vs cost served
    // from the battery. Sum = total cost of meeting demand this hour.
    // Same treatment as the energy panel: bars are plotted at the hour
    // midpoint so they span [H, H+1], and heights are √-compressed (EV-charge
    // hours cost 10-50× a normal hour) with per-component shares preserved.
    // Axis labels square back to real PLN; the tooltip reads raw values.
    const costTotalOf = (h: SeriesHour): number =>
      (h.hour_cost ?? 0) + (h.battery_use_cost ?? 0);
    const costSqrt = (h: SeriesHour, v: number | null): number | null => {
      if (v == null) return null;
      const total = costTotalOf(h);
      return total > 1e-9 ? v * (Math.sqrt(total) / total) : v;
    };
    const gridCostData = ts.map((t, i) => ({ x: t + HALF_HOUR, y: costSqrt(hrs[i], hrs[i].hour_cost) }));
    const batUseCostData = ts.map((t, i) => ({ x: t + HALF_HOUR, y: costSqrt(hrs[i], hrs[i].battery_use_cost) }));

    // Explicit axis bounds computed from the data, in √-space. Apex's
    // forceNiceScale rounds the √ maximum up to a "nice" number (e.g. 1.4 → 3),
    // which squares back to a 9 PLN/kWh axis for 2 PLN data and flattens
    // everything into the bottom quarter of the panel.
    let priceSqMax = 0;
    let priceSqMin = 0;
    let costMax = 0;
    for (const h of hrs) {
      for (const v of [h.total_price_kwh, h.battery_energy_cost]) {
        const s = sqrtP(v);
        if (s != null) {
          priceSqMax = Math.max(priceSqMax, s);
          priceSqMin = Math.min(priceSqMin, s);
        }
      }
      costMax = Math.max(costMax, costTotalOf(h));
    }
    const priceAxMax = priceSqMax > 0 ? priceSqMax * 1.06 : 1;
    const priceAxMin = priceSqMin < 0 ? priceSqMin * 1.06 : 0;
    const costAxMax = costMax > 0 ? Math.sqrt(costMax) * 1.08 : 1;

    const dark = this._isDark();
    const priceSeriesNames: string[] = [];
    const series: any[] = [
      // One stepline per price provenance, colored like the Ceny tab badges.
      ...(["certain", "forecast", "estimated"] as PriceType[]).map((t) => {
        const name = `Cena ${PRICE_TYPE_META[t].label}`;
        priceSeriesNames.push(name);
        return {
          name,
          type: "line",
          data: typedStep(t),
          color: PRICE_TYPE_META[t].color,
        };
      }),
      // Near-black (near-white in dark mode) — teal read as "blue" and sank
      // into the blue battery-cost columns it usually overlaps.
      {
        name: "Cena w baterii",
        type: "line",
        data: batCostData,
        color: dark ? "#e5e7eb" : "#111827",
      },
      { name: "Koszt energii - sieć", type: "column", data: gridCostData, color: "#e67e22" },
      { name: "Koszt energii - bateria", type: "column", data: batUseCostData, color: "#3b82f6" },
    ];

    const nowTs = s.now ? new Date(s.now).getTime() : Date.now();
    const nowColor = dark ? "#ffffff" : "#333333";
    return {
      chart: {
        // Same sync group as the energy panel above: shared cursor, tooltip
        // and zoom, so both panels behave like one chart.
        id: "pp-prices",
        group: "pp-overview",
        type: "line",
        height: 260,
        stacked: true,
        animations: { enabled: false },
        // No second toolbar — zooming/panning the energy panel syncs here.
        toolbar: { show: false },
        zoom: { enabled: true, type: "x" },
        background: "transparent",
      },
      theme: { mode: dark ? "dark" : "light" },
      stroke: {
        // 3 typed price lines + battery-cost line + 2 columns. Steplines: the
        // price holds flat for its whole hour instead of ramping between
        // hour starts.
        width: [3, 3, 3, 2, 0, 0],
        curve: ["stepline", "stepline", "stepline", "stepline", "straight", "straight"],
        dashArray: [0, 0, 0, 3, 0, 0],
      },
      plotOptions: { bar: { columnWidth: "95%", borderRadius: 0 } },
      dataLabels: { enabled: false },
      fill: { opacity: [1, 1, 1, 1, 0.75, 0.7] },
      series,
      xaxis: {
        type: "datetime",
        min: winStart,
        max: winEnd,
        labels: {
          datetimeUTC: false,
          format: this._rangeMode === "24h" ? "HH:mm" : "dd.MM HH:mm",
        },
      },
      yaxis: [
        {
          // Every PLN/kWh line shares one scale so equal values align visually.
          // Values live in √-space (see sqrtP) — square ticks back to PLN/kWh.
          // Bounds are explicit (data max + 6%): forceNiceScale would round the
          // √ max up and waste most of the panel above the lines.
          seriesName: [...priceSeriesNames, "Cena w baterii"],
          title: { text: "PLN/kWh, skala √" },
          min: priceAxMin,
          max: priceAxMax,
          tickAmount: 6,
          forceNiceScale: false,
          labels: {
            minWidth: 48,
            formatter: (v: number) =>
              v != null ? (Math.sign(v) * v * v).toFixed(2) : "",
          },
          decimalsInFloat: 2,
        },
        {
          seriesName: ["Koszt energii - sieć", "Koszt energii - bateria"],
          opposite: true,
          title: { text: "PLN/h, skala √" },
          // Column heights live in √-space — square tick values back to PLN.
          // Same explicit-bounds treatment as the price axis.
          min: 0,
          max: costAxMax,
          tickAmount: 6,
          forceNiceScale: false,
          labels: {
            minWidth: 48,
            formatter: (v: number) => (v != null ? (v * v).toFixed(2) : ""),
          },
        },
      ],
      tooltip: {
        shared: true,
        intersect: false,
        followCursor: false,
        // NOTE: ApexCharts datetime tokens, not date-fns — "EEEE" would
        // render literally in the x-axis hover box.
        x: { format: "dd.MM HH:mm" },
        // Custom HTML so price lines can show the energy/distribution split
        // that's encoded in the total. ApexCharts passes the data index of
        // the hovered point; we use it to look the slot back up.
        custom: ({ dataPointIndex }: { dataPointIndex: number }) => {
          const row = hrs[dataPointIndex];
          if (!row) return "";
          const fmtPrice = (v: number | null) => (v == null ? "—" : v.toFixed(2));
          const fmt2 = (v: number | null) => (v == null ? "—" : v.toFixed(2));
          const start = new Date(row.start);
          const date = start.toLocaleString("pl-PL", {
            weekday: "short",
            day: "2-digit",
            month: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          });
          // Price provenance badge (pewna/prognoza/szacowana), colored like
          // the line series and the Ceny tab.
          const ptMeta =
            PRICE_TYPE_META[
              row.price_type ?? (row.price_confirmed ? "certain" : "forecast")
            ];
          const typeBadge = `<span style="display:inline-block;padding:0 5px;border-radius:3px;background:${ptMeta.color};color:#fff;font-size:11px">${ptMeta.label}</span>`;
          const tt = this._isDark()
            ? { bg: "#1f2937", fg: "#f3f4f6", border: "#374151" }
            : { bg: "#ffffff", fg: "#1f2937", border: "#d1d5db" };
          return `
            <div style="padding:8px 10px;color:${tt.fg};font-size:12px;line-height:1.4;min-width:240px">
              <div style="font-weight:600;margin-bottom:6px;border-bottom:1px solid ${tt.border};padding-bottom:4px">${date}</div>
              <table style="border-collapse:collapse;width:100%">
                <tr><td style="padding:1px 0">Cena całkowita ${typeBadge}</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmtPrice(row.total_price_kwh)} PLN/kWh</td></tr>
                <tr><td style="padding:1px 0 1px 10px;opacity:0.8">· energia</td><td style="text-align:right;opacity:0.8;font-variant-numeric:tabular-nums">${fmtPrice(row.buy_price)} PLN/kWh</td></tr>
                <tr><td style="padding:1px 0 1px 10px;opacity:0.8">· dystrybucja (z VAT)</td><td style="text-align:right;opacity:0.8;font-variant-numeric:tabular-nums">${fmtPrice(row.distribution_price_kwh)} PLN/kWh</td></tr>
                ${row.fixed_cost ? `<tr><td style="padding:1px 0 1px 10px;opacity:0.8">· koszt stały (z VAT)</td><td style="text-align:right;opacity:0.8;font-variant-numeric:tabular-nums">${fmtPrice(row.fixed_cost)} PLN/h</td></tr>` : ""}
                <tr><td style="padding:1px 0">Cena w baterii</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmtPrice(row.battery_energy_cost)} PLN/kWh</td></tr>
                <tr><td colspan="2" style="padding:4px 0 2px"><div style="border-top:1px solid ${tt.border}"></div></td></tr>
                <tr><td style="padding:1px 0">Koszt z sieci</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmt2(row.hour_cost)} PLN</td></tr>
                <tr><td style="padding:1px 0">Koszt z baterii</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmt2(row.battery_use_cost)} PLN</td></tr>
                <tr><td style="padding:1px 0">Koszt stały</td><td style="text-align:right;font-variant-numeric:tabular-nums">${fmt2(row.fixed_cost)} PLN/h</td></tr>
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
          // Boundary lines only — the day names are printed on the energy
          // panel above; the "teraz" line likewise goes unlabelled here.
          ...this._dayBoundaryAnnotations(s, false),
          // Pinned-prognoza coverage band (unlabelled twin of the energy
          // panel's — prices/costs below also come from the pinned plan).
          ...this._forecastPinAnnotations(s).filter((a) => a.x2 != null),
          {
            x: nowTs,
            borderColor: nowColor,
            strokeDashArray: 4,
          },
        ],
      },
    };
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
    const fmtPrice = (v: number | null) => (v == null ? "—" : v.toFixed(2));
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
          @click=${this._openPicker}
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
    const fullPrices = rows
      .map((h) => h.total_price_kwh)
      .filter((v): v is number => v !== null && v !== undefined);
    const fullPriceHeat =
      fullPrices.length > 0
        ? { min: Math.min(...fullPrices), max: Math.max(...fullPrices) }
        : null;

    // Day-level accuracy vs the settled (certain) rows: how far off the last
    // pre-settlement forecast and the weekly-model estimate were on average.
    const mae = (get: (h: PriceArchiveHour) => number | null): number | null => {
      const errs = rows
        .filter((h) => h.type === "certain" && h.energy_price_kwh != null)
        .map((h) => {
          const v = get(h);
          return v == null ? null : Math.abs(v - (h.energy_price_kwh as number));
        })
        .filter((v): v is number => v != null);
      if (!errs.length) return null;
      return errs.reduce((a, b) => a + b, 0) / errs.length;
    };
    const maeForecast = mae((h) => h.forecast_energy_kwh);
    const maeEstimate = mae((h) => h.estimate_energy_kwh);

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
                  <th>Prognoza<br /><span class="muted">energia + błąd</span></th>
                  <th>Szacowana<br /><span class="muted">energia + błąd</span></th>
                  <th>TGE<br /><span class="muted">netto</span></th>
                  <th>Marża<br /><span class="muted">netto</span></th>
                  <th>Dystrybucja<br /><span class="muted">netto</span></th>
                  <th>Podatki<br /><span class="muted">akcyza+VAT</span></th>
                  <th>Cena pełna<br /><span class="muted">z VAT</span></th>
                </tr>
              </thead>
              <tbody>
                ${rows.map((h) =>
                  this._renderPriceRow(h, fmtHour, fmtStamp, fmtPrice, fullPriceHeat)
                )}
              </tbody>
            </table>
          </div>
          <div class="prices-legend">
            ${(["certain", "forecast", "estimated"] as PriceType[]).map(
              (t) => html`<span class="badge" style=${"background:" + PRICE_TYPE_META[t].color}>${PRICE_TYPE_META[t].label}</span>`
            )}
            <span class="muted">TGE / marża / dystrybucja są netto; „podatki” = akcyza + VAT, „cena pełna” = brutto. Opłata stała (abonamentowa) rozliczana osobno, poza ceną/kWh. „szacowana” = średnia ważona z 3 ostatnich tygodni — najedź na typ, by zobaczyć obliczenie.</span>
          </div>
          <div class="prices-legend">
            <span class="muted">
              Kolumny „Prognoza” i „Szacowana” pokazują cenę energii (brutto), jaką dawał dany
              model, z błędem względem wiersza: dla godzin pewnych — ostatnia prognoza źródła
              sprzed publikacji RDN (zwykle z D−1 rano) oraz szacunek tygodniowy; dla godzin
              prognozowanych — tylko szacunek (Δ vs bieżąca prognoza).
              ${maeForecast != null || maeEstimate != null
                ? html`Średni błąd dnia vs ceny pewne: prognoza
                    <b>${maeForecast != null ? maeForecast.toFixed(3) : "—"}</b>, szacunek
                    <b>${maeEstimate != null ? maeEstimate.toFixed(3) : "—"}</b> PLN/kWh.`
                : nothing}
            </span>
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
    fmtPrice: (v: number | null) => string,
    fullPriceHeat: { min: number; max: number } | null
  ): TemplateResult {
    const meta = h.type ? PRICE_TYPE_META[h.type] : null;
    const sourceLabel = h.source ? PRICE_SOURCE_LABEL[h.source] ?? h.source : "—";
    const badge = meta
      ? html`<span class="badge" style=${"background:" + meta.color} title=${this._priceTooltip(h)}>${meta.label}</span>`
      : html`<span class="muted">—</span>`;
    const fmtNet = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(3));
    // Rows fetched under the new pricing model carry the net breakdown; legacy
    // and estimated rows only have the lumped energy/distribution values.
    const hasBreakdown = h.tge_kwh != null;
    const taxesTitle =
      hasBreakdown && h.excise_kwh != null
        ? `akcyza ${h.excise_kwh.toFixed(3)} + VAT ${h.vat_rate != null ? Math.round(h.vat_rate * 100) : ""}%`
        : "";
    const fullPriceStyle =
      fullPriceHeat && h.total_price_kwh != null
        ? this._priceHeatStyle(h.total_price_kwh, fullPriceHeat.min, fullPriceHeat.max)
        : "";

    // "Prognoza" / "Szacowana" comparators: the model's energy price with its
    // signed error vs this row's energy price. A certain row compares both
    // models; a forecast row only the estimate (it *is* the forecast itself).
    const comparator = (value: number | null | undefined, title = "") => {
      if (value == null || h.energy_price_kwh == null)
        return html`<span class="muted">—</span>`;
      const d = value - h.energy_price_kwh;
      const deltaStr =
        Math.abs(d) < 0.0005
          ? html`<span class="muted">=</span>`
          : html`<span style=${"color:" + (d > 0 ? "#f59e0b" : "#3b82f6")}>
              ${(d > 0 ? "+" : "−") + Math.abs(d).toFixed(3)}
            </span>`;
      return html`<span title=${title}>${value.toFixed(3)} ${deltaStr}</span>`;
    };
    const forecastCell =
      h.type === "certain"
        ? comparator(
            h.forecast_energy_kwh,
            h.forecast_fetched_at
              ? `ostatnia prognoza przed RDN, pobrana ${fmtStamp(h.forecast_fetched_at)}`
              : "ostatnia prognoza przed RDN"
          )
        : html`<span class="muted">—</span>`;
    const estimateCell =
      h.type === "certain" || h.type === "forecast"
        ? comparator(h.estimate_energy_kwh, "szacunek: średnia ważona z 3 tygodni")
        : html`<span class="muted">—</span>`;

    return html`
      <tr>
        <td>${fmtHour(h.start)}</td>
        <td>${badge}</td>
        <td class="muted">${sourceLabel}</td>
        <td class="muted">${fmtStamp(h.fetched_at)}</td>
        <td>${forecastCell}</td>
        <td>${estimateCell}</td>
        <td>${hasBreakdown ? fmtNet(h.tge_kwh) : html`<span class="muted">—</span>`}</td>
        <td>${hasBreakdown ? fmtNet(h.markup_kwh) : html`<span class="muted">—</span>`}</td>
        <td>${fmtNet(hasBreakdown ? h.distribution_net_kwh : h.distribution_price_kwh)}</td>
        <td class="muted" title=${taxesTitle}>${hasBreakdown ? fmtPrice(h.taxes_kwh ?? null) : html`<span class="muted">—</span>`}</td>
        <td class="bold price-full-cell" style=${fullPriceStyle}>${fmtPrice(h.total_price_kwh)}</td>
      </tr>
    `;
  }

  private _priceHeatStyle(value: number, min: number, max: number): string {
    return `background:${this._heatColor(value, min, max)};color:#fff`;
  }

  /** Hover text explaining how a row's price was derived. */
  private _priceTooltip(h: PriceArchiveHour): string {
    if (h.type === "certain") {
      return "Cena pewna (wiążąca RDN) — nie zmienia się już.";
    }
    if (h.type === "forecast") {
      const band =
        h.p10 != null && h.p90 != null
          ? ` Przedział P10–P90: ${h.p10.toFixed(2)}–${h.p90.toFixed(2)} PLN/kWh.`
          : "";
      return `Prognoza ze źródła — odświeżana co kilka godzin.${band}`;
    }
    if (h.type === "estimated" && h.estimate_breakdown) {
      const lines = h.estimate_breakdown.map((s) => {
        const v = s.value == null ? "brak" : `${s.value.toFixed(2)} PLN/kWh`;
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
  // Simulations (forecast accuracy: consumption + price)
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
    const acc = this._accuracy;
    if (!acc) {
      return html`<div class="card empty">
        ${this._simLoading
          ? "Ładowanie…"
          : "Brak danych trafności. Optymalizator zapisuje jeden snapshot planu na godzinę — wróć tu za jakiś czas."}
      </div>`;
    }
    const fmt = (v: number | null, d = 3) => (v == null ? "—" : v.toFixed(d));

    return html`
      <div class="card">
        <div class="card-title">Trafność prognoz — przewidywanie vs rzeczywistość</div>
        <div class="sim-lead">
          <span class="muted">Wyprzedzenie:</span>
          ${[6, 12, 24, 48, 72].map(
            (l) => html`<button
              class="nav-btn ${this._accuracyLead === l ? "active" : ""}"
              @click=${() => this._setAccuracyLead(l)}
            >
              ${l} h
            </button>`
          )}
        </div>
        <div class="muted sim-hint">
          „Wyprzedzenie ${this._accuracyLead} h" = dla każdej minionej godziny bierzemy plan
          zapisany ${this._accuracyLead} h przed nią i porównujemy jego przewidywania z tym, co
          faktycznie się stało (ostatnie 7 dni). Prognoza zużycia opiera się na profilu
          tygodniowym i zmienia się powoli — różnice między wyprzedzeniami widać głównie w
          cenach, które po drodze przechodzą z szacowanych przez prognozowane do pewnych.
        </div>
        <div class="kpi-grid">
          ${this._kpi("Zużycie — próbki", `${acc.samples}`, null, "godzin z parą prognoza+realne")}
          ${this._kpi("Zużycie — MAE", `${fmt(acc.mae)} kWh`, null, "średni błąd bezwzględny")}
          ${this._kpi(
            "Zużycie — bias",
            `${fmt(acc.bias)} kWh`,
            null,
            "ujemny = niedoszacowanie"
          )}
          ${this._kpi("Cena — próbki", `${acc.price_samples}`, null, "vs ceny pewne (RDN)")}
          ${this._kpi("Cena — MAE", `${fmt(acc.price_mae, 3)} PLN/kWh`, null, "średni błąd bezwzględny")}
          ${this._kpi(
            "Cena — bias",
            `${fmt(acc.price_bias, 3)} PLN/kWh`,
            null,
            "ujemny = prognoza za niska"
          )}
        </div>
      </div>

      <div class="card">
        <div class="card-title">Zużycie: prognoza (sprzed ${this._accuracyLead} h) vs realne</div>
        <div id="pp-chart-accuracy" class="apex-chart"></div>
        <div class="card-title" style="margin-top:8px;">
          Cena energii: prognoza (sprzed ${this._accuracyLead} h) vs pewna
        </div>
        <div id="pp-chart-price-accuracy" class="apex-chart"></div>
        <div class="card-title" style="margin-top:8px;">Błąd zużycia wg godziny doby (kWh)</div>
        <div id="pp-chart-bias" class="apex-chart apex-chart-short"></div>
      </div>
    `;
  }

  private _buildPriceAccuracyOptions(acc: Accuracy): any {
    const dark = this._isDark();
    const grid = dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)";
    const fg = dark ? "rgba(255,255,255,0.7)" : "rgba(0,0,0,0.7)";
    const pts = (get: (h: AccuracyHour) => number | null) =>
      acc.hours.map((h) => ({ x: new Date(h.start).getTime(), y: get(h) }));
    return {
      chart: { type: "line", height: 280, background: "transparent", toolbar: { show: false }, animations: { enabled: false } },
      theme: { mode: dark ? "dark" : "light" },
      series: [
        { name: "Cena — prognoza", color: "#3498db", data: pts((h) => h.predicted_price) },
        { name: "Cena — pewna (RDN)", color: "#43a047", data: pts((h) => h.actual_price) },
      ],
      stroke: { width: [2, 2], dashArray: [5, 0], curve: "stepline" },
      colors: ["#3498db", "#43a047"],
      xaxis: { type: "datetime", labels: { datetimeUTC: false, style: { colors: fg } } },
      yaxis: { title: { text: "PLN/kWh", style: { color: fg } }, labels: { style: { colors: fg } }, decimalsInFloat: 2 },
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
  private _evBool(value: boolean | null, yes: string, no: string): string {
    if (value === null || value === undefined) return "—";
    return value ? yes : no;
  }

  /** Merge consecutive 1-hour forced slots into "HH:MM–HH:MM" ranges. */
  private _evForcedRanges(hours: string[]): string[] {
    const sorted = [...hours]
      .map((iso) => new Date(iso))
      .filter((d) => !isNaN(d.getTime()))
      .sort((a, b) => a.getTime() - b.getTime());
    if (!sorted.length) return [];
    const fmt = (d: Date) =>
      d.toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
    const fmtDay = (d: Date) =>
      d.toLocaleDateString("pl-PL", { day: "2-digit", month: "2-digit" });
    const ranges: string[] = [];
    let start = sorted[0];
    let prev = sorted[0];
    for (let i = 1; i <= sorted.length; i++) {
      const cur = sorted[i];
      const contiguous =
        cur && cur.getTime() - prev.getTime() === 3600_000;
      if (!contiguous) {
        const end = new Date(prev.getTime() + 3600_000);
        ranges.push(`${fmtDay(start)} ${fmt(start)}–${fmt(end)}`);
        if (cur) start = cur;
      }
      prev = cur;
    }
    return ranges;
  }

  private _renderEvCard(ev: EVPlan): TemplateResult {
    const plannedKwh = ev.planned_hours.reduce((sum, h) => sum + h.kwh, 0);
    const forcedRanges = this._evForcedRanges(ev.forced_hours);
    return html`
      <div class="card">
        <div class="card-title">🚗 Samochód elektryczny</div>
        <div class="check">
          Stan: <b>${ev.soc !== null ? `${ev.soc}%` : "—"}</b>
          ${ev.target_soc !== null ? html`<span class="muted">cel ${ev.target_soc}%</span>` : nothing}
          ${ev.min_soc != null ? html`<span class="muted">· min ${ev.min_soc}%</span>` : nothing}
        </div>
        <div class="check">
          Pojemność baterii:
          ${ev.capacity_kwh != null
            ? html`<b>${ev.capacity_kwh.toFixed(1)} kWh</b>
                <span class="muted">
                  ${ev.capacity_source === "learned"
                    ? `wyliczona z ${ev.capacity_sessions} sesji`
                    : "wstępna (uczy się z ładowań)"}
                </span>`
            : html`<b>uczy się…</b>
                <span class="muted">${ev.capacity_sessions}/${ev.min_capacity_sessions} sesji — EV nie planuje bez pojemności</span>`}
        </div>
        ${ev.kwh_per_km != null || ev.drain_days > 0
          ? html`<div class="check">
              Zużycie jazdy:
              <b>${ev.kwh_per_km != null ? `${ev.kwh_per_km.toFixed(3)} kWh/km` : "—"}</b>
              ${ev.drain_next24_kwh != null
                ? html`<span class="muted">prognoza 24 h: ${ev.drain_next24_kwh.toFixed(1)} kWh (${ev.drain_days} dni nauki)</span>`
                : nothing}
            </div>`
          : nothing}
        <div class="check">
          W domu: <b>${this._evBool(ev.home, "tak", "nie")}</b> ·
          Ładuje: <b>${this._evBool(ev.charging, "tak", "nie")}</b> ·
          Dostępny: <b>${ev.available ? "tak" : "nie"}</b>
        </div>
        ${ev.energy_added_kwh !== null
          ? html`<div class="check">Dodano w sesji: <b>${ev.energy_added_kwh} kWh</b></div>`
          : nothing}
        ${ev.trips?.length
          ? html`<div class="check"><b>Wyjazdy (kalendarz + Google Maps)</b></div>
              ${ev.trips.map(
                (t) => html`<div class="check">
                  <span class="dot bad"></span>
                  <b>${t.label || t.location}</b>
                  <span class="muted">
                    ${this._fmtRun(t.depart)} → ${this._fmtRun(t.return_end)}
                    ${t.distance_km != null
                      ? ` · ${t.distance_km.toFixed(0)} km w jedną stronę`
                      : " · brak dystansu (Google Maps)"}
                    ${t.duration_min != null ? ` · dojazd ${Math.round(t.duration_min)} min` : ""}
                    ${t.energy_kwh != null ? ` · ~${t.energy_kwh.toFixed(1)} kWh na podróż` : ""}
                  </span>
                </div>`
              )}`
          : nothing}
        ${ev.targets.length
          ? html`<div class="check"><b>Terminy z kalendarza</b></div>
              ${ev.targets.map(
                (t) => html`<div class="check">
                  <span class="dot ok"></span>${t.source === "trip" ? "min " : ""}${t.target_soc.toFixed(0)}% do
                  <b>${this._fmtRun(t.deadline)}</b>
                  ${t.label ? html`<span class="muted">${t.label}</span>` : nothing}
                  ${t.reserve_soc != null && t.trip_soc != null
                    ? html`<span class="muted">
                        = rezerwa EV ${t.reserve_soc.toFixed(0)}% (encja „EV minimalna
                        rezerwa SoC”) + podróż ${t.trip_soc.toFixed(0)}%
                      </span>`
                    : nothing}
                </div>`
              )}`
          : nothing}
        ${forcedRanges.length
          ? html`<div class="check"><b>Ręczne okna ładowania</b></div>
              ${forcedRanges.map(
                (r) => html`<div class="check"><span class="dot ok"></span>${r}</div>`
              )}`
          : nothing}
        <div class="check">
          Zaplanowane ładowanie: <b>${plannedKwh.toFixed(1)} kWh</b>
          <span class="muted">${ev.planned_hours.length} godz.</span>
        </div>
        ${!ev.targets.length && !forcedRanges.length && !ev.planned_hours.length
          ? html`<div class="check muted">Brak zaplanowanego ładowania.</div>`
          : nothing}
        <div class="check"><b>Sterowanie (encje dla automatyzacji)</b></div>
        <div class="check">
          Podłącz ładowarkę: <b>${this._evBool(ev.control.connect_charger, "tak", "nie")}</b> ·
          Ładuj teraz: <b>${this._evBool(ev.control.charging_now, "tak", "nie")}</b>
        </div>
        <div class="check">
          Start ładowania:
          <b>${ev.control.charge_start ? this._fmtRun(ev.control.charge_start) : "—"}</b> ·
          Limit SoC: <b>${ev.control.soc_limit !== null ? `${ev.control.soc_limit}%` : "—"}</b>
          ${ev.current_control
            ? html` · Prąd:
                <b>${ev.control.charge_amps !== null ? `${ev.control.charge_amps} A` : "—"}</b>`
            : nothing}
        </div>
        ${ev.current_control
          ? html`<div class="check muted">
              Dobór prądu ładowania: ${ev.min_current_a}–${ev.max_current_a} A
              (maks. ${ev.charger_power_kw ? ev.charger_power_kw.toFixed(1) : "—"} kW) —
              pełne godziny na maksimum, godzina dopełniająca najmniejszym
              wystarczającym prądem
            </div>`
          : ev.charger_power_kw
          ? html`<div class="check muted">
              Ładowanie zawsze pełną mocą ładowarki: ${ev.charger_power_kw.toFixed(1)} kW
            </div>`
          : nothing}
      </div>
    `;
  }

  // ------------------------------------------------------------------
  // "Przepływ" tab — the computation pipeline, live
  // ------------------------------------------------------------------
  /** One input entity row: label, live value and the entity id it comes from. */
  private _flowInput(label: string, e: FlowEntity | null): TemplateResult | typeof nothing {
    if (!e) return nothing;
    return html`<div class="flow-input ${e.available ? "" : "flow-missing"}">
      <span class="flow-input-label">${label}</span>
      <b>${e.value ?? "—"}${e.unit ? ` ${e.unit}` : ""}</b>
      <span class="flow-eid">${e.entity_id}</span>
    </div>`;
  }

  private _renderFlow(): TemplateResult {
    const f = this._flow;
    if (!f) {
      return html`<div class="card empty">
        ${this._flowLoading ? "Ładowanie…" : "Brak danych przepływu."}
      </div>`;
    }
    const n2 = (v: number | null | undefined, digits = 2) =>
      v == null ? "—" : v.toFixed(digits);
    const inp = f.inputs;
    const pr = f.pricing;
    const bat = f.battery;
    const opt = f.optimizer;
    const cur = opt.current;
    const arrow = (label: string) =>
      html`<div class="flow-arrow"><span>↓</span><span class="muted">${label}</span></div>`;

    return html`
      <div class="flow-col">
        <div class="card flow-stage">
          <div class="card-title">1 · Wejścia — sensory i źródła danych</div>
          <div class="flow-inputs">
            ${this._flowInput("Zużycie domu", inp.consumption)}
            ${this._flowInput("SoC baterii", inp.battery_soc)}
            ${this._flowInput("Ładowanie baterii", inp.battery_charge)}
            ${this._flowInput("Rozładowanie baterii", inp.battery_discharge)}
            ${this._flowInput("Import z sieci", inp.grid_import)}
            ${this._flowInput("Cena energii (RDN)", inp.buy_price_sensor)}
            ${this._flowInput("Pogoda", inp.weather)}
            ${this._flowInput("EV SoC", inp.ev_soc)}
            ${this._flowInput("EV energia sesji", inp.ev_energy_added)}
          </div>
          <div class="check muted">
            Źródło cen: <b>${inp.price_source === "pradcast" ? "Prądcast API" : "sensor HA"}</b>
            ${inp.device_sensors.length
              ? html` · podliczniki: <b>${inp.device_sensors.length}</b>`
              : nothing}
            ${inp.calendars.length
              ? html` · kalendarze: <b>${inp.calendars.join(", ")}</b>`
              : nothing}
          </div>
        </div>

        ${arrow("surowe odczyty → składanie ceny brutto")}

        <div class="card flow-stage">
          <div class="card-title">2 · Cena energii (PLN/kWh)</div>
          <div class="flow-formula">
            cena pełna = (RDN + marża ${n2(pr.markup)}) × VAT ${n2(pr.vat)}
            + akcyza ${n2(pr.excise_kwh)} + dystrybucja
          </div>
          <div class="check">
            Teraz: energia <b>${n2(pr.buy_price_now)}</b> + dystrybucja
            <b>${n2(pr.distribution_now)}</b> =
            <b>${n2(pr.total_now)} PLN/kWh</b>
            <span class="muted">(${pr.confirmed ? "cena pewna" : "prognoza"})</span>
          </div>
          <div class="check muted">
            Koszt stały (rozłożony na godziny): ${n2(pr.fixed_hourly)} PLN/h — nie wchodzi w PLN/kWh.
          </div>
        </div>

        ${arrow("cena godzinowa → koszt magazynowania i plan")}

        <div class="card flow-stage">
          <div class="card-title">3 · Prognoza zużycia</div>
          <div class="check">
            Profil uczony przez <b>${f.consumption_model.observed_days}</b> dni ·
            bieżąca godzina (baza): <b>${n2(f.consumption_model.base_now_kwh)} kWh</b> ·
            profile urządzeń: <b>${f.consumption_model.device_profiles}</b>
          </div>
        </div>

        ${arrow("zapotrzebowanie + ceny → model baterii")}

        <div class="card flow-stage">
          <div class="card-title">4 · Model baterii — skąd bierze się „Cena w baterii"</div>
          <div class="check">
            Pojemność <b>${n2(bat.capacity_kwh, 1)} kWh</b> · SoC
            <b>${bat.soc != null ? bat.soc.toFixed(0) + " %" : "—"}</b> ·
            η ładowania
            <b>${bat.efficiency_curve_points
              ? `krzywa (${bat.efficiency_curve_points} pkt, zależna od mocy)`
              : n2(bat.charge_efficiency)}</b> ·
            η rozładowania <b>${n2(bat.discharge_efficiency)}</b> ·
            koszt zużycia (wear) <b>${n2(bat.wear_cost)} PLN/kWh</b>
          </div>
          <div class="flow-formula">
            wkładanie: 1 kWh z sieci → ${n2(bat.charge_efficiency)} kWh w baterii
            (strata ładowania) — zmagazynowanie teraz kosztowałoby
            ≈ <b>${n2(bat.store_cost_now)} PLN/kWh</b>
          </div>
          <div class="flow-formula">
            koszt zmagazynowany (średnia ważona zakupów): <b>${n2(bat.reservoir_cost)} PLN/kWh</b>
          </div>
          <div class="flow-formula flow-highlight">
            „Cena w baterii" = ${n2(bat.reservoir_cost)} / η ${n2(bat.discharge_efficiency)}
            + wear ${n2(bat.wear_cost)} = <b>${n2(bat.delivered_cost)} PLN/kWh</b>
            <span class="muted">(strata rozładowania wliczana przy oddawaniu energii)</span>
          </div>
        </div>

        ${arrow("koszt z sieci vs koszt z baterii → decyzje co godzinę")}

        <div class="card flow-stage">
          <div class="card-title">5 · Optymalizator</div>
          <div class="check">
            Plan z <b>${opt.created_at ? this._fmtRun(opt.created_at) : "—"}</b> ·
            horyzont <b>${opt.horizon_hours} h</b> ·
            koszt horyzontu <b>${n2(opt.total_cost)} PLN</b>
          </div>
          ${cur
            ? html`<div class="check">
                Bieżąca godzina: tryb
                <b>${INVERTER_MODE_META[cur.inverter_mode]?.label ?? cur.inverter_mode}</b> ·
                ładowanie <b>${n2(cur.battery_charge_kwh)} kWh</b> ·
                rozładowanie <b>${n2(cur.battery_discharge_kwh)} kWh</b> ·
                import <b>${n2(cur.grid_buy_kwh)} kWh</b>
                ${cur.ev_charge_kwh > 0.005
                  ? html` · EV <b>${n2(cur.ev_charge_kwh)} kWh</b>`
                  : nothing}
              </div>
              <div class="check muted">
                Koszt godziny: ${n2(cur.hour_cost)} PLN z sieci +
                ${n2(cur.battery_use_cost)} PLN z baterii (rozładowanie ×
                „cena w baterii") · SoC na koniec: ${cur.battery_soc_end.toFixed(0)} %
              </div>`
            : html`<div class="check muted">Brak decyzji dla bieżącej godziny.</div>`}
          ${f.ev.enabled
            ? html`<div class="check muted">
                EV: SoC ${f.ev.soc != null ? f.ev.soc.toFixed(0) + " %" : "—"} ·
                pojemność ${f.ev.capacity_kwh != null ? f.ev.capacity_kwh.toFixed(1) + " kWh" : "uczona"} ·
                moc ładowarki ${f.ev.charger_power_kw != null ? f.ev.charger_power_kw.toFixed(1) + " kW" : "—"} ·
                cele: ${f.ev.targets} · wyjazdy: ${f.ev.trips}
              </div>`
            : nothing}
        </div>

        ${arrow("decyzje → encje sterujące i wykresy")}

        <div class="card flow-stage">
          <div class="card-title">6 · Wyjścia</div>
          <div class="check muted">
            Tryb falownika i moc ładowania → encje integracji (automatyzacje) ·
            plan i koszty → zakładka „Przegląd" · zapis vintage co godzinę →
            zakładki „Symulacje" i porównania prognoz.
          </div>
        </div>
      </div>
    `;
  }

  // ------------------------------------------------------------------
  // "Sprawność" tab — measured charging efficiencies vs the configured ones
  // ------------------------------------------------------------------
  private _renderEfficiency(): TemplateResult {
    const e = this._efficiency;
    if (!e) {
      return html`<div class="card empty">
        ${this._efficiencyLoading ? "Ładowanie…" : "Brak danych sprawności."}
      </div>`;
    }
    const pct = (v: number | null | undefined, d = 1) =>
      v == null ? "—" : (v * 100).toFixed(d) + " %";
    const kwh = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(1));
    // Measured-vs-configured delta, colored: within 2 p.p. neutral, above
    // configured green (better than assumed), below amber.
    const delta = (measured: number | null, configured: number | null) => {
      if (measured == null || configured == null) return nothing;
      const d = (measured - configured) * 100;
      if (Math.abs(d) < 0.05) return html`<span class="muted">=</span>`;
      const color = Math.abs(d) < 2 ? "inherit" : d > 0 ? "#16a34a" : "#f59e0b";
      return html`<span style=${"color:" + color}>
        ${(d > 0 ? "+" : "−") + Math.abs(d).toFixed(1)} p.p.
      </span>`;
    };

    const ev = e.ev;
    const bat = e.battery;
    const evConfiguredAtFull = ev.configured_curve.length
      ? ev.configured_curve[ev.configured_curve.length - 1].eff
      : null;

    return html`
      <div class="card">
        <div class="card-title">Sprawność ładowania — pomiar z sensorów (${e.window_days} dni)</div>
        <div class="muted sim-hint">
          Wartości <b>z ustawień są nadrzędne</b> — plan liczy się na nich; pomiar z sensorów
          jest informacyjny i służy do weryfikacji, czy wpisane sprawności nie są nieaktualne.
        </div>
      </div>

      <div class="card">
        <div class="card-title">🚗 Ładowanie EV — licznik ładowarki vs energia dodana do auta</div>
        ${!ev.available
          ? html`<div class="empty">
              Potrzebne oba sensory: licznik energii ładowarki (sieć) i „energia dodana”
              z auta — wskaż je w ustawieniach EV.
            </div>`
          : html`
              <div class="check muted">
                sieć: <b>${ev.grid_sensor}</b> · auto: <b>${ev.added_sensor}</b>
              </div>
              <div class="kpi-grid">
                ${this._kpi("Godzin ładowania", `${ev.hours}`, null, `ostatnie ${e.window_days} dni`)}
                ${this._kpi("Pobrano z sieci", `${kwh(ev.grid_kwh)} kWh`, null, "licznik ładowarki")}
                ${this._kpi("Dodano do auta", `${kwh(ev.added_kwh)} kWh`, null, "sensor energy added")}
                ${this._kpi("Sprawność zmierzona", pct(ev.measured_eff), null, "dodano ÷ pobrano")}
              </div>
              ${ev.buckets.length
                ? html`
                    <div class="prices-table-wrap">
                      <table class="prices-table">
                        <thead>
                          <tr>
                            <th>Moc (kW)</th>
                            <th>Godzin</th>
                            <th>Pobrano<br /><span class="muted">kWh</span></th>
                            <th>Dodano<br /><span class="muted">kWh</span></th>
                            <th>Zmierzona</th>
                            <th>Z ustawień<br /><span class="muted">krzywa</span></th>
                            <th>Δ</th>
                          </tr>
                        </thead>
                        <tbody>
                          ${ev.buckets.map(
                            (b) => html`<tr>
                              <td>${b.power_kw.toFixed(1)}</td>
                              <td class="muted">${b.hours}</td>
                              <td>${kwh(b.grid_kwh)}</td>
                              <td>${kwh(b.added_kwh)}</td>
                              <td class="bold">${pct(b.measured_eff)}</td>
                              <td class="muted">${pct(b.configured_eff)}</td>
                              <td>${delta(b.measured_eff, b.configured_eff)}</td>
                            </tr>`
                          )}
                        </tbody>
                      </table>
                    </div>
                    <div class="muted sim-hint" style="margin-top:8px">
                      Kubełki wg średniej mocy godziny (kWh/h). „Z ustawień” = krzywa
                      sprawności EV interpolowana w mocy kubełka
                      ${evConfiguredAtFull == null
                        ? html` — <b>krzywa nieskonfigurowana</b> (plan liczy ładowanie bezstratnie)`
                        : nothing}.
                    </div>
                  `
                : html`<div class="empty">Brak godzin ładowania w oknie pomiaru.</div>`}
            `}
      </div>

      <div class="card">
        <div class="card-title">🔋 Bateria domowa — round-trip z liczników</div>
        ${!bat.available
          ? html`<div class="empty">
              Potrzebne sensory ładowania i rozładowania baterii (ustawienia podstawowe).
            </div>`
          : html`
              <div class="check muted">
                ładowanie: <b>${bat.charge_sensor}</b> · rozładowanie:
                <b>${bat.discharge_sensor}</b>
              </div>
              <div class="kpi-grid">
                ${this._kpi("Załadowano", `${kwh(bat.charge_kwh)} kWh`, null, `ostatnie ${e.window_days} dni`)}
                ${this._kpi("Rozładowano", `${kwh(bat.discharge_kwh)} kWh`, null, "oddane do domu")}
                ${this._kpi("Round-trip zmierzony", pct(bat.measured_roundtrip), null, "rozładowano ÷ załadowano")}
                ${this._kpi(
                  "Round-trip z ustawień",
                  pct(bat.configured_roundtrip),
                  null,
                  `η ład. ${pct(bat.configured_charge_eff, 0)} × η rozład. ${pct(bat.configured_discharge_eff, 0)}`
                )}
              </div>
              <div class="check">
                Różnica: ${delta(bat.measured_roundtrip, bat.configured_roundtrip)}
                ${bat.charge_curve_points
                  ? html`<span class="muted">
                      · uwaga: skonfigurowana krzywa sprawności ładowania
                      (${bat.charge_curve_points} pkt) — płaskie η z ustawień to tylko
                      punkt odniesienia</span>`
                  : nothing}
              </div>
              <div class="muted sim-hint">
                Pomiar obejmuje przesunięcie SoC na krańcach okna (energia załadowana,
                ale jeszcze nie oddana) — przy ${e.window_days} dniach cyklowania to
                pomijalny szum.
              </div>
            `}
      </div>
    `;
  }

  private _renderStatus(): TemplateResult {
    const s = this._status;
    if (!s) return html`<div class="card empty">Brak statusu.</div>`;
    return html`
      <div class="card">
        <div class="card-title">Rozszerzenie</div>
        <div class="check">Wersja: <b>${s.version}</b></div>
      </div>
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
      ${s.ev?.enabled ? this._renderEvCard(s.ev) : nothing}
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
  // Diagnostics: is every optimizer input present, recorded and readable?
  // ------------------------------------------------------------------
  private _renderDiagnostics(): TemplateResult {
    const d = this._diagnostics;
    if (this._diagnosticsLoading && !d)
      return html`<div class="card empty">Sprawdzanie danych…</div>`;
    if (!d)
      return html`<div class="card empty">
        Brak diagnostyki.
        <button class="debug-btn" @click=${() => this._loadDiagnostics()}>Sprawdź</button>
      </div>`;
    const dotClass = (s: string) =>
      s === "ok" ? "ok" : s === "warn" ? "warn" : s === "skip" ? "skip" : "bad";
    return html`
      <div class="card">
        <div class="card-title">
          Gotowość optymalizatora
          <span class=${"diag-badge " + (d.ready ? "ok" : "bad")}>
            ${d.ready ? "✓ Komplet wymaganych danych" : "✗ Brakuje wymaganych danych"}
          </span>
        </div>
        <div class="muted">
          ✓ ${d.summary.ok} · ⚠ ${d.summary.warn} · ✗ ${d.summary.error} · —
          ${d.summary.skip}
          <button class="debug-btn" @click=${() => this._loadDiagnostics()}>Odśwież</button>
        </div>
      </div>
      ${d.groups.map(
        (g) => html`
          <div class="card">
            <div class="card-title">${g.title}</div>
            ${g.items.map(
              (it) => html`
                <div class="diag-row">
                  <div class="diag-head">
                    <span class=${"dot " + dotClass(it.status)}></span>
                    <span class="diag-label"
                      >${it.label}${it.required
                        ? html`<span class="diag-req">wymagane</span>`
                        : nothing}</span
                    >
                    <span class="muted diag-msg">${it.message}</span>
                  </div>
                  ${it.entity_id
                    ? html`<div class="diag-eid">${it.entity_id}</div>`
                    : nothing}
                  ${this._renderDiagDetail(it)}
                </div>
              `
            )}
          </div>
        `
      )}
    `;
  }

  private _renderDiagDetail(it: DiagItem): TemplateResult {
    const dt = it.detail;
    if (!dt || !("detected_kind" in dt)) return html``;
    const det = dt as unknown as DiagSensorDetail;
    const last = det.samples?.[det.samples.length - 1];
    return html`
      <div class="diag-detail">
        <span>jednostka: <b>${det.unit_of_measurement ?? "—"}</b></span>
        <span>state_class: <b>${det.state_class ?? "—"}</b></span>
        <span>typ: <b>${det.detected_kind ?? "—"}</b></span>
        <span>stat sum/mean: <b>${det.stat_rows_sum}/${det.stat_rows_mean}</b></span>
        <span>godz./48h: <b>${det.series_hours}</b></span>
        ${last ? html`<span>ostatnia: <b>${last.kwh} kWh</b></span>` : nothing}
      </div>
    `;
  }

  // ------------------------------------------------------------------
  // Profiles (7×24 heatmaps + D+1..D+3 overlay)
  // ------------------------------------------------------------------
  private _renderProfiles(): TemplateResult {
    const stats = this._stats;
    if (this._statsLoading && !stats)
      return html`<div class="card empty">Ładowanie profili zużycia…</div>`;
    if (!stats || !stats.profiles.length)
      return html`<div class="card empty">
        Brak danych o zużyciu — skonfiguruj czujnik zużycia (i opcjonalnie czujniki urządzeń).
      </div>`;

    const sel =
      stats.profiles.find((p) => p.key === this._statsKey) ?? stats.profiles[0];

    return html`
      <div class="card">
        <div class="card-title">Profile zużycia</div>
        <div class="prof-chips">
          ${stats.profiles.map(
            (p) => html`
              <button
                class="prof-chip ${p.key === sel.key ? "active" : ""}"
                @click=${() => {
                  this._statsKey = p.key;
                }}
              >
                <ha-icon icon=${p.icon}></ha-icon>
                <span>${p.name}</span>
                <span class="prof-chip-kwh">${p.avg_daily.toFixed(1)} kWh/d</span>
              </button>
            `
          )}
        </div>
      </div>

      <div class="card">
        <div class="card-title">${sel.name} — wskaźniki</div>
        <div class="kpi-grid">
          ${this._kpi("Średnio / dzień", `${sel.avg_daily.toFixed(1)} kWh`, null, "ost. 7 dni")}
          ${this._kpi(
            "Ten tydzień",
            `${sel.week_total.toFixed(1)} kWh`,
            sel.week_change_pct,
            "vs poprzednie 7 dni"
          )}
          ${this._kpi(
            "Ten miesiąc",
            `${sel.month_total.toFixed(1)} kWh`,
            sel.month_change_pct,
            "vs poprzednie 30 dni"
          )}
        </div>
      </div>

      <div class="card">
        <div class="card-title">Zużycie dzienne — ostatnie ${Math.min(sel.daily.length, 30)} dni</div>
        ${this._dailyBars(sel)}
      </div>

      ${this._heatmapForKey(sel.key)}
      ${this._climateProfileCard(sel.key)}
    `;
  }

  /** Learned temperature profile of a weather-dependent load (AC, heat pump):
   *  a temperature-bin × hour heatmap plus the learning/takeover status. */
  private _climateProfileCard(key: string): TemplateResult | typeof nothing {
    const info = this._profiles?.climate?.[key];
    if (!info) return nothing;
    const status = info.ready
      ? html`<b>model aktywny</b> — prognoza tego urządzenia liczona z temperatury
          (zamiast profilu tygodniowego)`
      : html`uczy się: <b>${info.observed_days}/${info.min_learn_days}</b> dni —
          do tego czasu urządzenie planowane z profilu tygodniowego`;
    return html`
      <div class="card">
        <div class="card-title">Profil temperaturowy — kWh/h wg temperatury × godziny</div>
        <div class="check">
          ${status}
          <span class="muted">· ${info.samples} próbek godzinowych</span>
        </div>
        ${info.matrix.length
          ? this._tempHeatmap(info.matrix)
          : html`<div class="empty">
              Brak zebranych par temperatura×zużycie — profil zacznie się budować po
              pierwszej pełnej dobie z zapisaną temperaturą.
            </div>`}
      </div>
    `;
  }

  /** Heatmap of a climate profile: rows = 2 °C bins (warmest on top), columns
   *  = hours of day. Mirrors the weekly heatmap's look, wider row labels. */
  private _tempHeatmap(rows: ClimateProfileRow[]): TemplateResult {
    const values: number[] = [];
    rows.forEach((r) =>
      r.values.forEach((v) => {
        if (v !== null && v !== undefined) values.push(v);
      })
    );
    if (!values.length)
      return html`<div class="empty">Brak danych — profil jeszcze się uczy.</div>`;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const sorted = [...rows].sort((a, b) => b.temp_from - a.temp_from);
    const label = (r: ClimateProfileRow) =>
      `${r.temp_from.toFixed(0)}–${r.temp_to.toFixed(0)}°`;
    return html`
      <div class="heatmap">
        <div class="hm-row hm-head">
          <div class="hm-label hm-label-temp"></div>
          ${Array.from({ length: 24 }, (_, h) => html`<div class="hm-h">${h}</div>`)}
        </div>
        ${sorted.map(
          (r) => html`
            <div class="hm-row">
              <div class="hm-label hm-label-temp" title="${r.samples} próbek">${label(r)}</div>
              ${r.values.map((v) => {
                const color =
                  v === null || v === undefined ? "transparent" : this._heatColor(v, min, max);
                const title = v === null || v === undefined ? "—" : `${v.toFixed(3)} kWh`;
                return html`<div class="hm-cell" style=${"background:" + color} title=${title}></div>`;
              })}
            </div>
          `
        )}
      </div>
      <div class="legend">
        <span>${min.toFixed(2)}</span>
        <div class="legend-bar"></div>
        <span>${max.toFixed(2)} kWh</span>
      </div>
    `;
  }

  /** A single KPI tile; `change` colours green (down = good for consumption). */
  private _kpi(
    label: string,
    value: string,
    change: number | null,
    sub: string
  ): TemplateResult {
    return html`
      <div class="kpi">
        <div class="kpi-label">${label}</div>
        <div class="kpi-value">${value}</div>
        <div class="kpi-sub">
          ${change == null
            ? html`<span class="muted">${sub}</span>`
            : html`<span class="kpi-delta ${change <= 0 ? "down" : "up"}">
                  ${change <= 0 ? "▼" : "▲"} ${Math.abs(change).toFixed(1)}%
                </span>
                <span class="muted">${sub}</span>`}
        </div>
      </div>
    `;
  }

  /** SVG daily bar chart for the last 30 days of a profile, with a kWh axis. */
  private _dailyBars(p: StatProfile): TemplateResult {
    const days = p.daily.slice(-30);
    if (!days.length) return html`<div class="empty">Brak danych dziennych.</div>`;
    const max = Math.max(0.001, ...days.map((d) => d.kwh));
    const w = 760;
    const ht = 220;
    const padB = 26; // x labels
    const padT = 10;
    const padL = 42; // y labels
    const gap = 3;
    const plotW = w - padL;
    const plotH = ht - padB - padT;
    const bw = (plotW - (days.length - 1) * gap) / days.length;
    const dark = this._isDark();
    const fg = dark ? "#9ca3af" : "#6b7280";
    const grid = dark ? "rgba(255,255,255,0.10)" : "rgba(0,0,0,0.08)";
    const yFor = (v: number) => padT + plotH - (v / max) * plotH;
    // "Nice" gridline step so labels read cleanly (e.g. 0 / 5 / 10 / 15 kWh).
    const rawStep = max / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
    const niceStep = [1, 2, 2.5, 5, 10].find((m) => m * mag >= rawStep)! * mag;
    const ticks: number[] = [];
    for (let v = 0; v <= max + 1e-9; v += niceStep) ticks.push(v);

    return html`
      <svg viewBox="0 0 ${w} ${ht}" class="chart" style="width:100%">
        ${ticks.map(
          (v) => svg`
            <line x1=${padL} y1=${yFor(v)} x2=${w} y2=${yFor(v)} stroke=${grid} stroke-width="1"></line>
            <text x=${padL - 6} y=${yFor(v) + 3} text-anchor="end" font-size="10" fill=${fg}>${v.toFixed(v < 10 ? 1 : 0)}</text>`
        )}
        <text x="2" y=${padT + 4} font-size="10" fill=${fg}>kWh</text>
        ${days.map((d, i) => {
          const h = (d.kwh / max) * plotH;
          const x = padL + i * (bw + gap);
          const y = padT + plotH - h;
          const dt = new Date(d.date + "T00:00:00");
          const weekend = dt.getDay() === 0 || dt.getDay() === 6;
          const color = d.partial ? "#9ca3af" : weekend ? "#f59e0b" : "#3b82f6";
          const ds = dt.toLocaleDateString("pl-PL", { day: "2-digit", month: "2-digit" });
          const label = `${ds}: ${d.kwh.toFixed(2)} kWh${d.partial ? " (dziś, niepełny)" : ""}`;
          return svg`<rect x=${x} y=${y} width=${Math.max(bw, 0.5)} height=${Math.max(h, 0)}
              rx="1.5" fill=${color} opacity=${d.partial ? 0.5 : 0.9}>
              <title>${label}</title></rect>
            ${
              i % 5 === 0
                ? svg`<text x=${x + bw / 2} y=${ht - 8} text-anchor="middle" font-size="10" fill=${fg}>${ds}</text>`
                : nothing
            }`;
        })}
      </svg>
      <div class="legend-row">
        <span><i class="dot-sq" style="background:#3b82f6"></i> dzień roboczy</span>
        <span><i class="dot-sq" style="background:#f59e0b"></i> weekend</span>
        <span><i class="dot-sq" style="background:#9ca3af"></i> dziś (niepełny)</span>
        <span class="muted">najedź na słupek, by zobaczyć dokładną wartość</span>
      </div>
    `;
  }

  /** 7×24 heatmap for the selected profile, mapped from the learned matrices. */
  private _heatmapForKey(key: string): TemplateResult {
    const p = this._profiles;
    if (!p) return html``;
    let matrix: Matrix | null = null;
    let note = "";
    if (key === "__base__") {
      matrix = p.consumption;
    } else if (key === "__main__") {
      note = "Heatmapa 7×24 dostępna dla tła i poszczególnych urządzeń.";
    } else {
      matrix = p.devices[key] ?? null;
    }
    return html`
      <div class="card">
        <div class="card-title">
          Profil tygodniowy 7×24 (${p.consumption_days} dni nauki)
        </div>
        ${matrix
          ? this._heatmap(matrix, "kWh")
          : html`<div class="empty">${note || "Brak wyuczonego profilu."}</div>`}
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
    /* Header styled after HA's toolbar: 20px regular title, text-style
       action button. */
    .header {
      display: flex;
      align-items: center;
      margin-bottom: 12px;
    }
    .title {
      font-size: 20px;
      font-weight: 400;
      letter-spacing: 0.1px;
    }
    .spacer {
      flex: 1;
    }
    .cfg {
      cursor: pointer;
      border: none;
      background: transparent;
      color: var(--primary-color);
      border-radius: 4px;
      padding: 8px 12px;
      font-size: 14px;
      font-weight: 500;
    }
    .cfg:hover {
      background: rgba(var(--rgb-primary-color, 33, 150, 243), 0.08);
    }
    /* Tabs styled after HA's underline tabs (ha-tabs / sl-tab-group). */
    .tabs {
      display: flex;
      gap: 0;
      margin-bottom: 16px;
      border-bottom: 1px solid var(--divider-color, rgba(127, 127, 127, 0.3));
      overflow-x: auto;
    }
    .tab {
      cursor: pointer;
      border: none;
      background: transparent;
      color: var(--secondary-text-color);
      border-bottom: 2px solid transparent;
      border-radius: 0;
      padding: 10px 16px;
      font-size: 14px;
      font-weight: 500;
      white-space: nowrap;
      margin-bottom: -1px;
      transition: color 0.15s, border-color 0.15s;
    }
    .tab:hover {
      color: var(--primary-text-color);
    }
    .tab.active {
      color: var(--primary-color);
      border-bottom-color: var(--primary-color);
      background: transparent;
    }
    .content {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    /* Card styled after ha-card (border + radius + shadow from theme vars,
       flat bordered look on default themes). */
    .card {
      background: var(--ha-card-background, var(--card-background-color, #1c1c1c));
      border-radius: var(--ha-card-border-radius, 12px);
      border: var(--ha-card-border-width, 1px) solid
        var(--ha-card-border-color, var(--divider-color, #e0e0e0));
      padding: 16px;
      box-shadow: var(--ha-card-box-shadow, none);
    }
    .card-title {
      color: var(--ha-card-header-color, var(--primary-text-color));
      font-family: var(--ha-card-header-font-family, inherit);
      font-size: 16px;
      font-weight: 500;
      letter-spacing: 0.1px;
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
    .dot.warn {
      background: var(--warning-color, #f9a825);
    }
    .dot.skip {
      background: var(--disabled-text-color, #9e9e9e);
    }
    .diag-badge {
      font-size: 12px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 10px;
      margin-left: 8px;
    }
    .diag-badge.ok {
      background: rgba(67, 160, 71, 0.15);
      color: var(--success-color, #43a047);
    }
    .diag-badge.bad {
      background: rgba(211, 51, 51, 0.15);
      color: var(--error-color, #d33);
    }
    .diag-row {
      padding: 8px 0;
      border-top: 1px solid var(--divider-color, #e0e0e0);
    }
    .diag-row:first-child {
      border-top: none;
    }
    .diag-head {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .diag-label {
      font-weight: 500;
    }
    .diag-req {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--error-color, #d33);
      border: 1px solid currentColor;
      border-radius: 6px;
      padding: 0 4px;
      margin-left: 6px;
    }
    .diag-msg {
      margin-left: auto;
    }
    .diag-eid {
      font-family: monospace;
      font-size: 12px;
      color: var(--secondary-text-color);
      margin: 2px 0 0 18px;
    }
    .diag-detail {
      display: flex;
      flex-wrap: wrap;
      gap: 4px 14px;
      font-size: 12px;
      color: var(--secondary-text-color);
      margin: 4px 0 0 18px;
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
    /* Temperature-bin rows need room for "24–26°". */
    .hm-label-temp {
      width: 52px;
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
    .prof-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .prof-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 999px;
      border: 1px solid var(--divider-color, #444);
      background: var(--card-background-color, #1c1c1c);
      color: var(--primary-text-color);
      cursor: pointer;
      font-size: 13px;
      transition: all 0.12s ease;
    }
    .prof-chip:hover {
      border-color: var(--primary-color);
    }
    .prof-chip.active {
      background: var(--primary-color);
      border-color: var(--primary-color);
      color: #fff;
    }
    .prof-chip ha-icon {
      --mdc-icon-size: 18px;
    }
    .prof-chip-kwh {
      opacity: 0.65;
      font-variant-numeric: tabular-nums;
      font-size: 12px;
    }
    .flow-col {
      display: flex;
      flex-direction: column;
      max-width: 860px;
      margin: 0 auto;
    }
    .flow-stage {
      margin-bottom: 0;
    }
    .flow-arrow {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 6px 0 6px 26px;
      color: var(--secondary-text-color);
      font-size: 12px;
    }
    .flow-arrow span:first-child {
      font-size: 18px;
      color: var(--primary-color);
    }
    .flow-inputs {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
      gap: 8px;
      margin-bottom: 8px;
    }
    .flow-input {
      display: flex;
      flex-direction: column;
      gap: 1px;
      padding: 8px 10px;
      border: 1px solid var(--divider-color, #444);
      border-radius: 8px;
      font-size: 13px;
    }
    .flow-input.flow-missing {
      opacity: 0.5;
      border-style: dashed;
    }
    .flow-input-label {
      font-size: 11px;
      color: var(--secondary-text-color);
    }
    .flow-eid {
      font-size: 10px;
      color: var(--secondary-text-color);
      font-family: monospace;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .flow-formula {
      margin: 6px 0;
      padding: 8px 12px;
      border-left: 3px solid var(--primary-color);
      background: rgba(127, 127, 127, 0.07);
      border-radius: 0 6px 6px 0;
      font-size: 13px;
      font-variant-numeric: tabular-nums;
    }
    .flow-formula.flow-highlight {
      border-left-color: #14b8a6;
    }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }
    .kpi {
      padding: 14px 16px;
      border-radius: 10px;
      background: var(--secondary-background-color, rgba(255, 255, 255, 0.04));
      border: 1px solid var(--divider-color, #333);
    }
    .kpi-label {
      font-size: 12px;
      color: var(--secondary-text-color);
      margin-bottom: 4px;
    }
    .kpi-value {
      font-size: 24px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }
    .kpi-sub {
      margin-top: 6px;
      font-size: 12px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .kpi-delta {
      font-weight: 700;
      padding: 1px 6px;
      border-radius: 6px;
      font-variant-numeric: tabular-nums;
    }
    .kpi-delta.down {
      color: #16a34a;
      background: rgba(22, 163, 74, 0.13);
    }
    .kpi-delta.up {
      color: #dc2626;
      background: rgba(220, 38, 38, 0.13);
    }
    .legend-row {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      margin-top: 8px;
      font-size: 12px;
      color: var(--secondary-text-color);
    }
    .legend-row .dot-sq {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 2px;
      margin-right: 4px;
      vertical-align: middle;
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
    .nav-row-secondary {
      margin-top: 8px;
    }
    .nav-row-secondary .nav-btn {
      padding: 4px 10px;
      font-size: 12px;
    }
    .nav-label {
      font-size: 12px;
      color: var(--secondary-text-color);
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
    /* One box, not two: the wrapper carries the background/border/radius and
       the custom tooltip HTML inside is frameless (a second inner frame plus
       the wrapper's own background used to read as a gray box with a white
       gap around it). */
    .apexcharts-tooltip,
    .apexcharts-xaxistooltip,
    .apexcharts-yaxistooltip {
      pointer-events: none !important;
      background: var(--card-background-color, #2a2a2a) !important;
      color: var(--primary-text-color) !important;
      border: 1px solid var(--divider-color, #444) !important;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4) !important;
    }
    .apexcharts-tooltip {
      border-radius: 8px !important;
      overflow: hidden;
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
    .prices-table .price-full-cell {
      color: var(--primary-text-color);
      text-shadow: 0 1px 1px rgba(0, 0, 0, 0.24);
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
