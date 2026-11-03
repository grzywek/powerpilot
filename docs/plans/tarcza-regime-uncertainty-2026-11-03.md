# PPE 1's cap regime under uncertainty and the tail's EV charging

Status: done 2026-11-03 (slices 1–4; see "Results").

Decision record: `docs/adr/003-cap-regime-under-uncertainty.md`. Background:
`docs/apartment-supply-planning.plan.md` (both meters' caps, "Where the money
is" in `docs/plans/apartment-supply-stage3-2026-10-17.md`).

## Goal

PPE 1 keeps trying to end the month under the cap while that is still
plausible, and gives up (prices its kWh at the limit) only when the recent
real price weeks say it cannot make it. The month forecast stops assuming the
car charges in the month's cheapest hours.

What the user will notice: in a month near the limit (October 2026: 0.562 vs
0.61) the plan keeps the apartment on meter 2 in expensive hours and charges
the battery at the cheapest market hours; in a month that is clearly lost
(November 2026 so far) it behaves as today. The Tarcza card says in how many
of the last four weeks the cap would bind, and its month forecast for PPE 1
gets close to the meter (October: 0.49 shown on 17.10, 0.562 billed).

## Evidence (backtest, 2026-11-02)

`scripts/backtest_month_tail.py` (commit 149c137) on the `powerpilot/series`
export August 1 – November 2 (2 244 h, kept out of git). At each as-of noon
the real optimizer plans PPE 1's month: the horizon on real prices to the end
of the next day, the tail on `avg3` (the integration's estimate), `week-k`
(the k-th latest archived week replayed) or `real` (perfect foresight). The
Victron's load is the real one, so only prices and the car differ.

PPE 1 on the meter: August 993 kWh @ 0.260, September 818 @ 0.349, October
786 @ 0.562 (car 385 kWh @ 0.614, house and battery 401 @ 0.512).

Calibration — October planned on 01.10 with perfect prices, by where the car's
tail import goes (`--ev`):

| `--ev` | where the car charges in the tail | October |
|---|---|---|
| `month` | the month's cheapest hours (today's model) | 0.307 |
| `day` | its day's cheapest hours | 0.465 |
| `hour` | the hours it really charged | 0.522 |
| meter | | 0.562 |

`avg3` never mis-called a regime in August–October under any `--ev`; it was
slightly optimistic, not pessimistic. Single replays did: with `--ev hour`,
3 of the 32 October replays bound (01.10 week-3 0.850, 05.10 week-4 0.789,
21.10 week-1 0.647) while the month ended under the cap.

November as of 02.11 11:00 (prices through 05.11):

| `--ev` | avg3 | week-1 | week-2 | week-3 | week-4 |
|---|---|---|---|---|---|
| `month` | 1.280 B | 1.462 B | 0.290 | 1.015 B | 0.379 |
| `day` | 1.167 B | 1.284 B | 0.540 | 0.916 B | 0.503 |
| `hour` | 1.271 B | 1.378 B | 1.079 B | 1.099 B | 0.883 B |

(B = the cap binds.) With the car calibrated, every recent week binds:
returning the apartment to the Victron on 02.11 was right; the plan reached it
on one path taken as certain.

## Decisions (binding)

1. **Tail EV = the learned charging profile.** Each tail hour carries a fixed
   EV import: the mean grid-side EV import of its weekday and hour over the
   last 28 complete days. Source: the hourly change of the EV energy sensor
   the series' realized EV reads (`CONF_EV_ENERGY_ADDED_SENSOR`, recorder
   statistics), converted to grid side by the EV charge efficiency as
   `ev_grid_kwh` is today. Negative hourly deltas are kept (AGENTS.md,
   recorder energy). The LP's free tail EV placement (`tail_ev_col`, its sum
   row, `MonthTail.ev_grid_kwh`) is removed; `MonthTail` carries the per-hour
   profile instead, which enters PPE 1's import and settlement as a constant.
   Fewer than 28 complete days of EV statistics → the cap is unavailable with
   a reason naming the sensor and the days found (today's missing-drain-profile
   path). `predicted_drain_between` goes if nothing else uses it. The horizon's
   EV planning is unchanged.
