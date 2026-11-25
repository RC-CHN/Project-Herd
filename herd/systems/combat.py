"""Resolve contested path and person actions, and deliver local physical evidence."""

from collections import defaultdict

from .context import adjust, sample


class CombatSystem:
    def __init__(self, ctx, gates):
        self.ctx = ctx
        self.gates = gates

    def run(self):
        ctx = self.ctx
        paths, items_teams, persons = defaultdict(list), defaultdict(list), defaultdict(list)
        for actor, intent in ctx.actions("force"):
            place = ctx.start[actor].place
            if intent.target == "items":
                items_teams[place].append(actor)
                continue
            kind, _, target = intent.target.partition(":")
            if kind == "path" and target in ctx.world.places[place].paths:
                paths[place, target].append(actor)
            elif kind == "person" and ctx.colocated(actor, target):
                persons[target].append(actor)
            else:
                ctx.result(actor, "failed: invalid force target")
        for (place, target), team in paths.items():
            self.force_path(place, target, team)
        for place, team in items_teams.items():
            self.force_items(place, team)
        for target, team in persons.items():
            place = ctx.start[target].place
            attackers = [a for a in team if ctx.world.people[a].is_alive]
            victim = ctx.world.people[target]
            if not victim.is_alive or victim.place != place:
                for actor in attackers:
                    ctx.result(actor, "failed: target unavailable")
            elif attackers:
                escorts = [g for g in self.gates.escorts(place, target) if g != target]
                won = self.fight(place, "person", target, attackers, [target, *escorts])
                if won and victim.is_alive:
                    self.disarm(place, victim)

    def disarm(self, place, victim):
        ctx = self.ctx
        candidates = [
            item
            for item, qty in victim.inv.items()
            if qty > 0 and ctx.ledger.balance(victim.id, item) > 0
        ]
        if not candidates:
            return
        dropped = max(
            candidates,
            key=lambda item: (ctx.items[item]["kind"] == "arms", ctx.items[item].get("weight", 1), item),
        )
        room = ctx.world.places[place]
        ctx.ledger.spend(victim.id, dropped, 1)
        adjust(room.items, dropped, 1)
        room.last_summary["placed"].append(dict(actor=victim.id, item=dropped, qty=1))
        ctx.transfer("disarm", room.id, victim.id, room.id, dropped, 1)
        ctx.result(victim.id, f"dropped 1 {dropped} in combat")

    def force_items(self, place, team):
        ctx = self.ctx
        guards = self.gates.items_guards(place)
        contested, defenders = [], set()
        for actor in team:
            if not ctx.world.people[actor].is_alive:
                continue
            blockers = self.gates.effective_blockers(guards, actor, ctx.start[actor])
            if blockers:
                contested.append(actor)
                defenders.update(blockers)
            else:
                ctx.result(actor, "items uncontested")
        if not contested:
            return
        blockers = sorted(defenders)
        won = self.fight(place, "items", "items", contested, blockers)
        if won:
            for guard in blockers:
                ctx.drop_guard(ctx.world.people[guard])

    def force_path(self, place, target, team):
        ctx = self.ctx
        guards = self.gates.edge_guards(place, target)
        contested, defenders = [], set()
        for actor in team:
            blockers = self.gates.effective_blockers(guards, actor, ctx.start[actor])
            if blockers:
                contested.append(actor)
                defenders.update(blockers)
            else:
                self.gates.move(ctx, actor, target, [])
        if not contested:
            return
        blockers = sorted(defenders)
        won = self.fight(place, "path", target, contested, blockers)
        if won:
            for guard in blockers:
                ctx.drop_guard(ctx.world.people[guard])
        for actor in contested:
            if ctx.world.people[actor].is_alive:
                self.gates.move(ctx, actor, target, [] if won else blockers)

    def fight(self, place, kind, target, attackers, defenders):
        ctx = self.ctx
        scope = "items" if kind == "items" else f"{kind}:{target}"
        jitter = sample(f"combat/{ctx.world.seed}/{ctx.tick}/force/{place}/{scope}")
        attack = self.gates.team_strength(attackers, ctx.start) * (0.85 + 0.30 * jitter)
        defense = self.gates.team_strength(defenders, ctx.start, terrain=kind == "path")
        ratio = attack / (attack + defense)
        won = ratio >= 0.5
        scale = ctx.rules["combat_damage_scale"]
        loss = max(2, round(4 * max(ratio, 1 - ratio) * 1.4))
        loss = max(1, round(loss * scale)) if scale else 0
        win = round(max(0, round(3 * min(ratio, 1 - ratio))) * scale)
        winner = "attackers" if won else "defenders"
        if ctx.trace is not None:
            damage, deaths = {}, []
            ctx.record(
                "combat",
                kind=kind,
                place=place,
                edge=sorted((place, target)) if kind == "path" else None,
                attackers=attackers,
                defenders=defenders,
                winner=winner,
                damage=damage,
                deaths=deaths,
            )
        participants = attackers + defenders
        for side, side_won in ((attackers, won), (defenders, not won)):
            base_hit = win if side_won else loss
            for actor in side:
                person = ctx.world.people[actor]
                hit = (
                    base_hit
                    if side_won or not base_hit
                    else max(1, round(base_hit * person.loss_damage_multiplier()))
                )
                person.health = max(0, person.health - hit)
                person.energy = max(0, person.energy - 1)
                if not side_won and hit > 0:
                    ctx.disrupted.add(actor)
                ctx.result(actor, f"combat {kind} {'won' if side_won else 'lost'}: damage {hit}")
                if ctx.trace is not None:
                    damage[actor] = hit
        visible_places = {place, target} if kind == "path" else {place}
        detail = f"{','.join(attackers)} forced {scope} against {','.join(defenders)}: {winner} won"
        for person in ctx.world.people.values():
            if person.is_alive and person.place in visible_places and person.id not in participants:
                ctx.event(person.id, "combat_witness", attackers[0], detail)
        for actor in participants:
            person = ctx.world.people[actor]
            if not person.is_alive:
                ctx.die(person, "combat")
                if ctx.trace is not None:
                    deaths.append(actor)
        return won
