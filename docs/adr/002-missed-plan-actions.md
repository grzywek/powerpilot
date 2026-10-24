# ADR 002: A durable record of missed plan actions

Status: accepted, 2026-10-24

## Context

PowerPilot has no actuator: Dawid or his automations carry out the plan. The
plan is recomputed every hour and on every relevant state flip, so an action it
asked for and nobody took disappears with the next plan. The car not plugged in
for a cheap hour, or the apartment left on the wrong meter, leaves no trace:

- The hour's vintage is recorded at the hour's first run, after the plan has
  already adapted to reality. The plug gates the running hour (an unplugged car
  gets no charging in it) and the running hour is planned on the apartment's
  actual supply, so the vintage of an hour that was missed reads as if nothing
  had been asked.
- The previous hour's vintage holds the plan from the start of that hour, not
  the last word before the boundary: mid-hour revisions record only the running
  hour's own row.
- The plan-vs-realization card compares an hour with its own vintage, and the
  ESS "plan not executed" watchdog is a live alarm kept in memory. Vintages are
  pruned after 30 days.

## Decision

**The ask.** After every plan run the coordinator keeps the plan's row for the
next clock hour: EV energy and minutes, the apartment's supply, the inverter
mode and the hour's price. That is what the panel's "Następna godzina" column
and the recommendation entities show. It is persisted with the snapshots; when
the hour begins, its last ask is frozen as that hour's ask, persisted, and
written into that hour's vintage as `ask`.

**A miss** is reality contradicting the running hour's ask on something a person
or an automation controls. At most one record per hour and kind:

| Kind | Found when |
|---|---|
| `ev_unplugged` | the ask charges the car (> 0.05 kWh), the calendar has the car available that hour, the car is at home (tracker and presence) and the plug sensor reads unplugged at a run of that hour |
| `supply` | switching is modelled and the confirmed supply is not the meter the ask placed the apartment on, at a run of that hour |
| `ess` | the watchdog's alarm: one record per episode, from the alarm going on to it going off |
| `ev_off_plan` | EV energy in an hour none of its plans asked for (the series' existing rule, now one shared function), recorded when the hour settles |

Without a plug sensor, or with the car's whereabouts unknown, no `ev_unplugged`
miss is recorded: nothing says the cable could have gone in.

**What it cost.** When an `ev_unplugged` or `supply` miss is found, the refresh's
inputs are solved once more with the ask honoured for the rest of the running
hour: the car on the cable, or the apartment on the asked meter as if switched
at the hour's start. The miss costs the plan's PLN objective of reality minus
that of this counterfactual. It is the re-solve the switch recommendations
already value cycles with, so it accounts for the monthly price cap, the battery
and wherever the plan moved the energy. The objective is taken without the cycle
thresholds, the reserve-recovery penalty and the `#ess` bonus
(`Plan.objective_pln`), which are not money.

When the hour ends, the cost is scaled by the share of the miss that stayed
missed: the EV energy the counterfactual would have charged in the rest of the
hour against what the car took after the miss was found (5-minute statistics),
or the minutes on the wrong meter until the switch the supply log dates by its
first agreeing sample. A counterfactual that charges nothing in the rest of
the hour keeps its full cost. A negative cost is kept: by then, following the
plan would have cost more. `ess` and `ev_off_plan` carry no cost, with a note:
the model cannot say what the inverter did instead, and charging nothing asked
for has no planned hour to compare against.

**Storage.** An append-only ledger in its own store,
`powerpilot_<entry>_misses`. It is never pruned and never cleared, not by
"Wyczyść dane" either: it is a record of what happened, not a cache. A record is
open until its hour settles (an ESS episode until the alarm ends), then frozen.
An unreadable file is moved aside (`.corrupt-<date>`), the panel says so, and a
new ledger starts next to it.

**Restarts.** The asks are persisted, a found miss is saved at once, and
settlement reads the recorder and the supply log, so a restart loses nothing
but the watchdog's live state. An ESS episode still open at startup is closed at
its last confirmation, with a note that a restart ended it.

**History.** Hours before this version have no ask, so they have no
`ev_unplugged` or `supply` records. `ev_off_plan` is backfilled from the
vintages that still exist (30 days).

**Shown.** A "Niewykonane zalecenia planu" card on "Przegląd", below the control
card: counts and costs for 7 and 30 days, then the records, newest first, with
the moment and its day, what the plan asked for, what happened and the cost. The
control card's hour columns flag their hour's misses. Each found miss is logged.
`powerpilot/misses` serves the records.

## Rejected

- **The ask taken from the previous hour's vintage (index 1).** It is the plan
  from the start of the previous hour and misses that hour's revisions, so it
  would invent asks the plan had already withdrawn.
- **Displaced energy times a price difference** (the kWh the car missed times
  the price where the plan moved them minus the price asked for). It ignores the
  monthly cap, under which moving energy between hours can cost nothing, and the
  battery, which the EV shares a phase with. The re-solve prices both.
- **Valuing a miss at settlement from recorded prices.** The counterfactual
  needs the model's inputs at the moment of the miss, which are gone by then.
- **Keeping the records in the vintages.** They are pruned after 30 days.
- **A notification per miss.** Notifications are for acting now; a miss is
  history by the time it is recorded.
