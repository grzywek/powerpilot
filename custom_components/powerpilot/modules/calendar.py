"""Calendar module.

Reads every configured HA ``calendar.*`` entity (``CONF_CALENDARS``) once per
planning cycle and turns the events into two shared products:

* ``events`` — all upcoming events (parsed bounds, summary, location). The EV
  module scans these for its ``#<keyword>`` tags across *all* calendars.
* ``trips`` — events with a non-home ``location``. Google Maps (see
  :mod:`..travel`) resolves the one-way driving distance and duration from the
  HA home coordinates; the car is treated as away from ``depart`` (event start
  minus travel time minus the configured margin) until ``return_end`` (event
  end plus travel time plus margin). Those hours are not chargeable and the
  round trip drains the pack via the learned kWh/km.

Everything is steered by ``#tags`` in the summary — see :func:`soc_tag` and
:func:`has_tag`. For the trip builder: ``#ignore`` removes the event entirely and
``#continue`` chains a located event onto the previous one in the same scope
(direct drive, no return to base in between). A located event that fully
contains others changes their base: children drive from/back to the parent's
location, and a child ending exactly with its parent returns straight home
(the parent's own return leg is dropped).

Downstream modules consume this via the coordinator (the registry updates the
calendar before the EV module — order matters).

The house battery reads its own instructions here too (``#ess_socNN`` deadlines,
bare ``#ess`` hours) — it has no module of its own, and the parsing needs no
state beyond the events themselves.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CALENDARS,
    CONF_EV_CALENDAR_KEYWORD,
    CONF_GMAPS_API_KEY,
    CONF_TRAVEL_MARGIN_AFTER_MIN,
    CONF_TRAVEL_MARGIN_BEFORE_MIN,
    DEFAULTS,
    HOME_LOCATION_MARKERS,
    PLANNING_HORIZON_HOURS,
)
from ..models import Forecast
from ..travel import TravelInfo, TravelResolver
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)

# How far ahead calendar events are read — the whole plan horizon, never less.
# A shorter read is invisible: the events simply never arrive, so the far end of
# the plan is made as if the calendar were empty there.
CALENDAR_LOOKAHEAD_HOURS = PLANNING_HORIZON_HOURS
# Summary tags steering the trip builder (case-insensitive):
# * ``#ignore`` — the event does not exist for PowerPilot at all.
# * ``#continue`` — this located event starts where the PREVIOUS located event
#   (same nesting scope) took place: the car drives there directly instead of
#   returning to base in between (dom→Gliwice→Katowice→dom, not two round
#   trips). Chains compose; the final stop still returns to its normal base.
IGNORE_TAG = "ignore"
CONTINUE_TAG = "continue"
# The house battery's stem is fixed; the car's comes from the configured
# keyword (CONF_EV_CALENDAR_KEYWORD, default "Kotek") so it stays the name the
# user already calls the car by: "#kotek_soc100".
ESS_TAG = "ess"
_HOME_LOCATION = "home"

# ``#stem_socNN`` — a SoC deadline for whatever ``stem`` names. The trailing
# "%" is optional because writing it is the natural habit; a comma decimal is
# accepted for the same reason.
_SOC_TAG_RE = re.compile(r"#(\w+)_soc(\d+(?:[.,]\d+)?)\s*%?", re.IGNORECASE)
# ``#stem`` — a bare switch. ``\w+`` is greedy, so "#kotek_soc100" yields the
# stem "kotek_soc100" and can never be mistaken for the bare "#kotek".
_BARE_TAG_RE = re.compile(r"#(\w+)", re.IGNORECASE)


def soc_tag(summary: str, stem: str) -> float | None:
    """SoC (%) requested by ``#<stem>_socNN``, or ``None``.

    Clamped to 0..100 — the deadline machinery downstream clamps again against
    the actual usable band (the car's charge ceiling, the battery's max SoC).
    """
    if not summary or not stem:
        return None
    for match in _SOC_TAG_RE.finditer(summary):
        if match.group(1).lower() == stem.lower():
            try:
                percent = float(match.group(2).replace(",", "."))
            except ValueError:
                return None
            return max(0.0, min(100.0, percent))
    return None


def has_tag(summary: str, stem: str) -> bool:
    """Whether the bare switch ``#<stem>`` is present."""
    if not summary or not stem:
        return False
    return any(
        match.group(1).lower() == stem.lower()
        for match in _BARE_TAG_RE.finditer(summary)
    )


