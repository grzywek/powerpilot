# Stage 2: apartment supply state and switch-aware load model

Status: Done 2026-10-17 (slices 1–4); accepted 2026-10-17 (open questions
answered at the end). Binds the executing agent.

Parent spec: `docs/apartment-supply-planning.plan.md` (stage 2 of 4; its
"Ustalenia z 2026-09-14" are settled and are not re-asked). Stage 1 (price cap
on PPE 1) shipped in v2.4.0–v2.4.2.

## Goal

Know, at any moment and for any past hour, whether the apartment was fed from
the Victron (`victron`, PPE 1) or from the second meter (`meter_2`, PPE 2), and
learn and forecast the household load with the topology that was really in
place. Stage 2 recommends nothing and never drives the switch; switching
recommendations, PPE 2 billing and the MILP source variables are stage 3.

## Settled (parent spec, 2026-09-14)

- State from power: `r` = Victron output power L1 − Σ power of the other
  sub-meters behind the Victron, `a` = apartment power. Decide only when
  `a ≥ P_min` (300 W): `victron` when `r ≥ a − margin` (100 W), else `meter_2`,
  after N agreeing samples in a row (3). Otherwise the last confirmed state
  holds, persisted. Before the first confirmation the state is unknown. All
  three thresholds are configurable. The ambiguous zone (apartment below the
  unmeasured loads) is known and accepted.
- Hours containing a switch are split by the recorder's state history, never
  assigned whole by the state at their end. Without the resolution to split,
  the hour is not a learning sample.
- The apartment profile (with its sub-meters) is independent of the source.
  The fixed loads behind the Victron exclude the apartment only in intervals
  where the Victron really fed it. Garage loads are always behind the Victron;
  the EV charger is before the inverter and unaffected.
- Missing telemetry never switches to a user declaration automatically.

## Facts from the live installation (2026-10-17)

- Inputs: `sensor.ess_multiplus_ii_output_power_l1` (Victron output L1),
  `sensor.garage_server_socket_power` (the only configured sub-meter behind the
  Victron outside the apartment), `sensor.tongou_m3_power` (apartment; its
  children boiler, wardrobe and AC are already parented under
  `tongou_m3_energy`). All update every few seconds (≈500–700 states per
  hour).
- `sensor.tongou_victron_out_*` has not changed since December 2025 — a dead
  meter. It must not be used as an input.
- Ground truth: `tauron_importer:3744162_60113405_3744162_consumption` is PPE 2's
  hourly import (143.5 kWh Jan–Aug 2026, equal to Tauron's yearly "mieszkanie"
  counter). The apartment was on PPE 2 in parts of January–August, not since.
- Backtest of the settled rule on hourly means, Jan–Aug where every input had
  statistics (apartment power exists for ~67 days): 611 decisive hours, 36 of 37
  PPE 2 hours found, no false `meter_2`, one miss; 1010 hours undecided
  (apartment < 300 W). Indicative only: the real detector samples seconds, not
  hourly means.
- Recorder state history reaches 14–19 days back; 5-minute statistics at least
  12 days; hourly statistics are long-term.

## Design (decided here, with reasons)

1. **`modules/supply.py` (`SupplyModule`) owns the state.** It samples the
   configured inputs on a fixed 30 s tick (not on every state change: the
   Victron updates every few seconds, and N must mean a duration, ~1–1.5 min
   at N = 3). A sample counts only when every input is available and numeric;
   an unavailable input breaks the streak, it never counts as agreement.
2. **Transitions are an append-only log** in its own `Store`
   (`powerpilot_<entry>_supply`): `{state, at, not_before, evidence}` where `at`
   is the first of the N agreeing samples and `not_before` the last decisive
   sample of the previous state. The switch happened inside
   `[not_before, at]`; when that window is longer than the sample spacing
   (apartment idle in between), the hours it overlaps are *uncertain*. Writes
   are atomic; an unreadable store is moved aside (`.corrupt-<date>`) and
   reported, never overwritten with an empty log.
3. **Seeding from history at setup.** The same detector replays the recorder's
   state history of the inputs (as far as it reaches) to fill the log before
   live sampling starts — one mechanism, older samples. Days beyond that reach:
   see open question 2.
