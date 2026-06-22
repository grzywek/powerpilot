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
                  │  prices · consumption · loads · weather   │
                  │  climate · ev · calendar                  │
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

### Forecast
A `Forecast` is an ordered list of `HourSlot`s, each carrying everything a module
knew about that hour: buy/sell price (and whether it is *confirmed* or
*forecast*), base consumption, extra loads (EV + scheduled), PV, temperature.

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
| `grid_connected` | bool | false when SoC below the configured floor |
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
  binary_sensor.py     grid_connected / ev_charge
  modules/
    base.py            PowerPilotModule + registry
    prices.py          dynamic-tariff price sources (confirmed > forecast)
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
