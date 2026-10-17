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
| ESS charge/passthrough start | timestamp of the ongoing-or-next hour needing the grid | `sensor.powerpilot_ess_charge_or_passthrough_start` |
| EV charge | on / off | `binary_sensor.powerpilot_ev_charge` |
| Battery energy cost | PLN/kWh after losses | `sensor.powerpilot_battery_energy_cost` |
| Full plan (chart data) | per-hour forecast + decisions | `sensor.powerpilot_optimization_plan` |

The `optimization_plan` sensor exposes the whole horizon as attributes
(`hours` + `forecast`), driving the two-chart dashboard in
[dashboards/powerpilot-dashboard.yaml](dashboards/powerpilot-dashboard.yaml)
(SoC/flows + prices incl. the battery-cost line). It needs the `apexcharts-card`
frontend card (HACS → Frontend).

### Sidebar panel

PowerPilot also ships a **custom sidebar panel** (Lit + TypeScript, ApexCharts)
that registers itself automatically — a **PowerPilot** entry appears in the HA
sidebar with ten tabs: *Przegląd* (energy and price charts, the current control,
the apartment-supply card), *Ceny* (the hourly price archive), *Symulacje*
(forecast accuracy against what happened), *Przepływ* (the live pipeline),
*Sprawność* (measured vs configured efficiencies), *Status* (what works and
what is missing), *Diagnostyka*, *Profile* (learned consumption profiles),
*Logi* and *Debug* (a full state export). It is backed by a WebSocket API
(`powerpilot/plan`, `status`, `log`, `profiles`, `series`, `prices`,
`accuracy`, `flow`, `efficiency`, `debug`, `diagnostics`,
`consumption_stats`), not by entity attributes. See
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
| Charger connected | on/off | **availability gate for the running hour**, both ways: unplugged → this hour drops out of the plan; plugged → this hour is chargeable even if a calendar trip said the car would already be gone (overrides the location tracker) |
| Charging now | on/off | plan-vs-reality check — warns when a charging window is due but the charger draws no power |
| Energy added this session | `kWh` (increasing) | how much energy the current session has delivered (shown in the panel) |
| Charger behind the meters | toggle | **off** = the charger taps in before the house meters (own metering point): grid import never sees EV charging, so the realized import and hour costs add the car's session energy back (÷ charging efficiency, skipping away-trip hours) |
| EV location (home/away) | tracker | fallback availability signal when no "charger connected" sensor is set |
| Charging power per phase | `kW` | the charger's per-phase power |
| Charger phases | `1` / `3` | number of phases — total charge power is *per-phase × phases* (e.g. 3.5 kW × 3 = 10.5 kW) |

With neither a "charger connected" sensor nor a location tracker, the car is
assumed to be available. EV charging always runs at the **full** charger power
(per-phase × phases) for any hour it's scheduled — never a throttled fraction —
clipped only by the pack's 100 % ceiling on the final hour.

A trip does not write off the hour it starts in: leaving at 13:40 still leaves
40 chargeable minutes, so that hour is planned as a **partial** one (its yield
and its charging minutes scale down accordingly). The hour the car comes back in
stays out of the plan — its free minutes sit at the *end* of the hour, where a
charger started at the top of the hour cannot reach them.

### Calendar plans

Point PowerPilot at any Home Assistant `calendar.*` entity (Google Calendar,
CalDAV/iCloud, Local Calendar, …). Everything is steered by **`#tags`** in the
event title. The car's tag stem is your **keyword** (default `Kotek`), so the
car reads `#kotek`; the house battery is always `#ess`.

| Tag | Where | Meaning |
|-----|-------|---------|
| `#kotek_soc100` | event **without** a location | **Deadline** — be at 100 % SoC by the event's **start**. The optimizer picks the cheapest available hours before it. |
| `#kotek_soc100` | event **with** a location (a trip) | **Deadline at departure** — be at 100 % when the car actually leaves, which is earlier than the event's start by the travel time. Raises the trip's automatic target; it can never lower it below what the drive needs. |
| `#kotek` | event without a location | **Forced window** — charge the car at full charger power for every hour the event covers. |
| `#ess_soc80` | any event | **Deadline** — the house battery at 80 % by the event's **start**. The battery never leaves, so there is no departure to aim at. |
| `#ess` | any event | Charge the house battery in the event's hours regardless of price. A preference, not a hard rule: on a full pack it simply does nothing. |
| `#ignore` | any event | PowerPilot skips the event entirely. |
| `#continue` | event with a location | This stop is driven to **directly from the previous located event** (same nesting level) — e.g. Gliwice 15–16 and Katowice 17–18 `#continue` plan dom→Gliwice→Katowice→dom instead of two separate round trips, and the charge for the whole tour must be in the pack before the first departure. |

