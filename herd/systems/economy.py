"""Local collection, consumption, delivery and bilateral exchange."""

from ..model.entities import Offer
from .context import adjust


CAPACITY_FAILURE = "failed: carry capacity exceeded"


class EconomySystem:
    def __init__(self, ctx, documents, gates=None):
        self.ctx = ctx
        self.documents = documents
        self.gates = gates

    def run(self):
        ctx = self.ctx
        for actor, intent in ctx.actions("eat", "take", "produce", "give", "accept", "wait"):
            person = ctx.world.people[actor]
            if intent.action == "eat":
                result = self.eat(person, intent.item)
            elif intent.action == "give":
                result = self.give(person, intent)
            elif intent.action == "accept":
                result = self.accept(person, intent.target)
            elif intent.action == "wait":
                result = "waited"
            else:
                result = self.collect(person, intent)
            ctx.result(actor, result)
        for actor, intent in ctx.actions("offer"):
            ctx.result(actor, self.offer(actor, intent))

    def eat(self, person, item):
        ctx = self.ctx
        foods = [
            i
            for i in ctx.start[person.id].inv
            if ctx.items[i]["kind"] == "edible" and ctx.ledger.balance(person.id, i) > 0
        ]
        item = item or next(iter(foods), "")
        if item not in foods:
            return "failed: food unavailable"
        ctx.ledger.spend(person.id, item, 1)
        restore = person.eat_restore(ctx.items[item]["energy_restore"])
        person.energy = min(ctx.rules["max_energy"], person.energy + restore)
        return f"ate {item}"

    def collect(self, person, intent):
        ctx = self.ctx
        if person.id in ctx.disrupted:
            return "failed: disrupted by combat"
        room = ctx.world.places[person.place]
        item, qty = intent.item, int(intent.qty or 1)
        if intent.action == "produce":
            facility = room.facility
            if not facility:
                return "failed: no facility"
            if facility["kind"] == "press":
                return self.documents.copy(person.id, intent.target, facility["copies_per_action"])
            if item and item != facility["item"]:
                return "failed: wrong facility"
            item, qty = facility["item"], facility["yield_per_action"]
        if self.gates is not None:
            blockers = self.gates.effective_blockers(
                self.gates.items_guards(room.id), person.id, ctx.start[person.id]
            )
            if blockers:
                room.last_summary["blocked_takes"].append(dict(actor=person.id, item=item))
                return f"blocked from taking items by {len(blockers)} guards"
        stock = room.items if item in ctx.items else room.docs
        qty = min(qty, stock.get(item, 0))
        if not qty:
            return "failed: empty stock"
        if item in ctx.items:
            qty = min(qty, person.fit_qty(item, ctx.items, person.carry_cap(ctx.rules["carry_cap"])))
            if not qty:
                return CAPACITY_FAILURE
        adjust(stock, item, -qty)
        ctx.ledger.credit(person.id, item, qty)
        if intent.action == "take":
            room.last_summary["taken"].append(dict(actor=person.id, item=item, qty=qty))
        elif cost := ctx.rules.get("exertion_energy_cost", 0):
            person.energy = max(1, person.energy - cost)
        ctx.transfer(intent.action, room.id, room.id, person.id, item, qty)
        return f"collected {qty} {item}"

    def give(self, person, intent):
        ctx = self.ctx
        if person.id in ctx.disrupted:
            return "failed: disrupted by combat"
        target, item, qty = intent.target, intent.item, int(intent.qty or 1)
        if ctx.ledger.balance(person.id, item) < qty:
            return "failed: transfer unavailable"
        if target == "here":
            room = ctx.world.places[person.place]
            ctx.ledger.spend(person.id, item, qty)
            adjust(room.items if item in ctx.items else room.docs, item, qty)
            room.last_summary["placed"].append(dict(actor=person.id, item=item, qty=qty))
            ctx.transfer("put", room.id, person.id, room.id, item, qty)
            return f"placed {qty} {item} here"
        if not ctx.colocated(person.id, target):
            return "failed: transfer unavailable"
        recipient = ctx.world.people[target]
        if item in ctx.items and recipient.fit_qty(item, ctx.items, recipient.carry_cap(ctx.rules["carry_cap"])) < qty:
            return CAPACITY_FAILURE
        self.pay("give", person.id, target, item, qty)
        ctx.world.places[person.place].last_summary["given"].append(
            {"from": person.id, "to": target, "item": item, "qty": qty}
        )
        ctx.event(target, "received", person.id, f"{qty} {item}")
        ctx.connect(person.id, target)
        return f"gave {qty} {item} to {target}"

    def pay(self, kind, actor, peer, item, qty, outcome="completed", offer_id=None):
        ctx = self.ctx
        if qty:
            ctx.ledger.spend(actor, item, qty)
            ctx.ledger.credit(peer, item, qty)
        ctx.transfer(kind, ctx.start[actor].place, actor, peer, item, qty, outcome, offer_id)

    def offer(self, actor, intent):
        ctx = self.ctx
        want, _, price = intent.target.rpartition(":")
        if intent.target == "none":
            want, price = "", 0
        elif (
            want not in ctx.items
            or not price.isascii()
            or not price.isdecimal()
            or not 1 <= int(price) <= 999
        ):
            return "failed: invalid consideration"
        if intent.item not in ctx.items or intent.detail not in ("swap", "fair", "cheat"):
            return "failed: invalid offer"
        offer_id = f"o{ctx.tick}-{actor}"
        ctx.world.offers[offer_id] = Offer(
            id=offer_id,
            issuer=actor,
            place=ctx.start[actor].place,
            tick=ctx.tick,
            expires=ctx.tick + ctx.rules["offer_ttl"],
            mode="swap" if intent.detail == "swap" else "pledge",
            give_item=intent.item,
            give_qty=int(intent.qty or 1),
            want_item=want,
            want_qty=int(price),
            terms=intent.detail,
        )
        return f"offered {offer_id}"

    def accept(self, buyer, offer_id):
        ctx = self.ctx
        offer = ctx.world.offers.get(offer_id)
        if not offer or offer.place != buyer.place or not offer.is_open(ctx.tick):
            return "failed: offer unavailable"
        seller = ctx.world.people[offer.issuer]
        if not ctx.colocated(buyer.id, seller.id):
            return "failed: issuer unavailable"
        want, price = offer.want_item, offer.want_qty
        give = offer.give_item
        funded = ctx.ledger.balance(seller.id, give) >= offer.give_qty
        if (price and ctx.ledger.balance(buyer.id, want) < price) or (offer.mode == "swap" and not funded):
            return "failed: insufficient balance"
        fulfilled = funded and offer.terms != "cheat"
        outcome = "honored" if fulfilled else "breached"
        qty = offer.give_qty if fulfilled else 0
        # An atomic swap frees the outgoing load at the same time as it adds the incoming load.
        weight_change = qty * ctx.items[give]["weight"] - (
            price * ctx.items[want]["weight"] if price else 0
        )
        base_cap = ctx.rules["carry_cap"]
        if (
            buyer.carry_weight(ctx.items) + weight_change > buyer.carry_cap(base_cap)
            or seller.carry_weight(ctx.items) - weight_change > seller.carry_cap(base_cap)
        ):
            return CAPACITY_FAILURE
        if price:
            self.pay(offer.mode, buyer.id, seller.id, want, price, outcome, offer_id)
        self.pay(offer.mode, seller.id, buyer.id, give, qty, outcome, offer_id)
        text = f"{outcome}: {offer_id}, paid {price} {want}, returned {qty} {give}"
        ctx.event(buyer.id, "contract", seller.id, text)
        ctx.event(seller.id, "contract", buyer.id, text)
        del ctx.world.offers[offer_id]
        ctx.connect(buyer.id, seller.id)
        return f"{outcome}: {offer_id}"