4. **Per-hour state** comes from one function, `supply_for_hour(hour) ->
   victron | meter_2 | mixed | unknown`, used by learning, the chart and
   diagnostics alike. `mixed` = the hour contains a transition or overlaps
   its `[not_before, at]` window. Stage 2 does not split energy inside an
   hour: a mixed hour is simply not a learning sample for the root (switches
   are a few a month). Splitting by the recorder's state history stays for
   stage 3, where PPE 2 billing needs it.
5. **State-aware meter tree.** `hierarchy.exclusive_series` takes the per-hour
   state: `victron` hours as today; `meter_2` hours detach the apartment
   subtree from the root (root exclusive = root − its other children);
   `mixed` and `unknown` hours give no root sample (the apartment and its own
   sub-meters stay learnable, they do not depend on the source). While rewriting it,
   two existing violations of the data rules go: a missing child reading makes
   the parent's hour unknown (today it counts as 0), and a negative exclusive
   value is skipped and counted in diagnostics (today it is clamped to 0).
6. **Forecast demand.** The Victron-side demand is fixed loads + the apartment
   profile while the apartment is on the Victron, fixed loads only while on
   PPE 2. With no switching plan yet, the forecast keeps the current confirmed
   state for the whole horizon. Unknown state: the apartment is included, as
   before this feature, and the entity and the plan's reminders say the state
   is unknown (answer 1).
7. **Topology fingerprint** includes the supply configuration, so the existing
   "meter tree changed → relearn" path rebuilds the profiles once, with the
   new attribution.
8. **Surfaces.** `sensor.…_apartment_supply_actual` (enum `victron` /
   `meter_2`, `unknown` before confirmation; attributes: since, `not_before`,
   last evidence `a`/`r`, streak, thresholds). Status/websocket and the debug
   export carry the transition log. No panel timeline yet (stage 3).
9. **Configuration** (options → *Zasilanie mieszkania*): apartment power
   sensor, which metered device is the apartment (its subtree moves with it),
   Victron output power sensor, sub-meters behind the Victron, `P_min`,
   margin, N. Unconfigured = today's behavior, unchanged.

## Data model and interfaces

Constants in `const.py`: `SUPPLY_VICTRON = "victron"`, `SUPPLY_METER_2 =
"meter_2"`; hour kinds add `"mixed"` and `"unknown"`. Config keys:
`apartment_power_sensor`, `apartment_meter` (one of the metered devices; its
subtree is the apartment), `victron_output_power_sensor`,
`victron_other_power_sensors` (list), `supply_min_apartment_w` (300),
`supply_margin_w` (100), `supply_confirm_samples` (3). Supply detection is on
when the three sensor keys are set.

```python
# models.py
@dataclass(frozen=True)
class SupplyTransition:
    state: str                 # SUPPLY_VICTRON | SUPPLY_METER_2
    at: datetime               # first of the N agreeing samples (aware, UTC)
    not_before: datetime | None  # last decisive sample of the previous state;
                               # None for the first confirmation ever
    apartment_w: float         # evidence at confirmation
    victron_net_w: float       # Victron output − other sub-meters
    source: str                # "live" | "replay"
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> "SupplyTransition": ...  # ValueError if unusable

# modules/supply.py (pure part, no Home Assistant)
def classify_sample(apartment_w: float, victron_net_w: float,
                    min_apartment_w: float, margin_w: float) -> str | None:
    """None = undecided (apartment below the minimum)."""

class SupplyDetector:
    def __init__(self, min_apartment_w, margin_w, confirm_samples,
                 state: str | None = None) -> None: ...
    def observe(self, at: datetime, apartment_w: float | None,
                victron_net_w: float | None) -> SupplyTransition | None:
        """Feed one sample; return a transition when one is confirmed."""
```

Detector semantics: a sample with any input missing resets the streak; an
undecided sample neither counts nor resets; a decisive sample agreeing with
the confirmed state resets the streak and becomes the new `not_before`
candidate; a decisive sample of the other state extends (or starts) the
streak; the N-th one in a row confirms, with `at` = the first sample of the
streak. The first confirmation from unknown works the same with
`not_before = None`.

`SupplyModule(PowerPilotModule)` wraps it: 30 s tick
(`async_track_time_interval`) reading the current states, an append-only log
persisted through `Store` (`{DOMAIN}_{entry_id}_supply`, `{"transitions":
[...]}`), `state` / `since` properties, `supply_for_hour(hour) -> str`, and
`hourly_state(hour_means) -> str | None` for history older than the log
(answer 2). The sensor entity reads the module.