So `_socNN` lets the optimizer choose *when* to charge (cheapest hours before
the deadline), while a bare tag lets you choose the hours yourself. Tags are
case-insensitive, a trailing `%` is tolerated (`#kotek_soc100%`) and a comma
decimal works (`#kotek_soc87,5`). Several tags can share one event, e.g.
`Babcia #kotek_soc100 #ess_soc80`. Earlier deadlines are honoured before later
ones, and no target is ever planned past what the pack can physically hold.
With no tagged events PowerPilot simply tops the car up to the target SoC in
the cheapest hours.

Events with a **location** become trips: the car is away, the drive drains the
pack, and a pre-departure charge target is added automatically.

An event that fully contains other located events changes their base: sub-events
of an all-day "Kraków" event drive from/back to Kraków, not home — and a
sub-event ending exactly when its parent ends returns straight home (the drive
home replaces the parent's own return leg).

The planned charging, upcoming deadlines and manual windows are shown on the
panel's **Status** tab, and the forecast EV SoC is drawn as a dashed line on the
energy chart's SoC axis.

### Steering the charger

PowerPilot decides *when* and *to what level* to charge, then exposes that as
entities so a Home Assistant **automation** does the actual steering (start/stop,
set the SoC limit). It does not drive the charger itself.

| Entity | Type | Meaning |
|--------|------|---------|
| `binary_sensor.powerpilot_ev_connect_charger` | on/off | charging is planned within the next 24 h — plug in / enable the charger |
| `sensor.powerpilot_ev_charge_start` | timestamp | when the next charging hour begins (HA shows a live "in X" countdown) |
| `binary_sensor.powerpilot_ev_charge` | on/off | a charging hour is active right now |
| `sensor.powerpilot_ev_soc_limit` | `%` | the SoC the car should stop at: the next `#<keyword>_socNN` calendar target, otherwise the planned SoC at the end of the next charging window (empty when nothing is planned) |

Charging always runs at the full charger power, so there is no separate
power-setpoint entity — the automation just starts/stops the charger.

## Monthly price cap (Tarcza)

Some sellers cap the **monthly weighted-average** energy price: at month end
the bill per point of delivery is `min(cost, limit × kWh)`. That is a
settlement over the whole month, not an hourly cap — and while it binds, every
further kWh costs the limit whatever the hour, so battery arbitrage on the
energy price stops paying and only distribution differences matter.

The terms change between editions, so each edition is configured on its own
(options → *Tarcza* → *Dodaj Tarczę*):

| Field | Tarcza Pstryk 2026 | Tarcza Pstryk 2027 |
|-------|--------------------|--------------------|
| Valid | 2025-01-01 … 2026-12-31 | 2027-01-01 … 2027-12-31 |
| Limit | 0.61 PLN/kWh | 0.50 PLN/kWh |
| Limit includes VAT | yes (gross) | no (net) |
| Handling fee counts in the average | yes | per the regulations' wording only "energy cost" — confirm with the seller |

An edition spans whole months and editions may not overlap; a month no edition
covers is billed at hourly prices. Excise and distribution are always outside
the cap. A net limit is grossed up with the VAT of the price settings, so every
figure below is gross.

PowerPilot models it end to end:

- the month-to-date import of PPE 1 is read from the configured grid-import
  meters (*Tarcza* → *Liczniki importu PPE 1*; summed, an hour missing from any
  of them is unknown, never zero) and priced from the price archive under that
  month's edition. Statistics the recorder is still compiling — the hour that
  just ended, the first minutes of the running one — are pending for up to
  15 minutes (an hour that just ended is read from its 5-minute buckets), not
  gaps;
- with apartment supply detection on (see below), the month-to-date import of
  PPE 2 is the apartment meter's energy in the hours the apartment was on the
  second meter, under the same edition as PPE 1: Victron hours count 0, an hour
  containing a switch is split at the switch by the 5-minute statistics (each
  completed split is archived in `.storage/powerpilot_<entry>_billing_ppe2`,
  since the recorder purges 5-minute statistics after about ten days), and an
  hour whose supply is unknown is a gap. The plan settles PPE 2's months next
  to PPE 1's (its outcome is in `plan.billing` with `ppe = ppe2`);
- the optimizer settles each capped month it touches as `min(C, limit × E)` and
  plans the battery under that regime, pricing the rest of the month from the
  weekly consumption profile, the archive's price estimate and the learned EV
  driving profile;
- when the cap cannot be modelled (unusable editions, incomplete month data,
  unpriced tail hours, no EV driving profile yet) the plan says why and the
  battery is planned without it — no substitute data.

| Entity | Meaning |
|--------|---------|
| `sensor.…_tarcza_ppe1_energy` | observed import since the month started (kWh) |
| `sensor.…_tarcza_ppe1_average` | observed weighted average so far (PLN/kWh, pre-cap) |
| `sensor.…_tarcza_ppe1_forecast_average` | forecast month-end average (observed + plan + tail) |
| `sensor.…_tarcza_ppe1_discount` | forecast discount the cap yields this month (PLN) |
| `binary_sensor.…_tarcza_ppe1_cap_binds` | the plan expects the cap to bind this month |
| `sensor.…_tarcza_ppe2_energy` | PPE 2's observed import since the month started (kWh) |
| `sensor.…_tarcza_ppe2_average` | PPE 2's observed weighted average so far (PLN/kWh, pre-cap) |

All of them carry the full settlement record (hours covered, gaps, reasons) as
attributes.

## Apartment supply (second meter)

A manual switch moves the apartment between the Victron output (PPE 1) and the
second point of delivery (PPE 2). Nothing reports its position, so PowerPilot
tells it from power and keeps the answer in `sensor.…_apartment_supply`:
`Victron (PPE 1)`, `Meter 2 (PPE 2)`, or unknown until the first confirmation.
Its attributes carry the last evidence, the thresholds and any storage errors.

**The rule.** Every 30 s PowerPilot compares the apartment power `a` with
`r` = Victron output power minus the other sub-meters behind the Victron.
Below the minimum apartment power (default 200 W) the sample decides nothing;
at lower loads the meters disagree by up to ~100 W, and 200 W was the lowest
minimum without a false meter-2 episode in a 14-day replay of live samples.
Otherwise it points to meter 2 when `r < share · a` (the Victron covers less
than the share of the apartment, default 0.5) and to the Victron when not. The
rule is proportional because the apartment meter and the Victron disagree by a
few percent at sustained loads and do not report at the same instant; an
absolute margin gave false meter-2 episodes on live data. A change is confirmed
after N such samples in a row (default 10); until then the last confirmed state holds. Undecided samples
neither count nor reset a streak. The confirmed changes are kept in a log of
their own and, after a restart, rebuilt from the recorder's state history;
hours older than that are classified from hourly means.

**Configuration.** Options → *Zasilanie mieszkania*: the apartment power
sensor, the Victron output power sensor, the other sub-meters behind the
Victron, the metered device that is the apartment, and the three thresholds (minimum power 200 W, Victron share 0.5, N = 10).
The same form holds the plan's switching rules: whether to recommend switches
at all (off by default), the cycle threshold (2 zł), the minimum stay on one
source (2 h) and the quiet window (00:00–09:00).
Leave the three sensors empty to turn detection off. The log is evidence read
through the rule, so changing any threshold sets the old log aside (renamed,
not deleted) and rebuilds it from the recorder's history. The apartment meter must
sit directly under the main consumption meter (options → *Hierarchia
liczników*); the form refuses anything else.

**What changes.**
- Learning: in hours the apartment spent on meter 2 the Victron's base
  profile is no longer reduced by the apartment's consumption (it was not in
  the Victron's reading), while the apartment and the meters under it keep
  learning in every hour, whatever fed them. Hours with a mixed or unknown
  supply give no base sample rather than a guess, and a missing sub-meter
  reading drops the hour instead of counting as zero. A negative remainder
  (sub-meters above the node's own reading) is kept as is and counted: the
  unmetered base is small, so its hourly value is meter noise around zero and
  the raw values average to the truth, while dropping or clamping them biases it.
  The profiles relearn once after the upgrade and again after you enable
  detection or change the apartment meter.
- Forecast and chart: the apartment's subtree (including climate sensors
  inside it) is forecast apart from the Victron demand, and the optimizer adds
  it to the Victron or to PPE 2 per each hour's planned supply (every plan
  hour carries `supply`). The plan may move the apartment to meter 2 when
  that pays more than the cycle threshold under both meters' price caps —
  never in the quiet window, during calendar trips or when a calendar cannot
  be read, and not sooner than the minimum stay after the last switch. It does
  so only with the supply known and both meters' months complete; otherwise
  it keeps the confirmed state for the whole horizon and says why
  (`supply_reason`). Switches are modelled only with recommendations turned
  on: a plan assuming switches nobody makes would size the battery for the
  wrong load. The chart's per-device forecast follows each hour's supply —
  the plan's for planned hours, the logged one for past hours — so the
  apartment's bars and the Victron consumption line agree.
