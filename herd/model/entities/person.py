"""A resident's condition, possessions and relationships."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math

from .guard import GuardPosture
from .place import Place


def inventory_weight(inv: dict[str, int], items: dict) -> int:
    return sum(quantity * items[item].get("weight", 1) for item, quantity in inv.items())


@dataclass(slots=True)
class Person:
    id: str
    role: str
    tag: str
    place: str
    energy: int
    health: int
    inv: dict[str, int] = field(default_factory=dict)
    docs: dict[str, int] = field(default_factory=dict)
    contacts: list[str] = field(default_factory=list)
    posture: GuardPosture | None = None
    size: int = 1
    discipline: float = 1.0

    @property
    def is_alive(self) -> bool:
        return self.health > 0 and self.energy > 0

    def armed(self, items: dict) -> bool:
        return any(qty > 0 and items[item]["kind"] == "arms" for item, qty in self.inv.items())

    def has_comm(self, items: dict, snapshot=None) -> bool:
        if snapshot is None:
            inv = self.inv
        elif isinstance(snapshot, Mapping):
            inv = snapshot["inv"]
        else:
            inv = snapshot.inv
        return any(qty > 0 and items[item]["kind"] == "comm" for item, qty in inv.items())

    def can_remote_comm(self, place_comms: bool, items: dict, snapshot=None) -> bool:
        if self.size > 1 and self.discipline < 0.7:
            return False
        return place_comms or self.has_comm(items, snapshot)

    def effective_size(self) -> float:
        return self.size ** (0.3 + 0.6 * self.discipline)

    def carry_cap(self, base_cap: int) -> int:
        return round(base_cap * math.sqrt(self.size))

    def eat_restore(self, base_restore: int) -> int:
        return max(1, round(base_restore / math.sqrt(self.size)))

    def loss_damage_multiplier(self) -> float:
        return 1.0 + 0.5 * (1.0 - self.discipline)

    def combat_power(self, items: dict, rules: dict, snapshot=None) -> float:
        state = self if snapshot is None else snapshot
        if isinstance(state, Mapping):
            inv, energy, health = state["inv"], state["energy"], state["health"]
        else:
            inv, energy, health = state.inv, state.energy, state.health
        best = max(
            (
                items[item]["combat_bonus"]
                for item, qty in inv.items()
                if qty > 0 and items[item]["kind"] == "arms"
            ),
            default=0,
        )
        vigor = 1 if energy > rules["low_energy_threshold"] else 0.5
        return self.effective_size() * (1 + best) * vigor * health / rules["max_health"]

    def carry_weight(self, items: dict) -> int:
        return inventory_weight(self.inv, items)

    def fit_qty(self, item: str, items: dict, cap: int) -> int | float:
        weight = items[item].get("weight", 1) if item in items else 0
        return max(0, cap - self.carry_weight(items)) // weight if weight else math.inf

    def connect(self, peer_id: str) -> None:
        if peer_id != self.id and peer_id not in self.contacts:
            self.contacts.append(peer_id)

    def drop_all_to(self, place: Place) -> None:
        for bag, ground in ((self.inv, place.items), (self.docs, place.docs)):
            for key, qty in bag.items():
                if qty:
                    ground[key] = ground.get(key, 0) + qty
            bag.clear()

    def to_dict(self) -> dict:
        return dict(
            role=self.role,
            tag=self.tag,
            place=self.place,
            energy=self.energy,
            health=self.health,
            inv=self.inv.copy(),
            docs=self.docs.copy(),
            contacts=self.contacts.copy(),
            posture=self.posture.to_dict() if self.posture else None,
            size=self.size,
            discipline=self.discipline,
        )

    @classmethod
    def from_dict(cls, actor_id: str, data: dict, *, valid_actors, paths, present, items) -> Person:
        posture = (
            GuardPosture.from_dict(
                data["posture"], valid_actors=valid_actors, paths=paths, present=present, items=items
            )
            if data["posture"]
            else None
        )
        return cls(
            actor_id,
            data["role"],
            data["tag"],
            data["place"],
            data["energy"],
            data["health"],
            data["inv"].copy(),
            data["docs"].copy(),
            data["contacts"].copy(),
            posture,
            data.get("size", 1),
            float(data.get("discipline", 1.0)),
        )
