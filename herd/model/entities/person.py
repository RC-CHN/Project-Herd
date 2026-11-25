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
    relations: dict[str, list[str]] = field(default_factory=dict)
    posture: GuardPosture | None = None
    size: int = 1
    discipline: float = 1.0
    initial_size: int = 0
    roe: str = ""

    def __post_init__(self) -> None:
        if self.initial_size <= 0:
            self.initial_size = max(1, self.size)

    @property
    def is_collective(self) -> bool:
        return self.initial_size > 1

    @property
    def contacts(self) -> list[str]:
        return list(self.relations.keys())

    @contacts.setter
    def contacts(self, peers: list[str]) -> None:
        updated: dict[str, list[str]] = {}
        for peer in peers:
            if peer != self.id and peer not in updated:
                updated[peer] = self.relations.get(peer, ["known"]).copy()
        self.relations = updated

    def rels_for(self, peer_id: str) -> list[str]:
        return list(self.relations.get(peer_id, ()))

    @property
    def is_alive(self) -> bool:
        return self.health > 0 and self.size > 0

    @property
    def is_exhausted(self) -> bool:
        return self.is_alive and self.energy == 0

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
        if self.is_collective and self.discipline < 0.7:
            return False
        return place_comms or self.has_comm(items, snapshot)

    def effective_size(self) -> float:
        return self.size ** (0.3 + 0.6 * self.discipline) if self.size > 0 else 0.0

    def carry_cap(self, base_cap: int) -> int:
        return round(base_cap * math.sqrt(max(1, self.size)))

    def eat_restore(self, base_restore: int) -> int:
        return max(1, round(base_restore / math.sqrt(max(1, self.size))))

    def loss_damage_multiplier(self, rout_shock: float = 1.0) -> float:
        return 1.0 + 0.5 * rout_shock * (1.0 - self.discipline)

    def arms_bonus(self, items: dict, snapshot=None, lethal_arms_scale: float = 1.0) -> float:
        state = self if snapshot is None else snapshot
        inv = state["inv"] if isinstance(state, Mapping) else state.inv
        return max(
            (
                items[item]["combat_bonus"]
                * (1.0 if items[item].get("nonlethal") else lethal_arms_scale)
                for item, qty in inv.items()
                if qty > 0 and items[item]["kind"] == "arms"
            ),
            default=0.0,
        )

    def combat_quality(
        self, items: dict, rules: dict, snapshot=None, *, mode_spec=None, terrain_defense: float = 0.0
    ) -> float:
        state = self if snapshot is None else snapshot
        if isinstance(state, Mapping):
            energy, health = state["energy"], state["health"]
        else:
            energy, health = state.energy, state.health
        lethal_scale = mode_spec["lethal_arms_scale"] if mode_spec else 1.0
        best = self.arms_bonus(items, state, lethal_scale)
        vigor = 1.0 if energy > rules["low_energy_threshold"] else (0.2 if energy == 0 else 0.5)
        return (1.0 + best) * vigor * (health / rules["max_health"]) * (1.0 + terrain_defense)

    def combat_power(
        self, items: dict, rules: dict, snapshot=None, *, mode_spec=None, terrain_defense: float = 0.0
    ) -> float:
        return self.effective_size() * self.combat_quality(
            items, rules, snapshot, mode_spec=mode_spec, terrain_defense=terrain_defense
        )

    def carry_weight(self, items: dict) -> int:
        return inventory_weight(self.inv, items)

    def fit_qty(self, item: str, items: dict, cap: int) -> int | float:
        weight = items[item].get("weight", 1) if item in items else 0
        return max(0, cap - self.carry_weight(items)) // weight if weight else math.inf

    def connect(self, peer_id: str, rels=None) -> None:
        if peer_id == self.id:
            return
        if rels is None:
            if peer_id not in self.relations:
                self.relations[peer_id] = ["acquaintance"]
            return
        tags = [rels] if isinstance(rels, str) else list(rels)
        existing = self.relations.setdefault(peer_id, [])
        for tag in tags:
            if tag and tag not in existing:
                if tag not in ("known", "acquaintance") and "acquaintance" in existing:
                    existing.remove("acquaintance")
                existing.append(tag)

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
            relations={peer: tags.copy() for peer, tags in self.relations.items()},
            posture=self.posture.to_dict() if self.posture else None,
            size=self.size,
            initial_size=self.initial_size,
            discipline=self.discipline,
            roe=self.roe,
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
        if "relations" in data:
            relations = {peer: list(tags) for peer, tags in data["relations"].items()}
        else:
            relations = {peer: ["known"] for peer in data.get("contacts", [])}
        size = data.get("size", 1)
        return cls(
            actor_id,
            data["role"],
            data["tag"],
            data["place"],
            data["energy"],
            data["health"],
            data["inv"].copy(),
            data["docs"].copy(),
            relations,
            posture,
            size,
            float(data.get("discipline", 1.0)),
            data.get("initial_size", max(1, size)),
            data.get("roe", ""),
        )
