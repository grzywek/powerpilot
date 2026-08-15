# PowerPilot – Architecture

PowerPilot is a Home Assistant **custom integration** that optimizes the use of a
home battery / inverter (e.g. Victron) under a **dynamic energy tariff**, taking
into account household consumption, EV charging, weather, heating/cooling and the
calendar.

The guiding principle is **modularity**: a small, stable core surrounded by
independent *modules* (providers) that each contribute one slice of information to
a shared hourly **forecast**, which the **optimizer** turns into concrete inverter
decisions.

```
                       ┌─────────────────────────────────────────┐
                       │              Home Assistant              │
                       │  sensors / calendar / weather / numbers  │
                       └───────────────┬─────────────────────────┘
                                       │ reads
                  ┌────────────────────▼─────────────────────┐
                  │                MODULES                    │
                  │  prices · tariff · consumption · loads ·  │
                  │  weather · climate · ev · calendar        │
                  └────────────────────┬─────────────────────┘
                                       │ contribute to
                  ┌────────────────────▼─────────────────────┐
                  │            ForecastBuilder                │
                  │   builds hourly Forecast (as far as the   │
                  │   price data reaches)                     │
                  └────────────────────┬─────────────────────┘
                                       │ feeds
                  ┌────────────────────▼─────────────────────┐
                  │              Optimizer                    │
                  │   + BatteryModel (cost-after-losses)      │
                  │   → schedule of Decisions per hour        │
                  └────────────────────┬─────────────────────┘
                                       │ exposed via
                  ┌────────────────────▼─────────────────────┐
                  │   Coordinator → Sensors / BinarySensors   │
                  │   inverter_mode · charge_power · grid ·   │
                  │   ev_charge · battery_energy_cost · plan   │
                  └──────────────────────────────────────────┘
```

## Core concepts

### Hourly slot
The unit of planning is one hour (`HourSlot`). The horizon stretches **as far as
price data is available** (typically D+1 confirmed + several days of forecast).
The first slot is the clock hour the plan is computed in: mid-hour only its
**remainder** is plannable, so the optimizer scales that slot's charge/discharge
caps, forecast consumption and EV charger yield by the minutes left (powers in
the decision stay true kW). Mid-hour re-plans — a calendar edit, the EV being
plugged/unplugged, a restart — are therefore physically consistent and may
legitimately change the running hour's action.

### EV availability: calendar predicts, the plug decides
Calendar trips are the only source of EV *unavailability*, but they are a
**forecast** of absence. The plug sensor is ground truth about the present, so it
overrides the calendar **for the running hour in both directions**: unplugged
drops that hour from the plan, still plugged brings it back even when the trip
window says the car should already be gone (it left late, came home early, the
event overran) — and the hour's predicted drive drain is dropped with it, since a
car on the cable is not on the road. Only the running hour is re-opened; whether
the car is still home in two hours is genuinely unknown, so later hours stay
calendar-driven and each one re-opens as it becomes current.

Availability is not all-or-nothing per hour either: the hour a trip departs in is
carried as a **fraction** (`EVRequest.hour_fraction`), so a 13:40 departure keeps
40 chargeable minutes instead of writing the hour off. The allocator scales that
hour's yield and its charging minutes by the fraction; for the running hour the
value already folds in the minutes elapsed. The RETURN hour stays unavailable —
its free minutes sit at the end of the hour, out of reach of a charger started at
the top of it.

### Vintage and plan revisions
One snapshot of the inputs + plan is persisted per clock hour (a **vintage**),
recorded on the first run of that hour. It stays frozen for the rest of the hour:
it is the plan the hour *started with*, the only honest baseline for measuring
forecast accuracy against what actually happened.

Mid-hour re-plans are appended to it as **revisions** — `{at, why, ev, chg, pw,
grid, mode}` — instead of overwriting it. Without them, the panel's *prognoza*
column shows the hour-start plan while reality reflects a plan revised at :10,
and the resulting delta cannot be told apart from the plan having simply been
wrong. A revision is recorded only when the running hour's plan *materially*
changes — inverter mode, whether the car charges, or the charge power. The kWh
figures are not compared: slot 0 covers only the hour's remainder, so they shrink
on every mid-hour re-run and would mark every refresh as a change.

The `why` comes from the reactive listener, which knows which entity flipped
("kabel EV: podłączono", "kalendarz"), so the record names its cause. The series
payload also carries `ev_off_plan`: EV energy realized in an hour that neither
its vintage nor any of its revisions asked for — charging PowerPilot never
planned, as opposed to charging it decided on mid-hour. Vintages recorded while
revision tracking is active are marked `revcap`; hours without that marker are
never flagged, since their silence means nobody was watching for re-plans rather
than that none happened.

