# PowerPilot — Handover (resume guide)

Authoritative state of the project so a fresh session can continue without
re-reading the whole history. Last updated at the end of the build session.

## 1. What this is

A Home Assistant **custom integration** (`custom_components/powerpilot`) that
optimizes a home battery/inverter (Victron-style) under a **dynamic energy
tariff**, factoring household consumption, EV, weather, heating/cooling and the
calendar. It must always know the **price of stored energy after losses**.

Outputs per hour: inverter mode (charge/discharge/passthrough), charge power
(full/limited), grid connected, EV charge, plus reminders.

## 2. Current status (what's done)

| Stage | Status | Notes |
|-------|--------|-------|
| 0 Foundation | ✅ | models, BatteryModel (cost-after-losses), module pipeline, heuristic optimizer, config flow, sensors/binary_sensors |
| 1 Prices | ✅ | pluggable sources; **prądcast.pl** adapter (confirmed RDN vs D+1..D+3 forecast); retail markup+VAT; learned 7×24 price profile (persisted + daily backfill) |
| 2 Consumption learning | ✅ | recorder-based weekly base profile = main − Σ(device sensors); per-device profiles; persisted + incremental; energy & power sensors |
| 6 Frontend | ✅ | **custom Lit panel** auto-registered in sidebar (Overview/Status/Profiles/Logs); WebSocket API; ApexCharts YAML dashboards also shipped |
| 8 Distribution tariffs | ✅ | `tariff` module: `Tariff(validity_ranges, base_component_kwh, periods)`; OptionsFlow CRUD for tariffs/periods/ranges; `workday.check_date` pre-fetch for future days; H+1 snapshot persisted; optimizer + chart split energy vs distribution |
| 3 EV + calendar | ⬜ | EV module is a stub (SoC-deficit sizing only); calendar module is a placeholder |
| 4 Weather + climate | ⬜ | modules are stubs; degree-hour model scaffolded |
| 5 Optimizer LP/MILP | ⬜ | still the heuristic |

Full task list in [ROADMAP.md](ROADMAP.md). Architecture in
[ARCHITECTURE.md](ARCHITECTURE.md). Install/verify in
[INSTALL_AND_VERIFY.md](INSTALL_AND_VERIFY.md).

## 3. Architecture (one-liner)

```
modules (prices · consumption · loads · weather · climate · ev · calendar)
   → ForecastBuilder → Optimizer (+ BatteryModel) → Plan
   → entities + WebSocket API → custom Lit panel
```

Modules only *add* to a shared hourly `Forecast`; they never read each other.
The optimizer is a transparent price-percentile + SoC heuristic, designed to be
swappable for an LP solver later.

## 4. Key files

**Backend core**
- [const.py](../custom_components/powerpilot/const.py) — all config keys, defaults, enums
- [models.py](../custom_components/powerpilot/models.py) — `HourSlot`, `Forecast`, `Decision`, `Plan` (+`as_dict`)
- [battery.py](../custom_components/powerpilot/battery.py) — `BatteryModel`, cost-after-losses (the explicit requirement)
- [optimizer.py](../custom_components/powerpilot/optimizer.py) — heuristic + `ChargeCurve`
- [forecast.py](../custom_components/powerpilot/forecast.py) — builds horizon, trims to where prices reach
- [coordinator.py](../custom_components/powerpilot/coordinator.py) — pipeline + `get_status/get_log/get_profiles/get_forecasts/get_series`
- [profiles.py](../custom_components/powerpilot/profiles.py) — reusable `WeeklyAccumulator` (7×24, persisted)

**Modules** (`custom_components/powerpilot/modules/`)
- `prices.py` + `price_sources.py` — Sensor & **Pradcast** sources, `PriceProfile`
- `tariff.py` — distribution tariff (commodity ≠ delivered cost). Resolves the
  active `Tariff` per day via `models.tariff_for_day`, writes
  `slot.distribution_price_kwh = base_component_kwh + period.price_kwh` for every
  future hour, snapshots the current hour into a persistent `Store` (90-day
  prune, 30 s `async_delay_save`). Future-day workday classification is
  pre-fetched in `async_update` via the `workday.check_date` HA service and
  cached per `(entity_id, date)` for the 4-day horizon.
- `consumption.py` — recorder learner (base + per-device)
- `ev.py` — `EVRequest` (SoC-deficit sizing) — **stub, expand in Stage 3**
- `loads.py`, `weather.py`, `climate.py`, `calendar.py` — **stubs/scaffolds**
- `base.py` — `PowerPilotModule` + `ModuleRegistry` (tracks `last_error`)

**HA glue**
- `__init__.py`, `config_flow.py` (user→prices→ev steps), `sensor.py`, `binary_sensor.py`
- `panel.py` — registers sidebar panel + static JS + WS (frontend optional)
- `websocket_api.py` — `powerpilot/plan|status|log|profiles|forecasts|series`

