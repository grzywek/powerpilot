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

    ``distance_km``/``duration_min`` are the *one-way* Google Maps values;
    ``None`` when travel could not be resolved (no API key, unknown address) —
    then the away window is just the event span plus margins, with no
    drive-energy model.
    """

    label: str
    location: str
    event_start: datetime
    event_end: datetime
    depart: datetime
    return_end: datetime
    distance_km: float | None = None
    duration_min: float | None = None


def trip_window(
    event_start: datetime,
    event_end: datetime,
    travel: TravelInfo | None,
    margin_before_min: float,
    margin_after_min: float,
) -> tuple[datetime, datetime]:
    """Away window for a located event: travel time (if known) plus margins."""
    travel_min = travel.duration_min if travel is not None else 0.0
    depart = event_start - timedelta(minutes=travel_min + margin_before_min)
    return_end = event_end + timedelta(minutes=travel_min + margin_after_min)
    return depart, return_end


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
        for event in self.events:
            location = event.location.strip()
            if not location or location.lower() in HOME_LOCATION_MARKERS:
                continue
            travel: TravelInfo | None = None
            if self._resolver is not None:
                travel = await self._resolver.async_resolve(location)
            elif location:
                self.log_warning(
                    "Brak klucza Google Maps — wyjazd "
                    f"„{event.summary}” ({location}) bez dystansu i czasu dojazdu.",
                    extra={"location": location},
                )
            depart, return_end = trip_window(
                event.start, event.end, travel, margin_before, margin_after
            )
            self.trips.append(
                Trip(
                    label=event.summary,
                    location=location,
                    event_start=event.start,
                    event_end=event.end,
                    depart=depart,
                    return_end=return_end,
                    distance_km=travel.distance_km if travel else None,
                    duration_min=travel.duration_min if travel else None,
                )
            )
        self.trips.sort(key=lambda t: t.depart)

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
        start = self._parse_dt(raw.get("start"))
        end = self._parse_dt(raw.get("end"))
        if start is None or end is None or end <= start:
            return None
        return CalendarEvent(
            summary=str(raw.get("summary") or "").strip(),
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
                }
                for t in self.trips
            ],
        }
