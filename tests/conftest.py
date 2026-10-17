"""Pytest fixtures for PowerPilot integration tests."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom_components/powerpilot in tests."""
    yield


class FakeSupplyRecorder:
    """Recorder stand-in for the supply module's history reads.

    ``history`` maps an entity id to its ``(time, raw state)`` changes in time
    order; reads follow the real API's boundaries: changes strictly inside
    ``(start, end)``, plus the last state strictly before ``start`` when asked
    for (``include_start_time_state``). ``statistics`` maps an entity id to its
    hourly rows (``{"start": timestamp, "mean": value}``).
    """

    def __init__(self, history=None, statistics=None, error=None) -> None:
        self.history = history or {}
        self.statistics = statistics or {}
        self.error = error
        self.windows: list[tuple] = []
        self.statistics_calls: list[tuple] = []

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def state_changes_during_period(
        self, hass, start, end, entity_id, no_attributes, descending, limit, include_start
    ):
        from types import SimpleNamespace

        if self.error is not None:
            raise self.error
        assert no_attributes and not descending
        self.windows.append((entity_id, start, end, limit))
        series = self.history.get(entity_id)
        if series is None:
            return {}
        rows = [
            SimpleNamespace(state=raw, last_updated=at)
            for at, raw in series
            if start < at and (end is None or at < end)
        ]
        if include_start:
            before = [(at, raw) for at, raw in series if at < start]
            if before:
                rows.insert(0, SimpleNamespace(state=before[-1][1], last_updated=start))
        if limit:
            rows = rows[:limit]
        return {entity_id: rows}

    def statistics_during_period(self, hass, start, end, ids, period, units, types):
        if self.error is not None:
            raise self.error
        self.statistics_calls.append((set(ids), period, units, set(types)))
        return {
            entity_id: [
                row
                for row in self.statistics[entity_id]
                if start.timestamp() <= row["start"] < end.timestamp()
            ]
            for entity_id in ids
            if entity_id in self.statistics
        }


@pytest.fixture
def supply_recorder(monkeypatch):
    """Install a :class:`FakeSupplyRecorder` for the supply module; returns the installer."""
    from custom_components.powerpilot.modules import supply

    def install(**kwargs) -> FakeSupplyRecorder:
        recorder = FakeSupplyRecorder(**kwargs)
        monkeypatch.setattr(supply, "get_instance", lambda hass: recorder)
        monkeypatch.setattr(
            supply, "state_changes_during_period", recorder.state_changes_during_period
        )
        monkeypatch.setattr(
            supply, "statistics_during_period", recorder.statistics_during_period
        )
        return recorder

    return install
