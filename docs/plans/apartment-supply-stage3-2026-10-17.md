# Stage 3: recommending apartment switches (PPE 1 ↔ PPE 2)

Status: draft, 2026-10-17. Blocked on the go/no-go question at the end; the
measured value of the feature is low.

Parent spec: `docs/apartment-supply-planning.plan.md` (stage 3 of 4: "zmienne
źródła, cykle, próg, kalendarz, encje, panel, eksport z przyczynami"; its
"Ustalenia z 2026-09-14" are settled). Stage 1 (price cap on PPE 1) shipped in
v2.4.x, stage 2 (supply detection, switch-aware load model) in v2.5.2.

## Goal

Recommend manual switches of the apartment to the second meter (PPE 2) when a
whole cycle — over, a stay of at least 2 h, back — saves more than the cycle
threshold, given both meters' monthly price caps, the battery and the user's
availability. The integration never drives the switch.

## Where the money is (measured 2026-10-17 on the live installation)

A switch pays only through PPE 2's own monthly cap: if PPE 2 draws only in
expensive hours, its average exceeds the limit and each of those kWh costs the
limit instead of the hourly price. On the Victron the battery already covers
most expensive hours with energy bought at ~0.2–0.3 zł, so moving a kWh the
battery would have covered costs money. What can be saved is the apartment's
consumption in hours above the limit that the battery did not cover:

| Month 2026 | Hours with cap price > 0.61 | Apartment kWh not covered by the battery | Saving if all of it had been on PPE 2 | Upper bound incl. battery reallocation (all PPE 1 import in those hours) |
| --- | --- | --- | --- | --- |
| July | 531 | 8.8 | 0.92 zł | 2.39 zł |
| August | 576 | 35.3 | 7.99 zł | 27.56 zł |
| September | 584 | 21.9 | 6.14 zł | 26.88 zł |
| October 1–16 | 322 | 35.3 | 8.30 zł | 15.30 zł |

Sources: PPE 1 hourly import from Tauron (`tauron_importer:3685152_…`), the
apartment meter's hourly energy, cap prices from the price archive. These are
ceilings assuming perfect, free switching; the settled threshold (5 zł per
cycle, at least 2 h on the source, no switching 00:00–09:00, the user present)
leaves a few cycles a month at most. Under the 2027 edition (0.50 net, handling
fee possibly excluded) fewer hours exceed the limit. Winter months are
unmeasured (the price archive starts in June) and could be higher.

## Variant A — the parent spec in full

- **Model.** Per horizon hour a binary `s[t]` (apartment on PPE 2) with open
  and close indicators; the threshold as a fixed cost per opened cycle; a 2 h
  minimum stay; opening and closing forbidden in the quiet window and in hours
  the user is unavailable (trips from the calendar module, a calendar read
  error); every cycle opened in the horizon closes inside it, so the month's
  tail stays on the Victron. The Victron demand loses the apartment while
  `s[t] = 1`, so the battery's discharge cap becomes a product of binaries
  (`z[t]·(1−s[t])`) and needs the usual linearisation. PPE 2 gets its own
  monthly settlement `min(C₂, L·E₂)` per month with a regime binary, on top of
  stage 1's PPE 1 settlement; distribution is the same tariff on both meters
  and cancels out.
- **Observed PPE 2.** Month-to-date PPE 2 import = apartment energy in
  `meter_2` intervals; hours containing a switch are split by the recorder's
  state history (stage 2 left this for here).
- **Marginal value.** The reference plan without new cycles (an ongoing cycle
  closes at its planned return) and one re-solve per proposed cycle without
  it; a cycle is recommended only if its own saving clears the threshold.
- **Surfaces.** The parent spec's entity table (recommended source, plan,
  next switch, switch required/available, cycle savings, min-cycle-savings
  number), PPE 2 price-cap diagnostics, the panel timeline and the export with
  reasons; notifications keyed by cycle id so a refresh does not repeat them.
- **Risks.** Several hundred extra binaries over 168 h on a solve that already
  takes 2–3 s with the month tail (Mac; the HA host is unmeasured), plus one
  re-solve per cycle. A first slice must measure the solve time and, if
  needed, restrict switching to the first 48 h.
- **Size.** Optimizer, billing (PPE 2), supply (splits), calendar
  availability, entities, panel, websocket and notifications: a large slice
  across modules — a prompt for a separate session, in four or five slices.

## Variant B — advisory rule on top of today's plan

No change to the optimizer. After each plan: find windows of at least 2 h,
outside the quiet window and while the user is available, in which the plan
imports for the Victron at a cap price above the limit; their saving is the
apartment's forecast kWh in those hours × (price − limit). If a window clears
the threshold, recommend switching at its start and back at its end. Requires
PPE 2's month-to-date average to stay above the limit (checked from the
observed PPE 2 import; otherwise no recommendation, with the reason). It
ignores the battery reallocation effect, so it underestimates the saving.
Entities: recommended source, next switch, switch required, cycle savings.
Size: medium, one module plus entities.

## Recommendation

Do not build either now. The realistic value is single zł to ~25 zł a month for
manual work at inconvenient hours, under terms whose 2027 basis is still
unclear. Re-check in January with winter prices in the archive and the first
2027 invoice; if expensive uncovered hours grow, start with variant B, which
needs no optimizer change and shows whether recommendations are ever acted on.

## Open questions

1. Build now (A or B), or park stage 3 until January as recommended?
2. If A: may every cycle be required to close inside the horizon (keeps the
   month's tail on the Victron and the model small)?
3. If A or B: how should a recommendation reach you — a Home Assistant
   notification on `switch_required`, or the entities only?
