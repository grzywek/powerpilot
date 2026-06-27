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
- [x] Permanent **price archive** — per-hour energy price with provenance
      (certain vs forecast), layered so a binding RDN price is never downgraded
      back to a forecast; persisted across restarts and pruned after 90 days.
- [x] Weighted **estimate** for hours the source no longer covers: same
      weekday+hour averaged over the last 1/2/3 weeks of confirmed prices,
      filling the D+4..D+7 tail without over-extending the horizon.
- [x] One-time historical backfill (~3 weeks) on first run so estimates have
      history immediately.
- [x] ~~Rolling weekday×hour price profile~~ — superseded by the archive +
      weighted estimate above; removed to keep a single source of truth.

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
- [x] EV module: SoC, home/away location, km-per-charge, weekly off-calendar km.
- [x] Charger telemetry: connected (availability gate), charging (plan-vs-reality),
      energy added (session kWh), target SoC (default charge goal).
- [x] 1/3-phase charger setting → full charge power = per-phase × phases; EV always
      charges at full power (whole-hour blocks), clipped only by the 100 % ceiling.
- [x] EV SoC tracking: real (sensor history) + forecast (`decision.ev_soc`),
      drawn as a dashed SoC line on the energy chart.
- [x] Control surface for automations: connect-charger (planned within 24 h),
      charge-start timestamp, SoC limit (charging is always full charger power).
- [ ] 3-phase charger sharing one phase with the inverter → power-limit coupling.
- [x] Calendar read (any HA `calendar.*` entity: Google, CalDAV/iCloud, Local).
      Keyword events: `<kw> NN%` = deadline target (be at NN% by event start,
      optimizer picks cheapest hours); bare `<kw>` = forced full-power window.
- [ ] Trip distances (home → event → home) sizing the pre-departure energy need.
- [ ] All-day "away" events keep SoC in a lower band and wait for better prices.
- [ ] Hourly events (washing 3 kWh/h, ironing 2 kWh/h) injected as loads.
- [x] Reminders (notify to plug in the car; charger-idle-but-due warning).

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
- [x] Panel **Profiles** tab: 7×24 consumption heatmap and a D+1..D+3
      forecast overlay (`/prices/forecasts`), via `powerpilot/profiles|forecasts`.
- [ ] Inverter-mode status markers + forecast-confidence shading on the charts.
- [x] Options flow grouped into sections + menu for a tidier config, including a
      **Clear data & cache** action (wipes stored data, keeps configuration).

## Stage 7 – Hardening
- [ ] Tests for battery math, optimizer decisions, module contributions.
- [ ] HACS release metadata, diagnostics, repair issues.

## Stage 8 – Distribution tariffs ✅
The price coming from prądcast (or any sensor) is only the **commodity** price
of energy. The household actually pays `energy + distribution`, where the
distribution component depends on time-of-day, day-of-week (workday vs
weekend/holiday) and the calendar season. PowerPilot models this as a separate
`tariff` module so a single Tariff definition can cover multiple validity
ranges (e.g. one "G12 zima" tariff active for both 2024/25 and 2025/26 winters).

- [x] **Etap A** — Data model: `Tariff` (with `list[ValidityRange]` and a flat
      `base_component_kwh` surcharge applied to every period) and `TariffPeriod`
      (day-sensor + start/end hour with wrap-around). `HourSlot` gained
      `distribution_price_kwh` and a computed `total_price_kwh = buy_price +
      distribution_price_kwh`.
- [x] **Etap B** — Multi-step OptionsFlow (`tariff_list → tariff_form →
      period_list → {period_form | range_list → range_form}`). Catch-all
      "Pozaszczyt" is modelled as an explicit 0–24 h period without a
      `day_sensor` so the user can see and adjust it.
- [x] **Etap C** — `TariffModule` writes `slot.distribution_price_kwh` for every
      future hour and snapshots the current hour to a persistent `Store` so the
      historical cost stays stable. Future-day workday classification is
      pre-fetched in `async_update` via the `workday.check_date` service and
      cached per `(entity_id, date)`.
- [x] **Etap D** — Optimizer percentiles + battery decisions now run on
      `total_price`, and `Decision.energy_cost / Decision.distribution_cost`
      break the per-hour cost into its two components. `coordinator.get_series`
      exposes both prices and both costs (past via the snapshot, future via the
      decision). The frontend price chart shows five PLN/kWh lines (energy
      confirmed/forecast, distribution, total confirmed/forecast) and two
      stacked PLN/h columns that sum to the hour total. The Status tab gains a
      "Taryfa dystrybucyjna" check.

## Stage 9 – Hardening (continued)
- [ ] Unit tests for the new tariff model (validity ranges, base component,
      `tariff_for_day`, `workday.check_date` cache hits/misses).
