# ADR 003: PPE 1's price-cap regime under uncertain tail prices

Status: accepted, 2026-11-03; implemented 2026-11-03 (`docs/plans/tarcza-regime-uncertainty-2026-11-03.md`, done, with the backtest and bench results)

## Context

Each meter's month is settled as `min(C, L·E)`. PPE 2 takes only the
apartment's expensive hours, so its cap binds from the first kWh; PPE 1 can
end the month under the cap only if it buys cheaply all month, and every
expensive kWh it takes raises the bar for the rest of the month.

The optimizer enumerates the cap regimes exactly, but over one tail price path
(the same weekday+hour averaged over three archived weeks) taken as certain,
and with the tail's EV import placed by the LP in the month's cheapest hours.
On 2026-11-02 it gave PPE 1's November up (forecast 1.12 vs 0.61) and sent the
apartment back to the Victron. The backtest (`scripts/backtest_month_tail.py`,
August–November 2026) found:

- The averaged tail price never mis-called a regime in August–October and was
  not biased toward "binds".
- The free EV placement is the large error: with perfect prices the model
  settled October at 0.307 zł/kWh, at 0.522 with the car at the hours it
  really charged; the meter says 0.562. The car is half of PPE 1's energy.
- Single replayed weeks disagree with each other near the limit (October:
  three of 32 replays bound, the month ended at 0.562), so one path is not
  enough to decide a regime that is worth tens of złoty a week.

The two errors are not symmetric. Planning PPE 1 as "binds" when it could
have stayed under costs the market price minus the limit on every kWh moved
onto it (~40 zł in the week of 2.11); planning it as "under" when it binds
costs only the distribution-zone arbitrage the battery would have done
(~4.5 zł that week).

## Decision

- The tail's EV import is a fixed load per hour from the car's learned
  charging profile (7×24 mean of the last 28 days' grid-side EV import). The
  LP no longer places it.
- PPE 1's regime is a probability `p` per month: the share of four scenarios
  — each the k-th latest real (certain) week of prices replayed over every
  hour without a source price, k = 1…4 — in which the month binds. A
  scenario is judged by the plan's own model solved as an LP relaxation over
  the regime combinations.
- The plan prices PPE 1's month as `p·L·E + (1−p)·C`: the expected marginal
  cost of a kWh, linear, so PPE 1's periods leave the enumeration. PPE 2's
  months keep the exact enumeration.
- `p` is computed once per refresh and reused by every re-solve of it.

## Rejected

- Fixing the price estimate's averaging: the backtest showed it was not the
  cause.
- A two-stage stochastic model with a regime per scenario and period: `2^(P·S)`
  combinations (256 for two meters and four scenarios), far over the
  hour-boundary budget.
- "Fight unless hopeless" (`p` = 0 unless every scenario binds): ignores how
  often the cap binds and fights months that are lost.
- Fixing only the EV placement: the model would turn pessimistic and give up
  borderline months like October, the expensive error.
- EV charging free within each day's hours (0.465 vs 0.562 for October) or
  within learned plug availability: more LP structure and history to read,
  calibrated worse than the charging profile.
