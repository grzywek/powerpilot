// The running hour's plan column reads one plan. On 2026-11-06 at 13:14 the
// card said the charger was "niepotrzebna" (hour-start plan: EV 0.002 kWh)
// beside an EV SoC of 5 → 17 % (live plan: EV 8.25 kWh in 50 min).
import { test } from "node:test";
import assert from "node:assert/strict";
import { chargerNeed, runningHourPlan, type HourPlan, type RunningHourPlan } from "../src/hour-plan.ts";

const live: RunningHourPlan = {
  charge: 0,
  discharge: 0,
  ev: 8.249,
  evMinutes: 50,
  grid: 9.616,
  gridPpe2: 0,
  consumption: 0.7,
  socEnd: 39.5,
  evSocEnd: 17.1,
};

const hourStart: HourPlan = {
  charge: 0,
  discharge: 0,
  ev: 0.002,
  ev_minutes: 1,
  grid: 0.002,
  grid_ppe2: 0.1,
  consumption: 0.701,
  soc_end: 39.5,
  ev_soc: 5,
  charge_power_kw: 0,
};

test("the hour-start plan supplies the charger verdict and the EV SoC together", () => {
  const plan = runningHourPlan(hourStart, live);
  assert.equal(chargerNeed(plan.ev), "niepotrzebna");
  assert.equal(plan.evSocEnd, 5);
  assert.equal(plan.gridPpe2, 0.1);
  assert.equal(plan.evMinutes, 1);
});

test("without an hour-start plan every value is the live plan's", () => {
  const plan = runningHourPlan(null, live);
  assert.equal(chargerNeed(plan.ev), "podłączona");
  assert.equal(plan.evSocEnd, 17.1);
});

test("a vintage without an EV SoC shows none rather than the live plan's", () => {
  const plan = runningHourPlan({ ...hourStart, ev_soc: null }, live);
  assert.equal(plan.evSocEnd, null);
});

test("no EV energy known means no verdict", () => {
  assert.equal(chargerNeed(null), null);
  assert.equal(chargerNeed(undefined), null);
});
