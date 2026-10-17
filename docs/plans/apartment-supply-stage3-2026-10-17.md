# Stage 3: recommending apartment switches (PPE 1 ↔ PPE 2)

Status: draft, rewritten 2026-10-17 after the user's correction of the value
mechanism. Decisions below are taken; one question for the user at the end.
Large slice across modules: executed in a separate session from the prompt at
the end.

Parent spec: `docs/apartment-supply-planning.plan.md` (stage 3 of 4; its
"Ustalenia z 2026-09-14" are settled and not re-asked). Stage 1 (price cap on
PPE 1) shipped in v2.4.x, stage 2 (supply detection, switch-aware load model)
in v2.5.2.

## Goal

Recommend, at any time of day, when to switch the apartment to the second
meter (PPE 2), for how long, and what the whole cycle saves — sensible for a
person to carry out by hand (no hour-on, hour-off toggling). The integration
does not drive the switch now; its recommendation entities are the interface a
future actuator automation will read.

## Where the money is

Each meter has its own monthly cap: its energy bill is `min(C, L·E)`.

- **The strong mechanism — keep PPE 1 under the limit.** When PPE 1's month
  would end above the limit, every PPE 1 kWh costs the limit, even the cheap
  ones the battery and the car bought. Moving the apartment to PPE 2 in the
  expensive hours the battery cannot cover gives PPE 2 a high average (so it is
  capped and pays `L` per kWh) and lets PPE 1's average fall below `L`, so PPE 1
  pays its real, low average. Saving ≈ `E₁' · (L − avg₁')` for the energy left
  on PPE 1 — e.g. 500 kWh at 0.45 against 0.61 is ~80 zł a month.
- **The weak mechanism — cap uncovered expensive kWh.** With PPE 1 under the
  limit anyway, only the apartment's consumption the battery did not cover in
  hours above the limit gains (price − `L`): measured 0.92 / 7.99 / 6.14 /
  8.30 zł for Jul / Aug / Sep / Oct 1–16 2026.
- **No gain** when even PPE 1's cheapest hours are above the limit (the
  weekend of 17–18.10: cheapest hour 0.60 zł, average 0.90): both meters are
  capped and every kWh costs `L` on either.

The strong mechanism needs months whose PPE 1 average lands above the limit
because of hours the battery cannot cover while cheap hours stay well below it
— most likely winter (December 2025: apartment 638 kWh, PPE 1 1172 kWh, against
~240 / ~720 a month now; battery 30 kWh). No month since June was in that band
(PPE 1 averages 0.21–0.42), and winter prices are not in the archive, so the
value cannot be measured yet. The stage-1 entity
`sensor.…_tarcza_ppe_1_prognoza_sredniej_na_koniec_miesiaca` shows when a month
approaches it.

## Decisions (taken here, with reasons)

1. **One joint model, not rules.** The value comes from the interplay of both
   caps, the battery and the car, so the switch schedule is part of the HiGHS
   model: a binary `s[t]` per horizon hour (apartment on PPE 2), the apartment's
   demand moving off the Victron while `s[t] = 1` (the discharge cap becomes a
   product of binaries, linearised), and PPE 2's monthly settlement with its own
   regime binary next to PPE 1's. The battery's behaviour follows: with PPE 1
   kept under the limit its energy arbitrage pays again, which the joint
   objective sees by itself. Distribution is the same tariff on both meters and
   cancels out.
2. **Cycles a person can do.** Opening a cycle costs the threshold (number
   entity, default 2 zł — lowered from the parent spec's working value of 5 zł
   by the user on 2026-10-17, see "Backtest" below); at least 2 h on a source
   (number entity); no switch in the
   quiet window (00:00–09:00, configurable) or while the user is unavailable
   (calendar trips, calendar read error); switches on hour boundaries. No daily
   cycle limit (settled). A cycle may last days if that pays.
3. **The month's tail keeps the state of the last horizon hour** — no switches
   are planned past the horizon, but the tail is settled under that state on
   both meters, so a long PPE 2 stay is valued for the whole month.
4. **Every recommended cycle is worth its threshold on its own:** the plan is
   re-solved without each proposed cycle (an ongoing one closes at its planned
   return); a cycle whose own saving is below the threshold is dropped and the
   plan re-solved. Ties favour fewer switches.