2. **Scenarios.** `S = 4` (const). Scenario k prices every hour that has no
   source price — the horizon's estimated hours and the whole tail — with the
   k-th latest archived **certain** price of the same weekday and hour within
   `_ESTIMATE_MAX_WEEKS_BACK` weeks; forecast entries never feed a scenario.
   Both the buy and the cap price come from that entry (the cap price through
   `cap_price_of` under the hour's edition). Scenario k is thus the k-th latest
   real week repeated. A scenario missing any hour is dropped, counted and
   named in the outcome; `p` is taken over the usable ones; none usable → the
   cap is unavailable with that reason.
3. **Verdict per scenario.** The scenario is solved with the plan's inputs and
   model (switching modelled exactly when the plan models it), as an LP
   relaxation over the regime combinations — the bounding step the solver
   already runs — and the cheapest combination gives each PPE 1 period's
   regime. The relaxation is optimistic (fractional discharge and supply); it
   errs toward "under", the cheaper error (ADR 003).
4. **Probability.** `p_P` = share of usable scenarios in which PPE 1's period
   `P` binds (0, ¼, ½, ¾, 1 with four).
5. **Blended settlement.** The plan prices each PPE 1 period as
   `p·L·E + (1−p)·C` with the existing `C`/`E` columns at fixed costs
   `(1−p)` and `p·L`; PPE 1 periods leave the regime enumeration. PPE 2's
   periods keep the exact enumeration. The tail keeps the `avg3` estimate (its
   central path). `p = 1` must reproduce today's binding plan and `p = 0`
   today's non-binding plan exactly.
6. **Once per refresh.** The coordinator computes `p` (and the scenario
   outcomes) once per refresh and passes them to every solve of that refresh;
   `settle_cycles` re-solves reuse them.
7. **Reporting.** PPE 1's `BillingOutcome` keeps the plan's own path (energy,
   cost, average, `cap_binds`, settled `min(C, L·E)`) and gains
   `bind_probability` and `scenarios` (first date of the replayed week,
   average, binds; dropped ones with the reason). The status payload, the
   `tarcza_ppe1_cap_binds` binary sensor's attributes and the panel's Tarcza
   card carry them: "limit działa w 4 z 4 ostatnich tygodni", the four
   averages on one muted line.
8. **Budget.** Scenario evaluation adds at most 3 s to an hour-boundary
   refresh in Home Assistant (2.3 s measured 2026-10-17 without a cycle
   re-solve). Measure
   with `scripts/bench_supply_model.py` (add a scenario case) and the HA log.
   Over budget: change only the tail's coefficients between scenarios
   (`changeCoeff`/`changeColsCost`) instead of rebuilding — never fewer
   scenarios.
9. **Unchanged:** switching rules and thresholds, recommendations (except
   through `p`), PPE 2's settlement, the `avg3` estimator for the plan's own
   estimated hours, the horizon's EV allocator.

## Slices

1. **fix(tarcza): tail EV from the charging profile** (decision 1).
   EV module learns the 7×24 charging profile; `_billing_inputs` builds the
   per-hour tail EV; optimizer drops the free tail EV columns. Backtest: add
   `--ev profile` (the 28 days before each as-of, same rule) and report
   August–October at the 1st of each month with `real` prices against the
   meter. Gate: October within ±0.06 of 0.562; otherwise stop and report.
2. **feat(tarcza): replayed-week scenarios and PPE 1's bind probability**
   (decisions 2–4, 6). `PriceArchive.replay(hour, k, value_of)`; scenario
   slots built next to the tail; an optimizer entry point returning the
   relaxed regime per period; `CapOutlook` per PPE 1 period. Backtest: add the
   `p` column per as-of.
3. **feat(optimizer): price PPE 1's month by its bind probability**
   (decision 5) and pass `p` through `settle_cycles`. Bench (decision 8) —
   gate: report the measured times before slice 4.
4. **feat(panel): show the cap's odds; docs** (decision 7). Status, binary
   sensor attributes, panel card, README Tarcza section,
   `docs/ARCHITECTURE.md`, one AGENTS.md invariant line (below), this plan's
   status and ADR 003's.

AGENTS.md line for slice 4:

> - **Tarcza: never decide PPE 1's month regime on one tail price path, and
>   never let the LP place the tail's EV charging.** The regime is the share of
>   four replayed real weeks in which the month binds, and the tail's EV
>   follows the car's learned charging hours. On one averaged path with the
>   car charging in the month's cheapest hours the plan settled October at
>   0.31 zł/kWh against 0.56 on the meter (backtest 2026-11-02).

## Tests (each fails when its rule breaks)

- Tail EV: a tail with one very cheap hour keeps the profile's EV import in
  its own hours (it does not move there); the settlement includes it; fewer
  than 28 days → cap unavailable with the sensor and day count in the reason.
  The docstring names the incident: 2026-11-02, October's tail EV priced at
  0.31 against 0.56 on the meter.
- Replay: k-th latest certain same weekday+hour; a forecast entry in between is
  skipped; a missing sample drops the scenario and names it; no usable
  scenario → unavailable.
- Probability: four scenarios with two binding give `p = 0.5`; a refresh with
  two re-solves evaluates scenarios once.
- Blend: `p = 1` plan equals today's plan with PPE 1 binding, `p = 0` equals
  today's non-binding plan (same decisions and objective within the MIP gap);
  at `p = 0.5` an hour priced 2.00 costs PPE 1 1.305 + distribution, so with
  switching modelled the apartment moves to meter 2 there; of two hours in one
  distribution zone priced 1.12 and 2.23 the battery charges in the cheaper
  (at `p = 1` it may pick either).
- Panel: the card's text for 0, 2 and 4 binding weeks, as a pure helper with a
  `frontend/test/*.test.ts` case (run by `tests/test_frontend_units.py`).

## Results (2026-11-03)

Slices: 1 `5a8fc2e`, 2 `84c9b3f`, 3 `7bd89c4`, 4 the panel/docs commit after
them.

**Gate 1** — `scripts/backtest_month_tail.py` on a fresh export (2026-07-04 –
2026-11-03 15:00), as of the 1st at 12:00 with `real` prices, EV on the grid
side (÷ 0.91, the installation's charge efficiency):

| month | meter | `profile` | `hour` | `month` | `p` (profile) |
|---|---|---|---|---|---|
| August | 0.260 | 0.209 | 0.233 | 0.152 | 0/4 |
| September | 0.349 | 0.416 | 0.330 | 0.268 | 0/4 |
| October | 0.562 | **0.523** | 0.527 | 0.307 | 1/4 |
| November (03.11 16:00) | 1.207 so far | | | | 4/4 |

October −0.039, inside ±0.06. `month` (the tail's cheapest hours, now placed
by the script instead of the LP) reproduces the 0.307 above. September's
profile is pessimistic (+0.067): August's charging hours priced at
September's prices.

**Gate 3** — `scripts/bench_supply_model.py`, old (`e5ebaf9`) and new code
alternated on a loaded Mac: the four scenarios add 0.18–0.27 s without
switching and 0.46–0.65 s with it (0.90 s under heavy load), within the 3 s;
no coefficient-only fallback needed. The plan's own MIP time moves with the
tail's EV shape, not with the model: the switching winter case with no tail
EV solves alike on old and new code (10.7 / 10.6 s), in 0.7 s with a flat EV,
8.7 s with the bench's night profile and 2.6 s with the old free placement.
Not measured in the Home Assistant log yet (needs a release): watch the
optimizer's "Tarcza PPE 1: limit działa w …" line, which carries the
scenarios' wall time.

Taken during implementation: the 28 complete days are looked for within the
last 56 (one missing hour does not take the cap down for four weeks); the
tail's learned EV does not narrow the battery's charge cap (an hourly mean,
not a session on the shared phase); "no usable week → cap unavailable" acts
from slice 3, when the plan first prices by `p`; the statistics reader keys
local hours, so the autumn change's repeated hour counts once (the same root
cause as the `powerpilot/series` follow-up below).

## Out of scope (separate follow-ups)

- `powerpilot/series` skips the repeated hour of the autumn clock change
  (25.10.2026 02:00 +01:00).
- The hour traces' "próg rozładowania" texts use market prices also when the
  cap binds.
- `ha.ds2.pl` login loop: Home Assistant sees Cloudflare edge addresses
  (`http:` `use_x_forwarded_for` / `trusted_proxies`), user's HA config.

## Execution prompt (for a separate session)

> Implement `docs/plans/tarcza-regime-uncertainty-2026-11-03.md` in PowerPilot
> (read it, `docs/adr/003-cap-regime-under-uncertainty.md`, `AGENTS.md`,
> `README.md` and `docs/plans/apartment-supply-stage3-2026-10-17.md` first;
> the decisions are settled, do not re-ask them). Work slice by slice; each
> slice builds, its tests pass (`.venv/bin/python -m pytest`), `ruff check` is
> clean on touched files, and it is committed on its own with a Conventional
> Commit ending with the `Co-Authored-By: AI` trailer. In slice 4 rebuild the
> panel bundle (`cd frontend && npm run build`) and commit
> `custom_components/powerpilot/frontend/powerpilot-panel.js` with the source. Stop at the
> gates of slices 1 and 3 and report the numbers. The backtest export lives
> outside git; ask the user for a fresh one if you need it (the script's
> docstring says how it is made). Do not push or touch Home Assistant without
> asking.
