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

The functions here are pure (no Home Assistant dependency) so they can be unit
tested directly and reused by both the learner and the chart series builder.
"""

from __future__ import annotations

from datetime import datetime

# Sentinel parent value meaning "direct child of the main consumption sensor".
# Stored in config instead of the root entity id so the mapping survives the
# root sensor being swapped out.
PARENT_ROOT = "__root__"


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


def exclusive_series(
    root: str,
    devices: list[str],
    parents: dict[str, str] | None,
    series: dict[str, dict[datetime, float]],
) -> dict[str, dict[datetime, float]]:
    """Compute exclusive hourly kWh for the root and every device.

    ``series`` maps each entity id (root + devices) to its raw ``{hour: kWh}``.
    Returns the same keys mapped to exclusive ``{hour: kWh}`` (raw minus the sum
    of the node's direct children, clamped at 0). Hours follow each node's own
    raw series; a child reading missing for an hour is treated as 0.
    """
    children = build_children(root, devices, parents)
    out: dict[str, dict[datetime, float]] = {}
    for node in (root, *devices):
        node_series = series.get(node) or {}
        kids = children.get(node, [])
        exclusive: dict[datetime, float] = {}
        for hour, value in node_series.items():
            kids_total = sum((series.get(k) or {}).get(hour, 0.0) for k in kids)
            exclusive[hour] = max(0.0, value - kids_total)
        out[node] = exclusive
    return out