5. **Observed PPE 2:** month-to-date PPE 2 import = the apartment meter's energy
   in `meter_2` intervals from the stage-2 log; hours containing a switch are
   split by the 5-minute statistics at the transition's `at`. A started cycle is
   recorded, so its return needs no threshold again (parent spec).
6. **Both meters use the configured Tarcza editions** (same terms per meter,
   confirmed by the user). Without an edition for a month, that month has no
   cap on either meter.
7. **Solve time gates everything.** Slice 2 measures the model on a recorded
   week; if it is too slow, switch decisions are restricted to the next 72 h
   and the rest of the horizon keeps the state of hour 72.
8. **Automation later, no change of meaning.** `switch_required` and the
   target source are the stable interface; an automation driving a contactor
   can read them. Not built now.

## Slices

1. **PPE 2 observed billing:** apartment energy in `meter_2` intervals, mixed
   hours split at the transition, PPE 2 periods in the billing module, PPE 2
   price-cap diagnostics entities. Tests with captured statistics.
2. **Model:** `s[t]`, open/close indicators, threshold cost, minimum stay,
   availability and quiet window, product linearisation, PPE 2 settlement,
   tail under the last state; solve-time measurement on a recorded week
   (gate of decision 7). Tests: the three regimes above as cases (strong, weak,
   none), threshold, minimum stay, quiet window, unavailable user.
3. **Recommendations:** per-cycle marginal saving by re-solve, started-cycle
   record, entities from the parent spec (recommended source, plan, next
   switch, switch required/available, cycle savings, min-cycle-savings
   number), a Home Assistant notification on `switch_required` keyed by cycle
   id.
4. **Panel and docs:** supply timeline next to the Victron's work, the
   with/without-cycles comparison and the reason when nothing is recommended;
   README; parent spec status.

## Slice 2 model design (binding)

Inputs per horizon hour `t`: the Victron-side demand without the apartment
`F[t]`, the apartment's forecast `A[t]` (the apartment subtree's device
profiles plus the climate module's forecast for climate sensors inside it),
EV energy, prices, cap prices; both meters' billing periods (PPE 2 from
slice 1); the actual supply and the last transition; availability.

- **Forecast split.** While supply detection is active, consumption and climate
  put the apartment subtree into a new `HourSlot.apartment_kwh` instead of the
  slot's demand, in every state; the optimizer adds it back per its supply.
  This replaces stage 2's exclusion of the subtree while on the second meter
  (`victron_detached_devices`) with one path: with switching not modelled the
  supply is fixed to the actual state (unknown → Victron, as stage 2 decided),
  so plans stay what they are today.
- **Variables.** `s[t] ∈ {0,1}` (apartment on PPE 2) for switch hours
  `t < T_sw`; beyond `T_sw` (and in the month tail) `s` equals `s[T_sw − 1]`.
  `s[0]` (the running hour) is fixed to the actual state; while the actual
  state is unknown no `s` exists (no recommendation). Open indicator
  `o[t] ≥ s[t] − s[t−1]`, `o ≥ 0`, cost = cycle threshold (zł) — exact under
  minimisation. The battery's binary discharge switch `z[t]` gets the product
  `w[t] = z[t]·s[t]` (McCormick: `w ≤ z`, `w ≤ s`, `w ≥ z + s − 1`, `w ≥ 0`;
  exact because `s` is binary; also valid for the tail's continuous `z`).
- **Flows.** Discharge delivered = `D0[t]·z[t] − (D0[t] − D1[t])·w[t]` with
  `D0 = min(F + A, inverter·frac)`, `D1 = min(F, inverter·frac)`. PPE 1 import
  `g1 = F + A − A·s + EV + Σc − discharge`; PPE 2 import `g2 = A·s` (an
  expression, no column).
- **Costs.** Distribution, excise and the hourly price are the same on both
  meters, so `s` cancels out of every per-hour term; it acts only through the
  discharge and the two monthly settlements: `C1/E1` sum `settled·g1`/`g1` as
  today, `C2 = C2_mtd + Σ settled·A·s`, `E2 = E2_mtd + Σ A·s`, each month
  `K2 = min(C2, L·E2)` with its own regime binary and big-M, next to PPE 1's.
  A month without a Tarcza edition has `settled = 0` on both meters.
- **Rules.** Switching at boundary `t` (between `t−1` and `t`) only when slot
  `t`'s local start hour is outside the quiet window (config, default
  00:00–09:00) and the user is available at that time (calendar trips, with
  their travel margins, and a calendar read error make the user unavailable;
  no events = present); elsewhere `s[t] = s[t−1]`. Minimum stay `H` hours on
  either source (config, default 2): `s[t] − s[t−1] ≤ s[τ]` and
  `s[t−1] − s[t] ≤ 1 − s[τ]` for `τ ∈ [t, t+H)`; the actual last transition
  counts, so a switch back is impossible before `transition.at + H`.
