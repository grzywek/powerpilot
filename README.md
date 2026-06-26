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

### Versioning & releases

Versions are published automatically. On every push to `main`, the
[Release workflow](.github/workflows/release.yml) computes the next version from
the commit messages (conventional commits: `feat:` → minor, `feat!:` /
`BREAKING CHANGE` → major, anything else → patch), writes it into
`manifest.json`, tags it, and creates a matching **GitHub Release**. HACS reads
those releases, so each change shows up as a new selectable version to install —
no manual version bumping required.

## EV charging

PowerPilot can schedule EV charging into the cheapest hours and react to a
calendar. Everything is optional and configured in the **EV** step of the config
flow (Settings → Devices & Services → PowerPilot → Configure → 🚗 EV).

### Sensors

| Field | Type | Used for |
|-------|------|----------|
| EV SoC sensor | `%` | current charge level — sizes how much energy is still needed |
| EV target SoC sensor | `%` | the car's own charge target; becomes the default goal (instead of a fixed 80 %) |
| Charger connected | on/off | **availability gate** — the car only charges while plugged in (overrides the location tracker) |
| Charging now | on/off | plan-vs-reality check — warns when a charging window is due but the charger draws no power |
| Energy added this session | `kWh` (increasing) | how much energy the current session has delivered (shown in the panel) |
| EV location (home/away) | tracker | fallback availability signal when no "charger connected" sensor is set |

With neither a "charger connected" sensor nor a location tracker, the car is
assumed to be available.

### Calendar plans

Point PowerPilot at any Home Assistant `calendar.*` entity (Google Calendar,
CalDAV/iCloud, Local Calendar, …) and pick a **keyword** (default `Kotek`). The
keyword is your car's name in event titles; only events whose title starts with
it are read. Two kinds of events:

| Event title | Meaning |
|-------------|---------|
| `Kotek 100%` at 12:00–13:00 | **Deadline target** — be at 100 % SoC by the event **start** (12:00). The optimizer picks the cheapest available hours before that deadline. |
| `Kotek 50%` | Same, but to 50 %. |
| `Kotek` (no percentage) | **Forced window** — charge at full charger power for every hour the event covers, no SoC limit. |

So a **percentage** lets the optimizer choose *when* to charge (cheapest hours
before the deadline), while a **bare** event lets you choose the hours yourself
(charge flat-out during the window). Earlier deadlines are honoured before later
ones, and charging never pushes the pack past 100 %. When the calendar has no
matching upcoming events, PowerPilot simply tops the car up to the target SoC in
the cheapest hours.

The planned charging, upcoming deadlines and manual windows are shown on the
panel's **Status** tab.

## Status

Implemented: **Stage 0** (foundation — models, battery cost-after-losses, module
pipeline, heuristic optimizer, config flow, entities), **Stage 1** (prądcast.pl
price source with confirmed/forecast split + permanent price archive and
weighted weekday+hour estimate for the tail), **Stage 2**
(recorder-based consumption learning with per-device breakdown), and **Stage 6**
(custom Lit sidebar panel + WebSocket API + dashboards). **Stage 3** is partly
done: calendar-driven EV charging (deadline targets + manual windows) and charger
telemetry sensors.

Pending: rest of Stage 3 (trip-distance/away strategies), Stage 4
(weather/climate), Stage 5
(LP/MILP optimizer). Full plan in [docs/ROADMAP.md](docs/ROADMAP.md); resume guide
for a fresh session in [docs/HANDOVER.md](docs/HANDOVER.md).
