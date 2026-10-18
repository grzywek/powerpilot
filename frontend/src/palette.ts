/** Colours shared by every surface that draws the same things (the energy
 *  chart, its tooltip, the "Przepływ" diagram) — one copy, never redrawn. */

/** Sub-metered devices, by their index in the series' / topology's device list.
 *  Deliberately avoids the fixed stack colours below and the base-consumption
 *  maroon (#b5475d), so every device reads as its own hue next to them. */
export const DEVICE_PALETTE = [
  "#7b6cf6", // indigo
  "#e91e63", // pink
  "#f97316", // orange
  "#06b6d4", // cyan
  "#facc15", // yellow
  "#e74c3c", // red
  "#14b8a6", // teal
  "#84cc16", // lime
];

/** Grid import per meter: PPE 1 behind the Victron, PPE 2 the apartment on
 *  meter 2 (a lighter shade of the same purple). */
export const PPE1_COLOR = "#8e44ad";
export const PPE2_COLOR = "#c39bd3";

/** Battery flows, as the energy chart's stacks. */
export const ESS_DISCHARGE_COLOR = "#b0a14f";
export const ESS_CHARGE_COLOR = "#c98a3a";
export const EV_COLOR = "#15803d";
