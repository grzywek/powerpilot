"""Calendar module.

Reads every configured HA ``calendar.*`` entity (``CONF_CALENDARS``) once per
planning cycle and turns the events into two shared products:

* ``events`` — all upcoming events (parsed bounds, summary, location). The EV
  module scans these for its charging keyword ("Kotek 100%" deadlines, bare
  "Kotek" forced windows) across *all* calendars.
* ``trips`` — events with a non-home ``location``. Google Maps (see
  :mod:`..travel`) resolves the one-way driving distance and duration from the
  HA home coordinates; the car is treated as away from ``depart`` (event start
  minus travel time minus the configured margin) until ``return_end`` (event
  end plus travel time plus margin). Those hours are not chargeable and the
  round trip drains the pack via the learned kWh/km.

Two summary tags steer the builder: ``#ignore`` removes the event entirely and
``#continue`` chains a located event onto the previous one in the same scope
(direct drive, no return to base in between). A located event that fully
contains others changes their base: children drive from/back to the parent's
location, and a child ending exactly with its parent returns straight home
(the parent's own return leg is dropped).

Downstream modules consume this via the coordinator (the registry updates the
calendar before the EV module — order matters).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CALENDARS,
    CONF_GMAPS_API_KEY,
    CONF_TRAVEL_MARGIN_AFTER_MIN,
    CONF_TRAVEL_MARGIN_BEFORE_MIN,
    DEFAULTS,
    HOME_LOCATION_MARKERS,
)
from ..models import Forecast
from ..travel import TravelInfo, TravelResolver
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)

# How far ahead calendar events are read (matches the optimizer horizon cap).
CALENDAR_LOOKAHEAD_HOURS = 96
# Summary tags steering the trip builder (case-insensitive):
# * ``#ignore`` — the event does not exist for PowerPilot at all.
# * ``#continue`` — this located event starts where the PREVIOUS located event
#   (same nesting scope) took place: the car drives there directly instead of
#   returning to base in between (dom→Gliwice→Katowice→dom, not two round
#   trips). Chains compose; the final stop still returns to its normal base.
IGNORE_EVENT_TAG = "#ignore"
CONTINUE_EVENT_TAG = "#continue"
_HOME_LOCATION = "home"
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


def _same_location(left: str, right: str) -> bool:
    return " ".join(left.split()).lower() == " ".join(right.split()).lower()


def _strip_tags(summary: str) -> str:
    """Trip label without the steering tags (case-insensitive)."""
    out = summary
    for tag in (CONTINUE_EVENT_TAG,):
        index = out.lower().find(tag)
        while index >= 0:
            out = out[:index] + out[index + len(tag) :]
            index = out.lower().find(tag)
    return " ".join(out.split())


class CalendarModule(PowerPilotModule):
    """Reads calendars and turns located events into trips."""

    domain = "calendar"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self.events: list[CalendarEvent] = []
        self.trips: list[Trip] = []
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
            if CONTINUE_EVENT_TAG not in event.summary.lower():
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
                    f"Wydarzenie „{event.summary}” ma {CONTINUE_EVENT_TAG}, ale nie ma "
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
                label=_strip_tags(event.summary),
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
        if IGNORE_EVENT_TAG in summary.lower():
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

    def plan_summary(self) -> dict:
        """Serialisable calendar snapshot for the panel."""
        return {
            "calendars": list(self.config.get(CONF_CALENDARS) or []),
            "events": len(self.events),
            "trips": [
                {
                    "label": t.label,
                    "location": t.location,
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
