# PowerPilot – Roadmap

Delivery is split into stages. Each stage is independently useful and leaves the
integration installable and working.

## Stage 0 – Foundation (this stage)
- [x] HA custom-integration scaffold (manifest, config flow, coordinator).
- [x] Core models: `HourSlot`, `Forecast`, `Decision`, `Plan`.
- [x] `BatteryModel` with **cost-after-losses** tracking.
- [x] Module contract + registry.
- [x] Heuristic optimizer producing the full decision set.
- [x] Output sensors + binary sensors + a `plan` sensor carrying the whole
      forecast as attributes (chart-ready).
- [x] Config + options flow for the core hardware parameters.

## Stage 1 – Real price sources
- [x] Price module with pluggable sources (confirmed vs forecast).
- [x] Adapter for a Polish dynamic tariff API (prądcast.pl: RDN + D+1..D+3).
- [x] Retail conversion of wholesale RDN via markup + VAT.
- [x] Rolling hourly × weekday **price profile** learned from confirmed prices
      ("cheapest 13–16, nights, weekends"), surfaced on the plan sensor.
- [x] Use the profile to fill interior gaps without over-extending the horizon.
- [ ] Persist the price profile across restarts (currently in-memory).
- [ ] Historical backfill from `/prices/trend` + `/prices/forecasts`.

## Stage 2 – Consumption learning
- [x] Rolling weekly base profile learned from the main sensor via recorder
      long-term statistics (handles both energy kWh and power W/kW sensors).
- [x] Break out separately-metered devices (AC, washer, boiler, iron) into their
      own weekly profiles so the base = main − Σ(devices) stays clean.
- [x] Persisted, incremental (one settled day folded in at a time), with a default
      shape fallback until learned.
- [ ] Recency weighting / decay so recent days weigh more.
- [ ] Let a smarter forward model (climate/calendar) mark a device "managed" to
      replace its learned profile instead of adding it back.

## Stage 3 – EV + calendar
- [ ] EV module: SoC, home/away location, km-per-charge, weekly off-calendar km.
- [ ] 3-phase charger sharing one phase with the inverter → power-limit coupling.
- [ ] Apple Calendar (CalDAV) read → trip distances (home → event → home).
- [ ] All-day "away" events keep SoC in a lower band and wait for better prices.
- [ ] Hourly events (washing 3 kWh/h, ironing 2 kWh/h) injected as loads.
- [ ] Reminders (notify to plug in the car).

## Stage 4 – Weather & climate
- [ ] Hourly temperature forecast module.
- [ ] Heating/cooling energy-vs-temperature model feeding the consumption forecast.

## Stage 5 – Optimizer upgrade
- [ ] Replace the heuristic with an LP/MILP cost-minimizer (e.g. `pulp`/`highs`),
      objective = minimize total grid cost incl. battery wear, subject to SoC,
      inverter charge curve, connection-power and phase constraints.
- [ ] Negative-price handling; weekend "arrive with empty EV battery" strategy.

## Stage 6 – Frontend
- [x] Two-chart dashboard (SoC/flows + prices incl. battery cost line) matching the
      reference mock, shipped as `dashboards/powerpilot-dashboard.yaml` (ApexCharts).
- [x] Full sidebar panel dashboard (`dashboards/powerpilot-panel.yaml`) as a YAML
      dashboard alternative.
- [x] **Custom Lit panel** auto-registered in the sidebar (no YAML needed):
      Overview (SVG charts + control), Status (what works / what's missing),
      Logs (recent optimization runs + module errors), plus a Configure button.
- [x] WebSocket API (`powerpilot/plan|status|log`) backing the panel instead of
      overloading entity attributes.
- [ ] Inverter-mode status markers + forecast-confidence shading on the charts.
- [ ] Overlay D+1/D+2/D+3 forecasts (`/prices/forecasts`).
- [ ] Options flow grouped into sections + menu for a tidier config.

## Stage 7 – Hardening
- [ ] Tests for battery math, optimizer decisions, module contributions.
- [ ] HACS release metadata, diagnostics, repair issues.
