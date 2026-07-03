# EV Dynamic Planning — Implementation Plan

Status: **mostly implemented**. Sections 1–3 landed earlier (capacity learning,
drain profile, charge-meter exclusion); sections 4–5 and the EV-card part of 6
landed in `feat(ev): multi-calendar trips with Google Maps travel + min-SoC
planning` (2026-07-03). Deviations from the original proposal:

- **No keyword→target routing rules.** §4 proposed per-calendar
  `{keyword, target}` rules (incl. `device:<entity>` scheduled loads like
  `prasowanie`). Implemented instead: a flat `CONF_CALENDARS` entity list, the
  single EV keyword stays in the EV step, and *any* located event becomes a
  trip (no `ev_trip` keyword needed). Device scheduled loads remain **open**.
- **No straight-line fallback.** §5 suggested straight-line × road-factor on
  Maps errors; per the no-fallback rule the trip stays unavailability-only
  (event span + margins) and a warning is logged. Cache is 30 days per
  location, not daily.
- **Trip target = `min SoC + round trip`,** not `current + needed` — sized off
  the new writable `number.ev_min_soc` reserve entity, so the car always comes
  home at/above the reserve. Trip targets are a floor (never lower the
  routine ceiling); trip drain also feeds the projected EV SoC line and the
  allocator (buy-back before later deadlines, pack-room credit after trips).
- **Margins are new:** configurable unavailability margin before departure /
  after return (default 30 min each) around the travel-time window.
- **§6 partially done:** trips (km, travel minutes, energy), min SoC and red
  away-window/trip-minimum chart annotations live on the existing EV card; a
  dedicated EV tab is **open**.

Original proposal below, kept for the confirmed decisions and open items.

Confirmed decisions:

- **Trip distance:** Google Maps now (API key, geocode event location, home→event km).
- **Odometer:** available → learn kWh/km from odometer + SoC/energy.
- **Battery capacity:** learned from charging sessions; **no manual field**. EV charge
  planning is disabled until capacity is learned (panel shows "uczy się…").
- **Calendars:** moved to **general settings**, multiple calendars, keyword→target routing
  (e.g. `Kotek`→EV, `prasowanie`→a device load).

