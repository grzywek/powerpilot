/**
 * The running hour's plan column, read from ONE plan.
 *
 * The card judges the running hour against the plan it STARTED with (its
 * hour-start vintage); a mid-hour re-plan shows up as a revision note instead.
 * Every plan value in the column comes from that one plan, or, when the hour
 * has no vintage, every value comes from the live plan. Mixing the two once
 * put the hour-start plan's charger verdict ("niepotrzebna") beside the live
 * plan's EV SoC (5 → 17 %) in the same column (2026-11-06).
 */

/** The current hour's full-hour baseline plan (its hour-start vintage). */
export interface HourPlan {
  charge: number | null;
  discharge: number | null;
  ev: number | null;
  ev_minutes: number | null;
  grid: number | null;
  grid_ppe2: number | null;
  consumption: number | null;
  soc_end: number | null;
  ev_soc: number | null;
  charge_power_kw: number | null;
}

/** The plan values the running hour's column shows. */
export interface RunningHourPlan {
  charge: number | null;
  discharge: number | null;
  ev: number | null;
  evMinutes: number | null;
  grid: number | null;
  gridPpe2: number | null;
  consumption: number | null;
  socEnd: number | null;
  evSocEnd: number | null;
}

/** The hour-start vintage's values, or all of the live plan's without one. */
export function runningHourPlan(
  hourStart: HourPlan | null,
  live: RunningHourPlan,
): RunningHourPlan {
  if (hourStart == null) return live;
  return {
    charge: hourStart.charge,
    discharge: hourStart.discharge,
    ev: hourStart.ev,
    evMinutes: hourStart.ev_minutes,
    grid: hourStart.grid,
    gridPpe2: hourStart.grid_ppe2,
    consumption: hourStart.consumption,
    socEnd: hourStart.soc_end,
    evSocEnd: hourStart.ev_soc,
  };
}

/** Whether a plan's EV energy for an hour needs the charger plugged in. */
export function chargerNeed(evKwh: number | null | undefined): string | null {
  if (evKwh == null) return null;
  return evKwh > 0.005 ? "podłączona" : "niepotrzebna";
}