### Forecast
A `Forecast` is an ordered list of `HourSlot`s, each carrying everything a module
knew about that hour: buy/sell price (and whether it is *confirmed* or
*forecast*), base consumption, extra loads (EV + scheduled), PV, temperature.

### Energy price vs distribution price
The price from prądcast (or any sensor) is only the **commodity** price of
energy. The household actually pays `energy + distribution`, where distribution
depends on time-of-day, workday vs weekend/holiday and the calendar season.
PowerPilot models this split explicitly:

- `HourSlot.buy_price` is the commodity price (set by the `prices` module).
- `HourSlot.distribution_price_kwh` is the regulated distribution surcharge
  for the hour (set by the `tariff` module).
- `HourSlot.total_price_kwh = buy_price + distribution_price_kwh` is what the
  optimizer uses for every cost decision.

A `Tariff` carries:
- a list of `ValidityRange`s — one tariff can cover several seasons (e.g.
  "G12 zima" active for 2024/25 *and* 2025/26 winters);
- a flat `base_component_kwh` surcharge added to every matching period
  (jakościowa + kogeneracyjna + OZE);
- a list of `TariffPeriod`s, each pinned to an optional `binary_sensor`
  (`workday` etc.) and a `start_hour..end_hour` window (wrap-around safe).

Future-day workday classification is pre-fetched via the HA
`workday.check_date` service so the same `binary_sensor.workday` entity can
report on each day in the 4-day horizon.

### BatteryModel and cost-after-losses
A central requirement: **PowerPilot must always know the price of the energy
currently stored in the battery, after losses.**

The battery is modelled as a reservoir with a **weighted-average cost** (PLN/kWh):

- **Charging** `g` kWh from the grid at price `p`:
  - energy actually stored: `g · η_charge`
  - cost of that stored energy: `g · p` (grid) `+ g · η_charge · wear_cost`
  - the reservoir cost becomes the weighted average of old and new energy.
- **Discharging** to deliver `d` kWh to the house:
  - energy drawn from the reservoir: `d / η_discharge`
  - cost of delivered energy: `reservoir_cost / η_discharge + wear_cost`

This `battery_energy_cost` is what the optimizer compares against the live grid
price to decide *charge / discharge / passthrough*.

### Optimizer
The first implementation is a **transparent heuristic** (price-percentile +
SoC-aware greedy). The interfaces are designed so it can later be swapped for an
LP/MILP solver without touching the modules or the HA glue (see `ROADMAP.md`).

Decision outputs per hour:

| Output | Type | Meaning |
|--------|------|---------|
| `inverter_mode` | charge / discharge / passthrough | what the inverter should do |
| `charge_power` | full / limited | limited when EV draws from the shared phase |
| `ev_charge` | bool | whether to charge the EV this hour |
| `reminders` | list | e.g. "plug in the car when you get home" |

## Module contract

Every module implements `PowerPilotModule`:

```python
class PowerPilotModule(Protocol):
    domain: str
    async def async_setup(self) -> None: ...
    async def async_update(self) -> None: ...
    def contribute(self, forecast: MutableForecast) -> None: ...
```

`contribute` only *adds* information to slots; modules never read each other
directly, which keeps them decoupled and independently testable.

## Directory layout

```
custom_components/powerpilot/
  __init__.py          integration setup / unload
  manifest.json
  const.py             keys, defaults, enums-as-strings
  models.py            HourSlot, Forecast, Decision, Plan
  battery.py           BatteryModel (cost-after-losses)
  optimizer.py         heuristic optimizer
  forecast.py          ForecastBuilder
  coordinator.py       DataUpdateCoordinator running the pipeline
  config_flow.py       config + options flow
  sensor.py            output + plan sensors
  binary_sensor.py     ev_charge / ev_connect_charger
  modules/
    base.py            PowerPilotModule + registry
    prices.py          dynamic-tariff price sources (confirmed > forecast)
    tariff.py          distribution-tariff module (energy + distribution split)
    consumption.py     weekly consumption profile from a power sensor
    loads.py           scheduled extra loads (washer, dishwasher, ironing)
    ev.py              EV SoC / location / range → charging need
    weather.py         hourly temperature forecast
    climate.py         heating/cooling energy vs outside temperature
    calendar.py        Apple Calendar → trips → EV charging plan
  strings.json
  translations/{en,pl}.json
```

See `ROADMAP.md` for the staged delivery plan.
