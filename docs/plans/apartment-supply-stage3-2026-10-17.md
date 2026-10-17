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
   entity, 5 zł); at least 2 h on a source (number entity); no switch in the
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
