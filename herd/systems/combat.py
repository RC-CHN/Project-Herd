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
        participants = attackers + defenders
        modes = ctx.rules["force_modes"]
        default_mode = min(modes, key=lambda k: (modes[k]["severity"], k))

        def actor_mode(actor):
            m = ctx.world.people[actor].roe
            return m if m in modes else default_mode

        mode_id = max(
            (actor_mode(a) for a in participants),
            key=lambda m: (modes[m]["severity"], m),
        )
        mode_spec = modes[mode_id]
        escalators = [a for a in participants if actor_mode(a) == mode_id]

        jitter = sample(f"combat/{ctx.world.seed}/{ctx.tick}/force/{place}/{scope}")
        attack = self.gates.team_strength(attackers, ctx.start, mode_spec=mode_spec) * (0.85 + 0.30 * jitter)
        defense = self.gates.team_strength(
            defenders, ctx.start, terrain=kind == "path", mode_spec=mode_spec
        )
        won = attack >= defense
        top, low = max(attack, defense), min(attack, defense)
        disadvantage = low / top if top > 0 else 1.0
        scale = ctx.rules["combat_damage_scale"] * mode_spec["damage_scale"]
        exp = mode_spec["lanchester_exp"]
        loss = max(1, round((2.5 + 3.5 * (1.0 - disadvantage)) * scale)) if scale else 0
        win = round(3.0 * (disadvantage**exp) * scale) if scale else 0
        lethal = mode_spec["lethal"]
        rout_shock = mode_spec["rout_shock"]
        casualty_rate = mode_spec["casualty_rate"]
        winner = "attackers" if won else "defenders"
        casualties = {}
        routed = {}
        if ctx.trace is not None:
            damage, deaths = {}, []
            ctx.record(
                "combat",
                kind=kind,
                mode=mode_id,
                escalated_by=escalators,
                place=place,
                edge=sorted((place, target)) if kind == "path" else None,
                attackers=attackers,
                defenders=defenders,
                winner=winner,
                damage=damage,
                casualties=casualties,
                routed=routed,
                deaths=deaths,
            )
        for side, side_won in ((attackers, won), (defenders, not won)):
            base_hit = win if side_won else loss
            for actor in side:
                person = ctx.world.people[actor]
                hit = (
                    base_hit
                    if side_won or not base_hit
                    else max(1, round(base_hit * person.loss_damage_multiplier(rout_shock)))
                )
                floor_hp = 0 if lethal else 1
                person.health = max(floor_hp, person.health - hit)
                person.energy = max(0 if lethal else 1, person.energy - 1)
                lost_pop = 0
                if lethal and hit > 0:
                    if person.is_collective:
                        ratio = min(1.0, hit / ctx.rules["max_health"])
                        raw = round(person.size * ratio * casualty_rate)
                        if hit >= 2 and casualty_rate > 0:
                            raw = max(1, raw)
                        lost_pop = min(person.size, raw)
                        person.size -= lost_pop
                    elif not person.is_alive:
                        lost_pop = 1
                        person.size = 0
                casualties[actor] = lost_pop
                if not side_won and hit > 0:
                    ctx.disrupted.add(actor)
                msg = f"combat {kind} ({mode_id}) {'won' if side_won else 'lost'}: damage {hit}"
                if lost_pop and person.is_collective:
                    msg += f", casualties {lost_pop} (remaining {person.size})"
                ctx.result(actor, msg)
                if ctx.trace is not None:
                    damage[actor] = hit
                if (
                    not side_won
                    and lethal
                    and person.is_collective
                    and person.health == 0
                    and person.size > 0
                    and person.energy > 0
                    and ctx.start[actor].health > 1
                ):
                    dest = self._rout_exit(actor, kind, place, target)
                    if dest is not None:
                        old_place = person.place
                        ctx.drop_guard(person)
                        person.health = 1
                        person.place = dest
                        ctx.world.places[old_place].last_summary["left"].append(actor)
                        ctx.world.places[dest].last_summary["entered"].append(actor)
                        routed[actor] = dest
                        ctx.result(
                            actor,
                            f"routed from {old_place} to {dest} (casualties {lost_pop}, remaining {person.size}, health 1)",
                        )
                        notice = (
                            f"{actor} suffered {lost_pop} casualties and routed from {old_place} to {dest} (remaining {person.size})"
                            if lost_pop
                            else f"{actor} routed from {old_place} to {dest} (remaining {person.size})"
                        )
                        for witness in ctx.world.people.values():
                            if witness.is_alive and witness.id != actor and witness.place in (old_place, dest):
                                ctx.event(witness.id, "notice", "world", notice)
        visible_places = {place, target} if kind == "path" else {place}
        total_cas = sum(casualties.values())
        detail = (
            f"{','.join(attackers)} forced {scope} "
            f"({mode_spec['label']}, roe={mode_id} by {','.join(escalators)}) "
            f"against {','.join(defenders)}: {winner} won"
        )
        if total_cas:
            detail += f", casualties {total_cas}"
        if routed:
            detail += f", routed {','.join(f'{a}->{d}' for a, d in routed.items())}"
        for person in ctx.world.people.values():
            if person.is_alive and person.place in visible_places and person.id not in participants:
                ctx.event(person.id, "combat_witness", attackers[0], detail)
        for actor in participants:
            person = ctx.world.people[actor]
            if not person.is_alive:
                ctx.die(person, "combat", casualties=casualties[actor])
                if ctx.trace is not None:
                    deaths.append(actor)
        return won

    def _rout_exit(self, actor, kind, place, target):
        ctx = self.ctx
        origin = ctx.world.people[actor].place
        avoid = ({place, target} - {origin}) if kind == "path" else set()
        candidates = [
            dest
            for dest in ctx.world.places[origin].paths
            if dest not in avoid
            and not self.gates.effective_blockers(self.gates.edge_guards(origin, dest), actor, ctx.start[actor])
        ]
        if not candidates:
            return None
        pick = sample(f"rout/{ctx.world.seed}/{ctx.tick}/{actor}")
        return candidates[min(len(candidates) - 1, int(pick * len(candidates)))]
