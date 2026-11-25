"""Geography and the physical contents of a room."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


def room_summary() -> dict[str, list]:
    return {
        name: []
        for name in (
            "entered",
            "left",
            "guards_dropped",
            "deaths",
            "taken",
            "placed",
            "given",
            "blocked_takes",
            "roe_changes",
        )
    }


@dataclass(slots=True)
class Place:
    id: str
    label: str
    paths: list[str]
    defense: float
    facility: dict | None
    pos: list[float] | None
    items: dict[str, int] = field(default_factory=dict)
    docs: dict[str, int] = field(default_factory=dict)
    board: list[str] = field(default_factory=list)
    regen: dict[str, dict] = field(default_factory=dict)
    last_summary: dict[str, list] = field(default_factory=room_summary)
    comms: bool = False

    @classmethod
    def create(cls, place_id: str, layout: dict) -> Place:
        return cls(
            place_id,
            layout["label"],
            layout["paths"].copy(),
            layout["defense"],
            deepcopy(layout["facility"]),
            deepcopy(layout.get("pos")),
            items=layout["initial_items"].copy(),
            regen=deepcopy(layout["regen"]),
            comms=bool(layout.get("comms", False)),
        )

    @classmethod
    def from_dict(cls, place_id: str, layout: dict, data: dict) -> Place:
        state = deepcopy(data)
        comms = bool(state.pop("comms", layout.get("comms", False)))
        return cls(
            place_id,
            layout["label"],
            layout["paths"].copy(),
            layout["defense"],
            deepcopy(layout["facility"]),
            deepcopy(layout.get("pos")),
            comms=comms,
            **state,
        )

    def to_dict(self) -> dict:
        """Archive only changing state. Static geography lives once in the frozen specification."""
        return dict(
            items=self.items.copy(),
            docs=self.docs.copy(),
            board=self.board.copy(),
            regen=deepcopy(self.regen),
            last_summary=deepcopy(self.last_summary),
            comms=self.comms,
        )