Guiding model (user's words): the **consumption profile is the base** for EV charge
planning (anticipate normal daily driving); **calendar trips are hard guarantees** that the
battery has enough before departure. Avoid **double-counting** EV energy in the household
consumption profile for archival data.

---

## 1. Data-derived battery capacity (learned)

**Goal:** replace the `ev_battery_kwh` config field with a value learned from charging
sessions: `capacity ≈ Δenergy_added / ΔSoC% × 100`.

- **Session tracker** (new, in EV module, persisted via `Store`):
  - Inputs: `ev_soc_sensor`, `ev_energy_added_sensor` (total_increasing, resets per session),
    `ev_charging_sensor` (optional).
  - Detect a session: energy-added rising / charging true → record `soc_start`, track
    `soc_end`, `energy_added`. Close when energy-added resets to ~0 or charging stops.
  - Emit a **capacity sample** only for clean sessions: `ΔSoC ≥ 15%` and `energy_added > 1 kWh`
    (small deltas amplify sensor noise). `sample = energy_added / (soc_end - soc_start) × 100`.
  - Aggregate: robust (median of last N≈10 samples). Persist samples + median.
  - **Bootstrap on startup:** backfill from recorder history (SoC + energy-added over ~30 days)
    so capacity isn't blank for days after a restart (mirrors the EV-SoC recorder seed pattern).
- **Readiness:** `capacity` is `None` until ≥ `MIN_CAPACITY_SAMPLES` (≈3) clean samples.
  While `None`, `EVRequest.is_actionable` is false → no EV allocation; panel shows
  "Pojemność: uczy się (N/3 sesji)".
- **Optimizer/EVRequest:** `battery_kwh` now comes from the learned value (already plumbed
  through `EVRequest.battery_kwh`). No optimizer change beyond sourcing it.

**Files:** `modules/ev.py` (tracker + Store + property), `coordinator.py` (pass learned
capacity into `EVRequest`), `const.py` (remove `CONF_EV_BATTERY_KWH`), new persistence key.

## 2. EV driving-consumption profile (learned)

**Goal:** a learned model of how the car drains, used to size routine charging.

- **kWh/km:** between charges, `Δodometer` km and `ΔSoC%` → `energy_out = ΔSoC% × capacity`;
  `kWh/km = energy_out / Δkm`. Robust-average over trips (`Δkm ≥ 2`). Needs `ev_odometer_sensor`.
- **Weekday×hour drain profile** (`WeeklyAccumulator`, reusing `profiles.py`): attribute the
  SoC drop (energy out of the pack) to the hours it occurred, learning a 7×24 expected-drain
  shape. This is the routine-driving forecast that charging anticipates.
- **Use in planning:** a daily/look-ahead **expected drain** (kWh) becomes a soft target — keep
  the pack able to cover the next ~24–48 h of predicted driving at cheap hours, *on top of*
  any calendar trip guarantees.

**Files:** `modules/ev.py` (odometer read + learning + Store), reuse `profiles.WeeklyAccumulator`,
new `CONF_EV_ODOMETER_SENSOR`, `optimizer._plan_ev` (add the routine-drain soft target as a
top-up deficit when no calendar event binds).

## 3. Double-counting fix (household profile vs EV charging)

**Problem:** if the EV charger is a metered device, its historical charging is in the household
consumption profile; `_plan_ev` then adds charging again → double count in the demand forecast.

**Fix:**
- Add a designation: the EV **charging** energy sensor (the charger sub-meter) is "owned by EV".
- In `consumption.contribute()` / `device_value()`, **exclude** the EV-charging device from the
  demand forecast (it is supplied by `_plan_ev`, not the base load).
- **Realized chart unchanged** — past EV charging really happened, so it still shows on the
  realized side and in the Profiles "raw" totals; only the **forecast demand** drops it.
- Driving drain is *not* in the house meter (it leaves the EV battery), so no house-profile
  double count there; it lives only in the EV drain profile (#2).

**Files:** `modules/consumption.py` (exclude EV-charging sensor from contribute), `const.py`
(reuse `ev_energy_added_sensor` or a new `ev_charge_meter` mapping), `coordinator` series
(keep realized EV charge as today).

## 4. Calendars → general settings, multiple, keyword routing

**Goal:** calendars are general (not EV-only); events can target EV *or* devices.

- **Config (general step):** `CONF_CALENDARS` = list of `{ calendar_entity, rules: [{keyword,
  target}] }`. Targets: `ev_charge` (Kotek), `device:<entity>` (e.g. `prasowanie`→ironing meter),
  `ev_trip` (location-bearing events). Remove `CONF_EV_CALENDAR` / `CONF_EV_CALENDAR_KEYWORD`
  from the EV step.
- **Real `CalendarModule`** (replace placeholder; move parsing out of `ev.py`):
  - Reads all configured calendars via `calendar.get_events`.
  - Routes each event by keyword → produces: EV deadline targets (`Kotek NN%`), EV forced
    windows (`Kotek`), EV trips (events with a location → #5), and **device scheduled loads**
    (`prasowanie` → kWh injected into that device's forecast at the event hours).
  - Exposes structured results the EV module and consumption module consume.
- Multiple calendars supported by iterating the list.

**Files:** new `modules/calendar.py` impl, `config_flow.py` (general calendars step + UI),
`const.py`, `modules/ev.py` (consume calendar results instead of reading itself),
`strings.json` + translations.

## 5. Google Maps trip distance → energy guarantee

**Goal:** a trip event guarantees enough SoC before departure, sized by real driving distance.

- **Config (general):** `CONF_GOOGLE_MAPS_API_KEY`. Home location from `zone.home` (lat/lon).
- For a trip event (keyword + a `location`): geocode/Distance Matrix (Routes API) home→location;
  round trip ≈ 2× legs (or there-and-back). `energy_needed = km × kWh/km` (from #2) × buffer.
- Produce a **deadline target**: by `event.start` (departure), `target_soc` = `current +
  energy_needed/capacity`, capped 100%. Feeds the existing `_plan_ev` deadline path.
- **Caching:** cache per (origin,destination) to avoid repeat API calls; refresh daily.
  Handle API errors gracefully → fall back to straight-line × road-factor, log a reminder.

**Files:** new `maps.py` (thin Distance Matrix client + cache), `modules/calendar.py` (trip →
target), `const.py`, `manifest.json` (no new pip dep — use `aiohttp` already in HA).

## 6. Frontend: dedicated EV section

Move all EV UI together (new "EV" tab or a large EV panel block):
- Telemetry (SoC, connected, charging, energy added).
- **Learned capacity** + confidence (N sessions, last sample, median) or "uczy się".
- **kWh/km** + driving drain profile (daily/weekly, like Profiles).
- **Calendar events:** upcoming deadlines, trips (with computed km + energy), forced windows.
- Planned charging (kWh, hours, cost) + control entities (existing).

**Files:** `frontend/src/powerpilot-panel.ts` (new tab + cards), new WS payload
`powerpilot/ev` (or extend `status.ev` + a new `ev_stats`), `coordinator.py` getters.

---

## Config migration

- Drop `ev_range_km`, `ev_weekly_km` (already effectively dead).
- Drop `ev_battery_kwh` field; **seed** the learned-capacity Store with the old value as a
  starting estimate so existing installs aren't blank on upgrade (then real samples refine it).
- Move `ev_calendar*` into the new general `calendars` list (migrate the single value).
- Add `ev_odometer_sensor`, `google_maps_api_key`.

## Suggested build order (incremental, each shippable)

1. **Capacity learning** + remove `ev_battery_kwh` field + readiness gating + panel readout.
2. **Double-count fix** (exclude EV charging from forecast demand).
3. **EV drain profile** (odometer kWh/km + weekly drain) + routine-drain soft target.
4. **Calendars → general settings**, multi-calendar, real `CalendarModule`, device events.
5. **Google Maps trips** → energy guarantee.
6. **Frontend EV section** consolidating all of the above.

## Risks / open points

- **Session detection robustness** depends on the energy-added sensor truly resetting per
  session; if it's a lifetime counter instead, capacity must come from `Δsoc` + a charge-power
  integral. Will verify against the real sensor's behaviour during build.
- **Capacity cold-start:** with "wait until learned", a brand-new install does nothing for EV
  until ~3 sessions. The old-value seed (migration) mitigates upgrades; fresh installs accept it.
- **Maps cost/quota:** mitigated by caching + daily refresh + graceful fallback.
- **kWh/km needs capacity**, so #2/#5 depend on #1 being learned first (or the seed).
- Tests: unit-test capacity estimator, drain profile, calendar routing, Maps client (mocked),
  double-count exclusion. Keep the existing 7 pre-existing failures out of scope.