- Recommendations (opt-in: options → *Zasilanie mieszkania* → recommend
  switches; off by default): the plan's runs of meter-2 hours are cycles
  (id = planned start, or the actual switch time for the one running). Each
  proposed cycle is re-solved without it, its hours held on the Victron; one
  saving less than the threshold is dropped and the plan re-solved, until
  every cycle clears it. `sensor.…_apartment_supply_recommended` is the next
  hour's planned supply, `binary_sensor.…_apartment_switch_required` is on
  while it differs from the actual one (switch before the next full hour),
  and while it is on a persistent notification is shown once per cycle and
  direction — "Przełącz mieszkanie na licznik 2 do HH:00, powrót ok. HH:00,
  oszczędność ~X zł", or the switch back to the Victron at the cycle's end —
  and dismissed once the switch is detected or re-planned away. A confirmed
  switch (or a log rebuilt with another transition time) re-plans at once,
  not at the next hour.
  `sensor.…_apartment_supply_plan` (number of cycles; cycles with start,
  return, hours, kWh and saving, and the hourly planned supply as
  attributes), `…_apartment_next_switch` (target, planned return, cycle id),
  `…_apartment_cycle_savings` (the running or next cycle, PLN) and
  `binary_sensor.…_apartment_switch_available` (not in the quiet window, not
  on a trip, calendars readable) complete the set, and two numbers set the
  cycle threshold and the minimum stay (they write the options, which
  reloads the integration). While switches are not modelled each entity
  carries the `reason`. PowerPilot does not operate the switch; an
  automation on `switch_required` can push to a phone or drive a contactor.