- **Switch horizon.** `T_sw` is an internal constant; slice 2 measures solve
  time for `T_sw` = full horizon and 72 h on a recorded week plus month tail
  and sets it. Measured 2026-10-17 with `scripts/bench_supply_model.py` (a
  synthetic week shaped like the live data, 168 h + 500 h tail, clock frozen,
  medians of 3 runs, seconds). "Before" is slice 2 (one MIP, big-M regime
  binaries); "after" is slice 2b (cap regimes enumerated, LP-bound pruning:
  2 combinations without switching, 4 with it, 1 MIP solved in every case).
  Both settle every case identically.

  | Case | Base before | Base after | Winter before | Winter after |
  | --- | --- | --- | --- | --- |
  | no switching, no tail | 3.98 | 0.67 | 3.74 | 0.20 |
  | no switching (today) | 22.83 | 2.41 | 15.02 | 0.46 |
  | switching, `T_sw` = 168 h | 24.60 | 9.71 | 14.84 | 1.70 |
  | switching, `T_sw` = 72 h | 33.15 | 2.38 | 12.63 | 3.23 |

  (`--winter`: apartment ×2, evening prices ×1.5, PPE 1's month at 0.60 zł;
  there 168 h plans a 6 h cycle that 72 h does not yet see.) On the base
  case 168 h is clearly slower than 72 h, so `T_sw` = 72 h; the plan is
  re-solved every hour, so a later cycle is planned once it enters the
  window. (Slice 2, before the enumeration and with an unfrozen clock, had
  chosen 168 h: 19.2 / 37.7 / 45.0 s.)
- **Outputs.** Each decision carries its planned supply; the plan carries both
  meters' `BillingOutcome`s. Recommendations (marginal value per cycle,
  entities, notifications) are slice 3.

## Backtest on October 1–17, 2026 (live data)

Hourly apartment energy, the Victron's grid-to-loads energy split between the
apartment and the garage by their consumption, cap prices from the archive;
battery-covered kWh cost ~0.43 zł (average charging price 0.37 zł / round-trip
0.89 + wear); PPE 2 capped at 0.61 zł per kWh. Optimal cycles with perfect
foresight, switching on hour boundaries outside 00:00–09:00, at least 2 h,
user always present:

| Cycle threshold | Recommended cycles | Saving |
| --- | --- | --- |
| 5 zł | none | 0 zł |
| 2 zł | 10.10 16:00 → 12.10 22:00 (54 h, 18 kWh) | 4.85 zł |
| 0.5 zł | 5 short cycles within 10–12.10 | 5.30 zł |
| no rules (hourly, ideal) | — | 7.50 zł |

PPE 1 stayed under the limit all month (average 0.43), so only the weak
mechanism was available; the strong one (PPE 1 pushed under the limit) has
no data yet.

## Question for the user

Build now (the prompt below, in a separate session), or when the PPE 1 forecast
average first approaches the limit?

## Execution prompt (for a separate session)

> Implement stage 3 of PowerPilot as specified in
> `docs/plans/apartment-supply-stage3-2026-10-17.md` (read it, the parent spec
> `docs/apartment-supply-planning.plan.md`, `docs/plans/apartment-supply-stage2-2026-10-17.md`,
> `AGENTS.md` and `README.md` first). Work slice by slice, in a worktree per
> slice; each slice builds, its tests pass (`.venv/bin/python -m pytest`),
> `ruff check` is clean on touched files, and it is committed on its own with a
> Conventional Commit. Stop after slice 2 and report the measured solve time
> before going on. Do not push or touch Home Assistant without asking.
