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
        exhaust_mode = ctx.rules.get("zero_energy_mode", "fatal") == "exhaust"
        if (ctx.tick + 1) % ctx.rules["clock_interval"] == 0:
            for actor in ctx.order:
                person = world.people[actor]
                if person.is_alive:
                    was_exhausted = person.energy == 0
                    if world.places[person.place].sustenance:
                        if person.energy < ctx.rules["max_energy"]:
                            person.energy = min(ctx.rules["max_energy"], person.energy + 1)
                            if was_exhausted and person.energy > 0:
                                ctx.event(
                                    actor,
                                    "notice",
                                    "world",
                                    "【据点补给】你在常驻补给点得到照料，脱离了力竭失能状态。",
                                )
                        if (
                            person.health < ctx.rules["max_health"]
                            and person.energy > ctx.rules["max_energy"] // 2
                        ):
                            person.health += 1
                        continue
                    person.energy = max(0, person.energy - world.energy_decay)
                    if person.energy == 0:
                        if not exhaust_mode:
                            ctx.die(person, "starvation")
                        else:
                            ctx.drop_guard(person)
                            loss = (
                                ctx.rules.get("collective_starvation_health_loss", 2)
                                if person.is_collective
                                else ctx.rules.get("starvation_health_loss", 0)
                            )
                            if loss > 0:
                                person.health = max(0, person.health - loss)
                            if not person.is_alive:
                                ctx.die(person, "starvation")
                            elif not was_exhausted:
                                ctx.enter_exhaustion(person)
                            elif loss > 0:
                                ctx.event(
                                    actor,
                                    "notice",
                                    "world",
                                    f"【持续饥疲】你仍处于力竭失能状态，健康/凝聚力下降 {loss}（当前 {person.health}/{ctx.rules['max_health']}）。",
                                )
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
        active_offers = {}
        for oid, offer in world.offers.items():
            issuer = world.people[offer.issuer]
            if offer.expires > ctx.tick and issuer.is_alive and issuer.place == offer.place:
                active_offers[oid] = offer
            elif issuer.is_alive:
                why = "已过期" if offer.expires <= ctx.tick else "你已离开原地点"
                ctx.event(
                    offer.issuer,
                    "notice",
                    "world",
                    f"【邀约失效】你发布的邀约 {offer.id}（{offer.give_qty} {offer.give_item}）{why}，已自动撤回。",
                )
        world.offers = active_offers
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
        elif kind == "sustenance_set":
            world.places[place].sustenance = event["enabled"]
        if ctx.trace is not None:
            ctx.record("shock", kind=kind, place=place, detail=event.copy(), doc_id=doc_id)