## Slices (each builds, is tested and committed on its own)

1. Model + detector + log + store + entity + config. Tests: rule and
   thresholds, streak broken by unavailable input, undecided zone keeps the
   state, unknown before the first confirmation, restart restores the state,
   corrupt store moved aside.
2. History replay at setup + `supply_for_hour` (`mixed` hours) + hourly-mean
   classification for history older than the log. Tests: replay of a captured
   real state sequence (fixture from the recorder, fake identities only), a
   switch mid-hour, a long `[not_before, at]` window making several hours
   mixed, hourly classification skipping undecided hours and hours next to a
   change.
3. State-aware `exclusive_series`, learning and demand contribution. Tests:
   `meter_2` hour detaches the subtree, missing child ≠ 0, negative exclusive
   skipped and counted, demand without the apartment while on PPE 2,
   stage-1 PPE 1 billing unchanged.
   Slice 3 in detail:
   - Past hour states for learning come from one function on the supply
     module: the log (`supply_for_hour`) from `covered_from` on; before it the
     hourly classification turned into states — the state of a decided hour
     carries forward through undecided and missing hours until the next
     decided hour; when that one has the other state, every hour between them
     plus the two hours next to the change is `mixed`; before the first
     decided hour `unknown`. Where the hourly states meet the log, a state
     differing from the log's first transition makes the hours between the
     last decided hour and that transition's `at` `mixed`.
   - `exclusive_series` gains the per-hour supply and the apartment meter
     (`apartment_meter`): `meter_2` hours detach the apartment subtree from
     the root; `mixed` and `unknown` hours drop the root's hour; the apartment
     subtree is computed as usual in every hour. For every node: a missing
     child reading makes the parent's hour unknown (dropped), and a negative
     exclusive value is dropped; both are counted per node and returned for
     diagnostics. Without supply detection or `apartment_meter` every hour is
     `victron`, i.e. today's tree with the two data fixes.
   - All three callers pass the hour states: learning in
     `ConsumptionModule._maybe_learn` and the two real-consumption series in
     `coordinator.py`.
   - Forecast: while the confirmed state is `meter_2`, the apartment subtree's
     device profiles and the climate module's forecast for climate sensors
     inside that subtree are left out of the Victron demand; `unknown` keeps
     them and adds a plan reminder that the apartment's supply is unknown.
   - The topology fingerprint includes `apartment_meter` and whether supply
     detection is on, so the profiles relearn once under the new attribution.
4. Diagnostics, README section, parent spec status. A backtest script
   (`scripts/backtest_supply_detector.py`) over exported hourly statistics
   against the PPE 2 Tauron series, printing the confusion matrix.

## Answers (2026-10-17)

1. Unknown state: plan with the apartment included, flagged.
2. History older than the recorder's state history: the same rule on hourly
   means, skipping undecided hours and hours next to a detected change.
   Clarified 2026-10-17 before slice 3: "skipping" means an undecided hour is
   no evidence, not that its state is unknown — the switch state persists
   through it, exactly as the live log holds its state through undecided
   samples. Dropping undecided hours from learning would discard ~60 % of
   them (backtest) and leave most days below the 20-of-24-hours rule.
3. Build now.

## Questions as asked

1. **State unknown** (fresh install, before the first confirmation): plan the
   Victron load with the apartment included, as today, and say so on the
   entity — or leave the apartment out until the state is known? Recommended:
   include it, flagged; it is the switch's normal position (PPE 2 drew
   143 kWh in 2026, nothing since August) and leaving it out would plan the
   battery for a load it does carry.
2. **History older than the recorder's state history** (needed to relearn
   21 days of profiles): (a) classify those hours with the same rule on hourly
   means, skipping undecided hours and hours next to a detected change
   (backtest above: no false positives); (b) use the Tauron PPE 2 hourly import
   as the state of past hours (a second data source, a day late); (c) learn
   only from the stored log and recorder history and accept thinner profiles
   for about three weeks. Recommended: (a).
3. **Is it worth building now?** Stage 2 alone changes the forecast only while
   the apartment is on PPE 2, which has not happened since August. Its value
   is as the base for stage 3, whose upper bound measured on Jul–Oct 2026 data
   is ~26–28 zł/month.
