"""Fixture plugin using ``from __future__ import annotations`` with a
``@dataclass`` -- must import cleanly (dataclasses looks the class's module
up in ``sys.modules`` to resolve string annotations).
"""

from __future__ import annotations

from dataclasses import dataclass

METRICS = {
    "dataclass_future.thing.count": {
        "kind": "cumulative",
        "label": "Thing Count",
        "unit": "things",
        "icon": "mdi:counter",
    },
}


@dataclass
class Reading:
    count: int
    label: str = "x"


def collect(config: dict, http) -> dict:
    return {"dataclass_future.thing.count": Reading(count=1).count}
