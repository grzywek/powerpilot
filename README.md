# PowerPilot

A Home Assistant custom integration that optimizes the use of a home battery /
inverter (e.g. Victron) under a **dynamic energy tariff**, factoring in household
consumption, EV charging, weather, heating/cooling and the calendar.

The goal: **minimize the cost of energy** by orchestrating charge / discharge /
passthrough, EV charging, and grid connection — while always knowing the
**price of the energy currently stored in the battery, after losses**.

## What it produces

For every hour of the horizon (as far as price data reaches), PowerPilot decides:

| Output | Values | Entity |
|--------|--------|--------|
| Inverter mode | charge / discharge / passthrough | `sensor.powerpilot_inverter_mode` |
| Charge power | full / limited (limited while EV uses the shared phase) | `sensor.powerpilot_charge_power` |
| Grid connected | on / off (off below the SoC floor) | `binary_sensor.powerpilot_grid_connected` |
| EV charge | on / off | `binary_sensor.powerpilot_ev_charge` |
| Battery energy cost | PLN/kWh after losses | `sensor.powerpilot_battery_energy_cost` |
| Full plan (chart data) | per-hour forecast + decisions | `sensor.powerpilot_optimization_plan` |

The `optimization_plan` sensor exposes the whole horizon as attributes
(`hours` + `forecast`), driving the two-chart dashboard in
[dashboards/powerpilot-dashboard.yaml](dashboards/powerpilot-dashboard.yaml)
(SoC/flows + prices incl. the battery-cost line). It needs the `apexcharts-card`
frontend card (HACS → Frontend).

### Sidebar panel

PowerPilot also ships a **custom sidebar panel** (Lit + TypeScript) that registers
itself automatically — a **PowerPilot** entry appears in the HA sidebar with three
tabs: *Overview* (SVG charts + current control + Configure button), *Status* (what
works / what's missing + learning progress), and *Logs* (recent optimization runs
and module errors). It is backed by a WebSocket API (`powerpilot/plan|status|log`),
not by entity attributes. See
[docs/INSTALL_AND_VERIFY.md](docs/INSTALL_AND_VERIFY.md) for how to verify and
[the frontend build notes](docs/INSTALL_AND_VERIFY.md#budowanie-frontendu-dla-deweloperów).

## Architecture

A small stable core surrounded by independent **modules** that each contribute one
slice of information to a shared hourly forecast, which the **optimizer** turns
into decisions. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and the staged
delivery plan in [docs/ROADMAP.md](docs/ROADMAP.md).

```
modules (prices · consumption · loads · weather · climate · ev · calendar)
        → ForecastBuilder → Optimizer (+ BatteryModel) → Plan → entities
```

## Installation

1. Copy `custom_components/powerpilot` into your Home Assistant `config/custom_components/`
   directory (or add this repository to HACS as a custom repository).
2. Restart Home Assistant.
3. Add the integration via **Settings → Devices & Services → Add Integration →
   PowerPilot** and complete the three-step setup (core → prices → EV).

## Status

Implemented: **Stage 0** (foundation — models, battery cost-after-losses, module
pipeline, heuristic optimizer, config flow, entities), **Stage 1** (prądcast.pl
price source with confirmed/forecast split + learned price profile), **Stage 2**
(recorder-based consumption learning with per-device breakdown), and **Stage 6**
(custom Lit sidebar panel + WebSocket API + dashboards).

Pending: Stage 3 (EV + Apple calendar), Stage 4 (weather/climate), Stage 5
(LP/MILP optimizer). Full plan in [docs/ROADMAP.md](docs/ROADMAP.md); resume guide
for a fresh session in [docs/HANDOVER.md](docs/HANDOVER.md).