def strip_tags(summary: str, stems: tuple[str, ...]) -> str:
    """Event title with the recognised steering tags removed.

    Only known stems go: an unrelated hashtag is part of the title the user
    wrote and has no business disappearing from the chart.
    """
    lowered = {stem.lower() for stem in stems if stem}
    out: list[str] = []
    for word in summary.split():
        match = _BARE_TAG_RE.fullmatch(word)
        if match:
            stem = match.group(1).lower()
            soc = _SOC_TAG_RE.fullmatch(word)
            if stem in lowered or (soc and soc.group(1).lower() in lowered):
                continue
        out.append(word)
    return " ".join(out)


_RETURN_TRAVEL_UNSET = object()


@dataclass
class CalendarEvent:
    """A parsed calendar event shared with other modules."""

    summary: str
    location: str
    start: datetime
    end: datetime
    calendar: str  # source calendar entity_id


@dataclass
class Trip:
    """A located event: the car is away ``depart`` → ``return_end``.

    The legacy ``distance_km``/``duration_min`` fields describe the outbound leg
    for display. ``outbound_*`` and ``return_*`` carry the exact route legs used
    for EV drain modelling; either leg may be ``None`` when Google Maps could
    not resolve it, and no distance is guessed.

    ``continues`` marks a ``#continue`` hop: the car arrives straight from the
    previous trip's location (its ``origin_location``), so it cannot charge at
    home before this trip's own ``depart`` — the EV module folds the whole
    chain into one pre-departure target at the chain head instead.

    ``calendar`` is the source calendar's entity_id, carried through from the
    event so the panel can say *which* calendar an entry came from — with
    several calendars configured, a trip is otherwise unfindable.
    """

    label: str
    location: str
    event_start: datetime
    event_end: datetime
    depart: datetime
    return_end: datetime
    distance_km: float | None = None
    duration_min: float | None = None
    origin_location: str = _HOME_LOCATION
    return_location: str = _HOME_LOCATION
    outbound_distance_km: float | None = None
    outbound_duration_min: float | None = None
    return_distance_km: float | None = None
    return_duration_min: float | None = None
    continues: bool = False
    calendar: str = ""  # source calendar entity_id
    # ``#<keyword>_socNN`` on the event: the SoC (%) the car must reach before
    # THIS trip departs, overriding the distance-derived floor. ``None`` = no
    # tag, so the automatic reserve+trip target applies.
    soc_target: float | None = None


def trip_window(
    event_start: datetime,
    event_end: datetime,
    travel: TravelInfo | None,
    margin_before_min: float,
    margin_after_min: float,
    return_travel: TravelInfo | None | object = _RETURN_TRAVEL_UNSET,
) -> tuple[datetime, datetime]:
    """Away window for a located event: travel time (if known) plus margins."""
    outbound_min = travel.duration_min if travel is not None else 0.0
    inbound = travel if return_travel is _RETURN_TRAVEL_UNSET else return_travel
    return_min = inbound.duration_min if inbound is not None else 0.0
    depart = event_start - timedelta(minutes=outbound_min + margin_before_min)
    return_end = event_end + timedelta(minutes=return_min + margin_after_min)
    return depart, return_end


def _is_home_location(location: str) -> bool:
    return location.strip().lower() in HOME_LOCATION_MARKERS


def is_trip_location(location: str) -> bool:
    """Whether a location makes the event a trip (the car drives off)."""
    return bool(location and location.strip()) and not _is_home_location(location)


def _same_location(left: str, right: str) -> bool:
    return " ".join(left.split()).lower() == " ".join(right.split()).lower()