- Panel: the overview chart shades the hours the apartment was on meter 2
  (solid teal, from the supply log) and the hours the plan puts it there
  (light teal, dashed), labelled "Mieszkanie na liczniku 2". The card
  *Zasilanie mieszkania* below it shows the actual supply and since when, the
  next hour's recommendation and whether a switch is required now, the next
  switch, the cycles (start → return, hours, kWh, saving), the threshold,
  minimum stay and quiet window, the reason when nothing is recommended or
  switching is not modelled, and a Tarcza block per meter (average so far,
  forecast month-end average against the limit, forecast discount).
- Unknown: the apartment is planned on the Victron, and the plan carries a
  reminder that its supply is not known yet.

**Diagnostics.** The panel status and the debug export have a `supply` section:
state, since, history coverage, checkpoint, the last 50 transitions (with the
total), storage/replay/save errors, thresholds, configured sensors, how many
hours the last consumption learning run dropped or kept negative
(`learning_dropped`), the `recommendation` the entities show and the switching
`rules`; a `tarcza` section carries both meters' observed month and the plan's
forecast of it. The chart series marks each hour's `supply_actual` (past) and
`supply_planned` (plan).
`scripts/backtest_supply_detector.py` checks the rule against the PPE 2 import
statistic on exported hourly data; its docstring explains how to export it.

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