**Frontend** (`frontend/`, built into `custom_components/powerpilot/frontend/powerpilot-panel.js`)
- `src/powerpilot-panel.ts` — Lit panel. Tabs: Overview (axis charts: SoC line,
  consumption real/forecast, charge/discharge bars, inverter-mode strip, now
  marker, confirmed vs forecast prices, battery-cost line), Status, Profiles
  (7×24 heatmaps + D+1..D+3 overlay), Logs.
- `package.json`, `tsconfig.json`, `esbuild.mjs`

**Dashboards** (optional, Lovelace/ApexCharts): `dashboards/*.yaml`

## 5. WebSocket API (panel data)

| Command | Returns |
|---------|---------|
| `powerpilot/plan` | full plan (`hours[]` + `forecast[]`) |
| `powerpilot/status` | checks (sensors/price source), learning days, module errors |
| `powerpilot/log` | last ~50 optimization runs |
| `powerpilot/profiles` | 7×24 price + consumption matrices |
| `powerpilot/forecasts` | D+1..D+3 horizon prices for a date (Pradcast) |
| `powerpilot/series` | unified past(real)+future(forecast) hourly series for charts |

## 6. How to run / test / build

```bash
# Logic tests (no HA) — seconds
python3 scripts/dev_pipeline_test.py
python3 scripts/dev_pradcast_test.py
python3 scripts/dev_consumption_test.py

# Integration tests (real HA loop)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-test.txt
pytest -q                      # tests/test_init.py, tests/test_websocket.py

# Frontend
cd frontend && npm install && npm run build   # → ../custom_components/.../powerpilot-panel.js
npx tsc --noEmit                               # type-check
```

Node 25 / npm 11 available locally. HA 2026.6.4 installed in `.venv`.
Validate JSON with the miniconda python (`/opt/miniconda3/bin/python`); the base
`python3` lacks `yaml`/`json` deps for some checks but works for `py_compile`.

## 7. Pradcast API (Stage 1 source)

- Auth header: `X-API-Key: pcast_...`. Endpoints under `https://api.pradcast.pl`.
- `/prices/date/{YYYY-MM-DD}` → `PriceResponse` (`prices[].price_kwh`, `horizon`
  null = confirmed RDN, set = forecast; `confidence`, `level`).
- `/prices/forecasts/{date}` → `{forecasts: {"D+1":{prices[...p10,p90]}, ...}}`.
- Range: 2026-06-01 .. D+3; confirmed hourly only ~last 7 days.
- ⚠️ The API key was shared in chat during the session — **rotate it** and set the
  new key only in the integration's config (never in source).

## 8. Gotchas / lessons

- **Test env has no `hass_frontend`** → `frontend` must NOT be a hard manifest
  dependency. `panel.py` registers the panel only if `"frontend" in
  hass.config.components`; WS + static path register regardless.
- The local shell wraps `curl`/`pip`/`npm`/`tsc`/`ls` with an `rtk`/uv wrapper;
  use `/usr/bin/curl` and the venv binaries directly when results look off.
- Heavy data goes through the **WebSocket API**, not entity attributes (16KB limit
  + recorder bloat). The `optimization_plan` sensor still exposes attributes for
  the optional ApexCharts dashboards.
- Consumption learning needs sensors with **long-term statistics** (`state_class`):
  energy `total_increasing`, power `measurement`.
- `grid_connected = SoC ≥ grid_disconnect_soc` is implemented literally per the
  brief ("false when SoC below XX%") — confirm this is the intended semantics.

## 9. Suggested next steps (pick up here)

1. **Interactive chart tooltip** (hover → hour values) — pure frontend.
2. **EV bars on the SoC chart** (`ev_charge_kwh` already in plan/series-able).
3. **Unit tests for distribution tariffs** (ValidityRange, `tariff_for_day`,
   base-component arithmetic, `workday.check_date` cache fallback, snapshot
   lazy create + 90-day prune).
4. **Stage 3 — EV + Apple calendar**: real EV need from calendar trips
   (home→event→home km), 3-phase shared-phase power coupling, "away" SoC band,
   hourly appliance events (washing 3 kWh/h, ironing 2 kWh/h), reminders.
5. **Options flow polish**: editable charge curve.
6. **Stage 4** weather/climate; **Stage 5** LP/MILP optimizer.

## 10. Quick resume checklist

- [ ] Read this file + `ROADMAP.md`.
- [ ] `cd frontend && npm install` (node_modules is gitignored).
- [ ] `source .venv/bin/activate && pytest -q` to confirm green baseline.
- [ ] Rotate the Pradcast API key if testing live prices.
- [ ] In HA, configure at least one Tariff (Options → "Taryfy dystrybucyjne").
      Add a catch-all *Pozaszczyt* period (0–24 h, no day_sensor) before any
      time-of-day periods so every hour resolves to some price.