class CalendarModule(PowerPilotModule):
    """Reads calendars and turns located events into trips."""

    domain = "calendar"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self.events: list[CalendarEvent] = []
        self.trips: list[Trip] = []
        # House-battery instructions read off the same events: ``#ess_socNN``
        # deadlines (moment, SoC %, label) and bare ``#ess`` hours to charge in.
        self.ess_targets: list[tuple[datetime, float, str]] = []
        self.ess_forced_hours: set[datetime] = set()
        self._resolver: TravelResolver | None = None

    async def async_setup(self) -> None:
        api_key = self.config.get(CONF_GMAPS_API_KEY)
        if api_key:
            self._resolver = TravelResolver(
                self.hass, self.coordinator.entry.entry_id, str(api_key)
            )
            await self._resolver.async_setup()

    async def async_clear_data(self) -> None:
        if self._resolver is not None:
            await self._resolver.async_clear_data()

    async def async_update(self) -> None:
        calendars = list(self.config.get(CONF_CALENDARS) or [])
        self.events = []
        self.trips = []
        self.ess_targets = []
        self.ess_forced_hours = set()
        if not calendars:
            return

        now = dt_util.now()
        end = now + timedelta(hours=CALENDAR_LOOKAHEAD_HOURS)
        for entity_id in calendars:
            for raw in await self._async_fetch_events(entity_id, now, end):
                event = self._parse_event(raw, entity_id)
                if event is not None:
                    self.events.append(event)
        self.events.sort(key=lambda e: e.start)

        await self._build_trips()
        self._build_ess_plans(now)

        self.log_info(
            f"Kalendarze: {len(calendars)} źródeł, {len(self.events)} wydarzeń, "
            f"{len(self.trips)} wyjazdów (z lokalizacją).",
            extra={
                "calendars": calendars,
                "events": len(self.events),
                "trips": [
                    {
                        "label": t.label,
                        "location": t.location,
                        "depart": t.depart.isoformat(),
                        "return_end": t.return_end.isoformat(),
                        "distance_km": t.distance_km,
                    }
                    for t in self.trips
                ],
            },
        )

    def contribute(self, forecast: Forecast) -> None:
        """Tag forecast hours when the car is away on a trip."""
        for slot in forecast.slots:
            hour_end = slot.start + timedelta(hours=1)
            for trip in self.trips:
                if slot.start < trip.return_end and hour_end > trip.depart:
                    slot.tags.append("ev_away")
                    break

    # ------------------------------------------------------------------
    # Trips
    # ------------------------------------------------------------------
    def _ev_tag_stem(self) -> str:
        """Tag stem naming the car — the configured keyword, e.g. "kotek"."""
        return str(
            self.config.get(CONF_EV_CALENDAR_KEYWORD)
            or DEFAULTS[CONF_EV_CALENDAR_KEYWORD]
        ).strip()

    def _tag_stems(self) -> tuple[str, ...]:
        """Every stem that steers PowerPilot, for stripping event labels."""
        return (IGNORE_TAG, CONTINUE_TAG, ESS_TAG, self._ev_tag_stem())

    def _build_ess_plans(self, now: datetime) -> None:
        """House-battery deadlines and forced hours from ``#ess`` tags.

        Unlike the car, the battery never leaves, so an ``#ess_socNN`` deadline
        is always the event's start — there is no departure to aim at. A bare
        ``#ess`` asks for charging in every hour the event covers.
        """
        for event in self.events:
            percent = soc_tag(event.summary, ESS_TAG)
            if percent is not None:
                if event.start > now:  # a passed deadline is not schedulable
                    self.ess_targets.append(
                        (event.start, percent, strip_tags(event.summary, self._tag_stems()))
                    )
                continue
            if not has_tag(event.summary, ESS_TAG):
                continue
            hour = max(event.start, now).replace(minute=0, second=0, microsecond=0)
            while hour < event.end:
                self.ess_forced_hours.add(hour)
                hour += timedelta(hours=1)

    async def _build_trips(self) -> None:
        margin_before = float(
            self.config.get(
                CONF_TRAVEL_MARGIN_BEFORE_MIN, DEFAULTS[CONF_TRAVEL_MARGIN_BEFORE_MIN]
            )
        )
        margin_after = float(
            self.config.get(
                CONF_TRAVEL_MARGIN_AFTER_MIN, DEFAULTS[CONF_TRAVEL_MARGIN_AFTER_MIN]
            )
        )
        parent_by_event = {
            id(event): self._parent_event(event)
            for event in self.events
            if event.location.strip() and not _is_home_location(event.location)
        }
        terminal_parent_ids = {
            id(parent)
            for event in self.events
            for parent in [parent_by_event.get(id(event))]
            if parent is not None
            and event.end == parent.end
            and not _same_location(event.location, parent.location)
        }

        # Located events in chronological order; ``#continue`` links each tagged
        # event to the previous located event in the SAME nesting scope (both
        # top-level, or children of the same parent) — the car drives there
        # directly instead of returning to base in between.
        located = [
            event
            for event in self.events
            if event.location.strip() and not _is_home_location(event.location)
        ]
        continue_from: dict[int, CalendarEvent] = {}
        continued_by: dict[int, CalendarEvent] = {}
        for event in located:
            if not has_tag(event.summary, CONTINUE_TAG):
                continue
            parent = parent_by_event.get(id(event))
            prev = max(
                (
                    candidate
                    for candidate in located
                    if candidate is not event
                    and candidate.start < event.start
                    and parent_by_event.get(id(candidate)) is parent
                ),
                key=lambda c: (c.start, c.end),
                default=None,
            )
            if prev is None:
                self.log_warning(
                    f"Wydarzenie „{event.summary}” ma #{CONTINUE_TAG}, ale nie ma "
                    "wcześniejszego wydarzenia z lokalizacją w tym samym zakresie — "
                    "traktuję jak zwykły wyjazd z bazy.",
                    extra={"event": event.summary},
                )
                continue
            continue_from[id(event)] = prev
            continued_by[id(prev)] = event

        trip_by_event: dict[int, Trip] = {}
        for event in located:
            location = event.location.strip()
            parent = parent_by_event.get(id(event))
            prev = continue_from.get(id(event))
            if prev is not None:
                origin = prev.location.strip()
            else:
                origin = parent.location if parent is not None else _HOME_LOCATION
            # An event at its own base is a no-op stay, not a drive — unless it
            # continues a chain (the car really is elsewhere and comes here).
            if prev is None and _same_location(origin, location):
                continue
            # The car leaves for the NEXT chain stop, not for base: no inbound
            # leg here — the successor's outbound covers the onward drive.
            if id(event) in continued_by:
                return_to: str | None = None
            elif parent is None:
                return_to = (
                    None if id(event) in terminal_parent_ids else _HOME_LOCATION
                )
            else:
                return_to = _HOME_LOCATION if event.end == parent.end else parent.location

            outbound: TravelInfo | None = None
            inbound: TravelInfo | None = None
            if self._resolver is not None:
                outbound = await self._resolver.async_resolve_route(
                    None if origin == _HOME_LOCATION else origin,
                    location,
                )
                if return_to is not None:
                    inbound = await self._resolver.async_resolve_route(
                        location,
                        None if return_to == _HOME_LOCATION else return_to,
                    )
            elif location:
                self.log_warning(
                    "Brak klucza Google Maps — wyjazd "
                    f"„{event.summary}” ({location}) bez dystansu i czasu dojazdu.",
                    extra={"location": location},
                )
            depart, return_end = trip_window(
                event.start,
                event.end,
                outbound,
                margin_before,
                margin_after,
                return_travel=inbound,
            )
            trip = Trip(
                label=strip_tags(event.summary, self._tag_stems()),
                location=location,
                event_start=event.start,
                event_end=event.end,
                depart=depart,
                return_end=return_end,
                distance_km=outbound.distance_km if outbound else None,
                duration_min=outbound.duration_min if outbound else None,
                origin_location=origin,
                return_location=return_to or location,
                outbound_distance_km=outbound.distance_km if outbound else None,
                outbound_duration_min=outbound.duration_min if outbound else None,
                return_distance_km=inbound.distance_km if inbound else None,
                return_duration_min=inbound.duration_min if inbound else None,
                continues=prev is not None,
                calendar=event.calendar,
                soc_target=soc_tag(event.summary, self._ev_tag_stem()),
            )
            trip_by_event[id(event)] = trip
            self.trips.append(trip)

        # Seam chained windows: the predecessor stays "away" until its
        # successor departs (the car sits at the stop, it is not home) — the
        # unavailable window must be continuous across the whole chain.
        for event in located:
            successor = continued_by.get(id(event))
            if successor is None:
                continue
            trip = trip_by_event.get(id(event))
            next_trip = trip_by_event.get(id(successor))
            if trip is None or next_trip is None:
                continue
            trip.return_end = max(trip.event_end, next_trip.depart)

        self.trips.sort(key=lambda t: t.depart)

    def _parent_event(self, event: CalendarEvent) -> CalendarEvent | None:
        """Smallest located event that fully contains ``event``."""
        candidates = [
            candidate
            for candidate in self.events
            if candidate is not event
            and candidate.location.strip()
            and not _is_home_location(candidate.location)
            and candidate.start <= event.start
            and event.end <= candidate.end
            and (candidate.start < event.start or event.end < candidate.end)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda e: (e.end - e.start, e.start))

    # ------------------------------------------------------------------
    # Event fetching / parsing
    # ------------------------------------------------------------------
    async def _async_fetch_events(
        self, cal_entity: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Read events via the public ``calendar.get_events`` service.

        Returns ``[]`` (and logs) when the calendar entity is unavailable — there
        is no alternative source, so the plan simply runs without that calendar.
        """
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "entity_id": cal_entity,
                    "start_date_time": start.isoformat(),
                    "end_date_time": end.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - entity/service may be missing
            self.log_warning(
                f"Nie udało się odczytać kalendarza {cal_entity}: {err}.",
                extra={"calendar": cal_entity},
            )
            return []

        data = (response or {}).get(cal_entity) or {}
        return list(data.get("events") or [])

    def _parse_event(self, raw: dict, cal_entity: str) -> CalendarEvent | None:
        summary = str(raw.get("summary") or "").strip()
        if has_tag(summary, IGNORE_TAG):
            return None
        start = self._parse_dt(raw.get("start"))
        end = self._parse_dt(raw.get("end"))
        if start is None or end is None or end <= start:
            return None
        return CalendarEvent(
            summary=summary,
            location=str(raw.get("location") or "").strip(),
            start=start,
            end=end,
            calendar=cal_entity,
        )

    @staticmethod
    def _parse_dt(value) -> datetime | None:
        """Parse a calendar ``start``/``end`` (datetime or all-day date)."""
        if not value:
            return None
        text = str(value)
        parsed = dt_util.parse_datetime(text)
        if parsed is None:
            day = dt_util.parse_date(text)
            if day is None:
                return None
            parsed = datetime(day.year, day.month, day.day)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return dt_util.as_local(parsed)

    # ------------------------------------------------------------------
    # Panel / diagnostics payloads
    # ------------------------------------------------------------------
    def calendar_name(self, entity_id: str) -> str | None:
        """Friendly name of a configured calendar, or ``None`` if HA has no
        such entity (the entity was renamed or removed after being picked).

        ``None`` is the honest answer — the caller decides how to show a
        calendar that is configured but absent, and the diagnostics report
        flags it as an error rather than papering over it with the raw id.
        """
        state = self.hass.states.get(entity_id)
        return state.name if state is not None else None

    def calendars_payload(self) -> list[dict]:
        """One row per configured calendar: what it is and what it delivered.

        ``events``/``trips`` are the counts from the last read, which is what
        makes this answerable: "the calendar is selected but brought nothing"
        looks completely different from "it was never selected".
        """
        rows: list[dict] = []
        for entity_id in list(self.config.get(CONF_CALENDARS) or []):
            rows.append(
                {
                    "entity_id": entity_id,
                    "name": self.calendar_name(entity_id),
                    "available": self.hass.states.get(entity_id) is not None,
                    "events": sum(1 for e in self.events if e.calendar == entity_id),
                    "trips": sum(1 for t in self.trips if t.calendar == entity_id),
                }
            )
        return rows

    def plan_summary(self) -> dict:
        """Serialisable calendar snapshot for the panel."""
        return {
            "calendars": self.calendars_payload(),
            "events": len(self.events),
            "trips": [
                {
                    "label": t.label,
                    "location": t.location,
                    "calendar": t.calendar,
                    "calendar_name": self.calendar_name(t.calendar),
                    "event_start": t.event_start.isoformat(),
                    "event_end": t.event_end.isoformat(),
                    "depart": t.depart.isoformat(),
                    "return_end": t.return_end.isoformat(),
                    "distance_km": t.distance_km,
                    "duration_min": t.duration_min,
                    "origin_location": t.origin_location,
                    "return_location": t.return_location,
                    "outbound_distance_km": t.outbound_distance_km,
                    "return_distance_km": t.return_distance_km,
                    "outbound_duration_min": t.outbound_duration_min,
                    "return_duration_min": t.return_duration_min,
                }
                for t in self.trips
            ],
        }
