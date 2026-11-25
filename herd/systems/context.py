"""Opening eligibility, inventory accounting, and event sinks shared by a single round."""

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256

from ..model.entities import room_summary


def sample(key):
    return int.from_bytes(sha256(key.encode()).digest()[:4], "big") / 2**32


def adjust(stock, item, delta):
    quantity = stock.get(item, 0) + delta
    if quantity:
        stock[item] = quantity
    else:
        stock.pop(item, None)


@dataclass(frozen=True)
class OpeningSnapshot:
    place: str
    energy: int
    health: int
    inv: dict[str, int]
    docs: dict[str, int]

    @classmethod
    def of(cls, person):
        return cls(person.place, person.energy, person.health, person.inv.copy(), person.docs.copy())


class InventoryLedger:
    """A receipt changes physical weight immediately, but becomes spendable next round."""

    def __init__(self, world, opening):
        self.world = world
        self.opening = opening
        self.spent = Counter()

    def balance(self, actor, item):
        before = self.opening[actor]
        bag = before.inv if item in self.world.spec["items"] else before.docs
        return bag.get(item, 0) - self.spent[actor, item]

    def spend(self, actor, item, qty):
        self.spent[actor, item] += qty
        self.credit(actor, item, -qty)

    def credit(self, actor, item, qty):
        person = self.world.people[actor]
        adjust(person.inv if item in self.world.spec["items"] else person.docs, item, qty)


class RoundContext:
    def __init__(self, world, intents, trace=None):
        self.world, self.intents, self.trace = world, intents, trace
        self.tick = world.tick
        self.rules = world.spec["rules"]
        self.items = world.spec["items"]
        self.start = {a: OpeningSnapshot.of(p) for a, p in world.people.items() if p.is_alive}
        self.order = sorted(
            self.start, key=lambda a: sha256(f"{world.seed}/{self.tick}/{a}".encode()).digest()
        )
        self.actors = [actor for actor in self.order if actor in intents]
        self.open_boards = {room.id: set(room.board) for room in world.places.values()}
        self.ledger = InventoryLedger(world, self.start)
        self.disrupted = set()
        self.events = []
        for room in world.places.values():
            room.last_summary = room_summary()

    def actions(self, *kinds):
        for actor in self.actors:
            intent = self.intents[actor]
            if self.world.people[actor].is_alive and intent.action in kinds:
                yield actor, intent

    def colocated(self, actor, peer):
        return (
            peer != actor
            and peer in self.start
            and self.world.people[peer].is_alive
            and self.start[peer].place == self.start[actor].place
        )

    def connect(self, actor, peer):
        self.world.people[actor].connect(peer)
        self.world.people[peer].connect(actor)

    def event(self, viewer, kind, source, detail):
        self.events.append((viewer, self.tick, kind, source, detail))

    def result(self, actor, detail):
        self.event(actor, "result", actor, detail)

    def record(self, event_type, **fields):
        self.trace.append({"tick": self.tick, "type": event_type, **fields})

    def transfer(self, kind, place, source, target, item, qty, outcome="completed", offer_id=None):
        if self.trace is not None:
            self.record(
                "transfer",
                kind=kind,
                place=place,
                **{"from": source, "to": target},
                item=item,
                qty=qty,
                outcome=outcome,
                offer_id=offer_id,
            )

    def drop_guard(self, person):
        if person.posture:
            if self.trace is not None:
                self.record(
                    "guard", actor=person.id, place=person.place, **person.posture.to_dict(), active=False
                )
            self.world.places[person.place].last_summary["guards_dropped"].append(person.id)
            person.posture = None

    def die(self, person, cause):
        room = self.world.places[person.place]
        person.health = 0
        self.drop_guard(person)
        room.last_summary["deaths"].append(person.id)
        self.event(person.id, "death", person.id, cause)
        if person.size > 1:
            detail = "dispersed (routed in combat)" if cause == "combat" else "dispersed (exhausted)"
        else:
            detail = "was killed in combat" if cause == "combat" else "died of starvation"
        for witness in self.world.people.values():
            if witness.is_alive and witness.place == person.place:
                self.event(witness.id, "notice", "world", f"{person.id} {detail}")
        if self.trace is not None:
            self.record(
                "death",
                actor=person.id,
                place=room.id,
                cause=cause,
                dropped_inv=person.inv.copy(),
                dropped_docs=person.docs.copy(),
            )
            for bag in (person.inv, person.docs):
                for item, qty in bag.items():
                    if qty:
                        self.transfer("drop", room.id, person.id, room.id, item, qty)
        person.drop_all_to(room)
