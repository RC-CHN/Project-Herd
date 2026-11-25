"""Aggregate root for one deterministic polity run."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field

from .entities import Document, Offer, Person, Place
from .intent import Intent
from .observation import observe
from .scenario import ENGINE_POLITY, initial_population, load
from .scenario.population import assign_personas
from .scenario.validator import integer


@dataclass(slots=True)
class World:
    scenario: str
    seed: int
    tick: int
    spec: dict
    energy_decay: int
    places: dict[str, Place]
    people: dict[str, Person]
    documents: dict[str, Document]
    offers: dict[str, Offer]
    dynamic_items: dict[str, dict] = field(default_factory=dict)

    def all_items(self) -> dict:
        if not self.dynamic_items:
            return self.spec["items"]
        return {**self.spec["items"], **self.dynamic_items}

    @classmethod
    def create(cls, spec, count: int | None, seed: int) -> World:
        spec = load(spec)
        if count is None:
            count = spec["default_residents"]
        integer(count, 2, "resident count")
        places = {p: Place.create(p, layout) for p, layout in spec["places"].items()}
        return cls(
            ENGINE_POLITY,
            seed,
            0,
            spec,
            spec["rules"]["base_energy_decay"],
            places,
            initial_population(spec, count, seed),
            {},
            {},
        )

    @classmethod
    def from_dict(cls, data: dict) -> World:
        if "energy_decay" not in data and "env" in data:
            raise ValueError(
                "pre-refactor world format: replay this archive with the version that created it"
            )
        if data.get("scenario") != ENGINE_POLITY:
            raise ValueError(f"unsupported world: {data.get('scenario')}")
        spec = deepcopy(data["spec"])
        places = {p: Place.from_dict(p, spec["places"][p], state) for p, state in data["places"].items()}
        actors = data["people"].keys()
        present = defaultdict(set)
        for actor, state in data["people"].items():
            if state["health"] > 0 and state.get("size", 1) > 0:
                present[state["place"]].add(actor)
        dynamic_items = deepcopy(data.get("dynamic_items", {}))
        all_items = {**spec["items"], **dynamic_items} if dynamic_items else spec["items"]
        people = {
            actor: Person.from_dict(
                actor,
                state,
                valid_actors=actors,
                present=present[state["place"]],
                paths=places[state["place"]].paths,
                items=all_items,
            )
            for actor, state in data["people"].items()
        }
        documents = {d: Document(d, **deepcopy(state)) for d, state in data["documents"].items()}
        offers = {o: Offer(o, **state) for o, state in data["offers"].items()}
        return cls(
            data["scenario"],
            data["seed"],
            data["tick"],
            spec,
            data["energy_decay"],
            places,
            people,
            documents,
            offers,
            dynamic_items,
        )

    def to_dict(self) -> dict:
        return dict(
            scenario=self.scenario,
            seed=self.seed,
            tick=self.tick,
            spec=deepcopy(self.spec),
            energy_decay=self.energy_decay,
            places={p: room.to_dict() for p, room in self.places.items()},
            people={a: person.to_dict() for a, person in self.people.items()},
            documents={d: doc.to_dict() for d, doc in self.documents.items()},
            offers={o: offer.to_dict() for o, offer in self.offers.items()},
            dynamic_items=deepcopy(self.dynamic_items),
        )

    def active_residents(self) -> list[str]:
        return [actor for actor, person in self.people.items() if person.is_alive]

    def personas(self) -> dict[str, str]:
        return assign_personas(self.spec, self.people, self.seed)

    def build_index(self):
        from ..systems.comm import CommSystem

        return CommSystem(self)

    def observe(self, actor_id: str, index=None) -> dict:
        return observe(self, actor_id, index if index is not None else self.build_index())

    def settle(self, intents: dict[str, Intent], trace: list | None = None) -> list:
        from ..systems import settle

        return settle(self, intents, trace=trace)

    def statistics(self) -> dict:
        living = [p for p in self.people.values() if p.is_alive]
        return dict(
            scenario=self.spec["id"],
            engine=self.scenario,
            alive=len(living),
            deaths=len(self.people) - len(living),
            group_sizes=Counter(p.role for p in self.people.values()),
            venue_sizes=Counter(p.place for p in living),
            documents=len(self.documents),
            active_guards=sum(p.posture is not None for p in living),
        )
