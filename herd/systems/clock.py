"""Periodic physiology and renewal, boundary changes and the closing state."""

from math import floor
from ..model.entities import Document
from .context import adjust


class NaturalClock:
    def __init__(self, ctx):
        self.ctx = ctx

    def run(self, gates, documents):
        ctx = self.ctx
        world = ctx.world
        if (ctx.tick + 1) % ctx.rules["clock_interval"] == 0:
            for actor in ctx.order:
                person = world.people[actor]
                if person.is_alive:
                    person.energy = max(0, person.energy - world.energy_decay)
                    if person.energy == 0:
                        ctx.die(person, "starvation")
                    elif (
                        person.health < ctx.rules["max_health"]
                        and person.energy > ctx.rules["max_energy"] // 2
                    ):
                        person.health += 1
            gates.release_absent_escorts(ctx)
            for room in world.places.values():
                for item, regen in room.regen.items():
                    stock = room.items.get(item, 0)
                    # The renewal cap limits growth. Deposited and dropped units remain physical.
                    growth = min(regen["rate"], max(0, regen["cap"] - stock))
                    adjust(room.items, item, growth)
        for entries in (world.spec["shocks"], world.spec["notices"]):
            for index, scheduled in enumerate(entries):
                if scheduled["tick"] != ctx.tick:
                    continue
                if "kind" in scheduled:
                    self.shock(scheduled, index, documents)
                places = scheduled["announce_places"]
                for actor in ctx.order:
                    person = world.people[actor]
                    if person.is_alive and (places == "*" or person.place in places):
                        ctx.event(actor, "notice", scheduled["source"], scheduled["text"])
        world.offers = {
            oid: offer
            for oid, offer in world.offers.items()
            if offer.expires > ctx.tick
            and world.people[offer.issuer].is_alive
            and world.people[offer.issuer].place == offer.place
        }
        world.tick += 1

    def shock(self, event, index, documents):
        ctx = self.ctx
        world = ctx.world
        kind, place, item = event["kind"], event.get("place"), event.get("item")
        doc_id = None
        if kind == "regen_multiplier":
            regen = world.places[place].regen[item]
            regen["rate"] = floor(regen["rate"] * event["multiplier"])
        elif kind == "energy_decay_set":
            world.energy_decay = event["value"]
        elif kind == "items_delta":
            stock = world.places[place].items
            adjust(stock, item, max(-stock.get(item, 0), event["delta"]))
        elif kind == "drop_doc":
            doc_id = f"x{ctx.tick}-{index}"
            world.documents[doc_id] = Document(
                id=doc_id, tick=ctx.tick, author=event["source"], text=event["text"], seal="", signers=[]
            )
            documents.post(world.places[place], doc_id)
        elif kind == "comms_set":
            world.places[place].comms = event["enabled"]
        if ctx.trace is not None:
            ctx.record("shock", kind=kind, place=place, detail=event.copy(), doc_id=doc_id)
