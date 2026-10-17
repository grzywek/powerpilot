"""Consumption-meter hierarchy maths.

Meters in a real installation are nested: a sub-meter (washing machine) sits
inside another sub-meter (apartment) which sits inside the main consumption
sensor (Victron output). Naively subtracting every sub-meter from the main
double-counts the nested ones.

This module turns a flat ``{node: parent}`` map into a tree rooted at the main
consumption sensor and computes each node's **exclusive** (own) consumption:

    exclusive(node) = reading(node) − Σ reading(direct children)

Because the tree telescopes, ``Σ exclusive(all nodes) == reading(root)`` — every
kWh is attributed to exactly one node. The two-level model used before this
module is the special case where every node is a direct child of the root.

Two data rules hold for every node: an hour in which a direct child has no
reading is unknown for the parent (a gap is not 0 kWh), and a negative exclusive
value (a child reading above its parent's) is no measurement either. Both hours
are dropped and counted per node, never clamped or zero-filled.

The apartment can be fed from the Victron or from the second meter (stage 2 of
``docs/apartment-supply-planning.plan.md``). Given the per-hour supply and the
apartment's meter, the root's exclusive energy follows the topology really in
place: on the Victron as above; on meter 2 the apartment's subtree is detached
(root − its other children); a mixed or unknown hour gives no root value at all.
The apartment's own subtree does not depend on its source and is computed the
same way in every hour.

The functions here are pure (no Home Assistant dependency) so they can be unit
tested directly and reused by both the learner and the chart series builder.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .const import (
    CONF_CLIMATE_SENSORS,
    CONF_DEVICE_SENSORS,
    SUPPLY_HOUR_UNKNOWN,
    SUPPLY_METER_2,
    SUPPLY_VICTRON,
)

# Sentinel parent value meaning "direct child of the main consumption sensor".
# Stored in config instead of the root entity id so the mapping survives the
# root sensor being swapped out.
PARENT_ROOT = "__root__"


def metered_sensors(config: Mapping[str, Any]) -> list[str]:
    """Every separately-metered load: the device sensors plus the climate ones.

    A weather-dependent meter (AC, heat pump) is a sub-meter like any other: its
    energy has to be broken out of the main reading, or the climate module would
    add its temperature-driven demand on top of a base that still contains it.
    Picking a sensor as weather-dependent therefore implies "separately metered",
    so it gets a weekly profile, a slot in the meter tree and its own panel
    profile even when it is not repeated in the device list.
    """
    devices = list(config.get(CONF_DEVICE_SENSORS) or [])
    seen = set(devices)
    for entity_id in config.get(CONF_CLIMATE_SENSORS) or []:
        if entity_id not in seen:
            seen.add(entity_id)
            devices.append(entity_id)
    return devices


def normalize_parents(
    root: str, devices: list[str], parents: dict[str, str] | None
) -> dict[str, str]:
    """Return ``{device: parent}`` where every parent is a valid ancestor.

    Parents that are unknown, self-referential, point outside the device set, or
    form a cycle fall back to ``root``. The returned values are either the
    ``root`` entity id or another device entity id (never the ``PARENT_ROOT``
    sentinel), so callers can compare directly against ``root``.
    """
    valid = set(devices)
    raw = parents or {}
    result: dict[str, str] = {}
    for node in devices:
        parent = raw.get(node)
        if parent in (None, PARENT_ROOT, root) or parent not in valid or parent == node:
            result[node] = root
        else:
            result[node] = parent

    # Break cycles / dangling chains: any node that cannot reach the root by
    # walking parents is re-parented to the root.
    for node in devices:
        seen: set[str] = set()
        cur = node
        while result[cur] != root:
            if cur in seen or len(seen) > len(devices):
                result[node] = root
                break
            seen.add(cur)
            cur = result[cur]
    return result


def build_children(
    root: str, devices: list[str], parents: dict[str, str] | None
) -> dict[str, list[str]]:
    """Map every node (root + devices) to its list of direct children."""
    norm = normalize_parents(root, devices, parents)
    children: dict[str, list[str]] = {root: []}
    for node in devices:
        children.setdefault(node, [])
    for node, parent in norm.items():
        children.setdefault(parent, []).append(node)
    return children


def subtree(
    root: str, devices: list[str], parents: dict[str, str] | None, node: str
) -> set[str]:
    """``node`` and every meter below it; empty when ``node`` is not a device."""
    if node not in devices:
        return set()
    children = build_children(root, devices, parents)
    out: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        out.add(current)
        stack.extend(children.get(current, []))
    return out


@dataclass
class ExclusiveSeries:
    """Exclusive hourly kWh per node, with the hours that could not be computed.

    ``missing_child`` and ``negative`` count, per node, the hours dropped
    because a direct child had no reading or the value came out negative;
    ``supply_dropped`` counts the root's hours dropped because the apartment's
    supply was mixed or unknown. Nodes with nothing dropped are absent.
    """

    series: dict[str, dict[datetime, float]]
    missing_child: dict[str, int] = field(default_factory=dict)
    negative: dict[str, int] = field(default_factory=dict)
    supply_dropped: int = 0


def exclusive_series(
    root: str,
    devices: list[str],
    parents: dict[str, str] | None,
    series: dict[str, dict[datetime, float]],
    *,
    apartment: str | None = None,
    supply: Mapping[datetime, str] | None = None,
) -> ExclusiveSeries:
    """Compute exclusive hourly kWh for the root and every device.

    ``series`` maps each entity id (root + devices) to its raw ``{hour: kWh}``.
    The result maps the same keys to exclusive ``{hour: kWh}``: raw minus the
    sum of the node's direct children. Hours follow each node's own raw series;
    an hour with a direct child's reading missing, or with a negative result,
    is dropped and counted.

    ``supply`` maps each hour to the apartment's supply (``victron``,
    ``meter_2``, ``mixed`` or ``unknown``; an hour it lacks is ``unknown``) and
    ``apartment`` is the apartment's meter, a direct child of the root. Without
    them every hour is on the Victron.
    """
    children = build_children(root, devices, parents)
    if supply is not None and apartment:
        if apartment not in devices:
            raise ValueError(f"apartment meter {apartment} is not a metered device")
        if apartment not in children[root]:
            raise ValueError(
                f"apartment meter {apartment} must hang directly under {root}"
            )
    result = ExclusiveSeries(series={})
    for node in (root, *devices):
        node_series = series.get(node) or {}
        kids = children.get(node, [])
        exclusive: dict[datetime, float] = {}
        for hour, value in node_series.items():
            hour_kids = kids
            if node == root and supply is not None:
                state = supply.get(hour, SUPPLY_HOUR_UNKNOWN)
                if state == SUPPLY_METER_2 and apartment:
                    hour_kids = [k for k in kids if k != apartment]
                elif state not in (SUPPLY_VICTRON, SUPPLY_METER_2):
                    result.supply_dropped += 1
                    continue
            readings = [(series.get(k) or {}).get(hour) for k in hour_kids]
            if any(r is None for r in readings):
                result.missing_child[node] = result.missing_child.get(node, 0) + 1
                continue
            own = value - sum(r for r in readings if r is not None)
            if own < 0:
                result.negative[node] = result.negative.get(node, 0) + 1
                continue
            exclusive[hour] = own
        result.series[node] = exclusive
    return result
