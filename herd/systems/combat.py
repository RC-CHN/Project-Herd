"""Resolve contested path and person actions, and deliver local physical evidence."""

from collections import defaultdict

from .context import EXHAUSTED_FAILURE, adjust, sample


class CombatSystem:
    def __init__(self, ctx, gates):
        self.ctx = ctx
        self.gates = gates

    def run(self):
        ctx = self.ctx
        paths, items_teams, persons = defaultdict(list), defaultdict(list), defaultdict(list)
        for actor, intent in ctx.actions("force"):
            if ctx.is_exhausted(actor):
                ctx.result(actor, EXHAUSTED_FAILURE)
                continue
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
        unrestricted_modes = [k for k, v in modes.items() if not v.get("requires_lethal_arms")]
        unarmed_cap = (
            max(unrestricted_modes, key=lambda k: (modes[k]["severity"], k))
            if unrestricted_modes
            else default_mode
        )

        def actor_mode(actor):
            m = ctx.world.people[actor].roe
            if m not in modes:
                m = default_mode
            if modes[m].get("requires_lethal_arms") and not ctx.world.people[actor].lethal_armed(
                ctx.items, ctx.start[actor]
            ):
                return unarmed_cap
            return m

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
            hits = {}
            expected_cas = {}
            for actor in side:
                person = ctx.world.people[actor]
                hit_roll = 0.75 + 0.50 * sample(
                    f"combat_hit/{ctx.world.seed}/{ctx.tick}/{place}/{scope}/{actor}"
                )
                hit = (
                    round(base_hit * hit_roll)
                    if side_won or not base_hit
                    else max(1, round(base_hit * person.loss_damage_multiplier(rout_shock) * hit_roll))
                )
                hits[actor] = hit
                if lethal and hit > 0 and person.is_collective and casualty_rate > 0 and person.size > 0:
                    ratio = min(1.0, hit / ctx.rules["max_health"])
                    expected_cas[actor] = person.size * ratio * casualty_rate
            allocated_cas = self._allocate_side_casualties(expected_cas, hits, place, scope)
            for actor in side:
                person = ctx.world.people[actor]
                hit = hits[actor]
                floor_hp = 0 if lethal else 1
                person.health = max(floor_hp, person.health - hit)
                person.energy = max(0 if lethal else 1, person.energy - 1)
                fatal_zero = ctx.rules.get("zero_energy_mode", "fatal") == "fatal"
                if lethal and person.energy == 0:
                    if fatal_zero:
                        person.health = 0
                    elif person.is_alive and ctx.start[actor].energy > 0:
                        ctx.enter_exhaustion(person)
                lost_pop = 0
                if lethal and hit > 0:
                    if person.is_collective:
                        lost_pop = min(person.size, allocated_cas.get(actor, 0))
                        person.size -= lost_pop
                        if lost_pop > 0:
                            ctx.leave_body(person.place, f"body_t{ctx.tick}_{person.id}", person, "combat", lost_pop)
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
                        who = f"{actor}({person.tag})"
                        notice = (
                            f"【目击溃退】{who} suffered {lost_pop} casualties and routed from {old_place} to {dest} (remaining {person.size})"
                            if lost_pop
                            else f"【目击溃退】{who} routed from {old_place} to {dest} (remaining {person.size})"
                        )
                        for witness in ctx.world.people.values():
                            if witness.is_alive and witness.id != actor and witness.place in (old_place, dest):
                                ctx.event(witness.id, "notice", "world", notice)
        visible_places = {place, target} if kind == "path" else {place}
        total_cas = sum(casualties.values())
        tag_of = lambda a: f"{a}({ctx.world.people[a].tag})"
        detail = (
            f"{','.join(tag_of(a) for a in attackers)} forced {scope} "
            f"({mode_spec['label']}, roe={mode_id} by {','.join(escalators)}) "
            f"against {','.join(tag_of(d) for d in defenders)}: {winner} won"
        )
        if total_cas:
            detail += f", casualties {total_cas}"
        if routed:
            detail += f", routed {','.join(f'{tag_of(a)}->{d}' for a, d in routed.items())}"
        for person in ctx.world.people.values():
            if person.is_alive and person.place in visible_places and person.id not in participants:
                ctx.event(person.id, "combat_witness", attackers[0], detail)
        if lethal:
            sound = mode_spec.get("sound") or f"剧烈的{mode_spec['label']}声响与惨叫"
            adjacent_sources = defaultdict(list)
            for vp in sorted(visible_places):
                for neighbor in ctx.world.places[vp].paths:
                    if neighbor not in visible_places:
                        adjacent_sources[neighbor].append(f"{ctx.world.places[vp].label}({vp})")
            for adj_place, srcs in adjacent_sources.items():
                heard = f"【邻室动静】从 {'、'.join(srcs)} 方向传来{sound}！"
                for person in ctx.world.people.values():
                    if person.is_alive and person.place == adj_place:
                        ctx.event(person.id, "notice", "world", heard)
        for actor in participants:
            person = ctx.world.people[actor]
            if not person.is_alive:
                ctx.die(person, "combat", casualties=casualties[actor])
                if ctx.trace is not None:
                    deaths.append(actor)
        return won

    def _allocate_side_casualties(self, expected_cas, hits, place, scope):
        """Conserve a side's total collective casualty pool while distributing it with random fire concentration."""
        if not expected_cas:
            return {}
        ctx = self.ctx
        if len(expected_cas) == 1:
            actor, exp_val = next(iter(expected_cas.items()))
            roll = 0.80 + 0.40 * sample(f"combat_cas/{ctx.world.seed}/{ctx.tick}/{place}/{scope}/{actor}")
            raw = round(exp_val * roll)
            if hits[actor] >= 2:
                raw = max(1, raw)
            return {actor: min(ctx.world.people[actor].size, raw)}
        max_cap = sum(ctx.world.people[a].size for a in expected_cas)
        pool = round(sum(expected_cas.values()))
        if pool == 0 and any(hits[a] >= 2 for a in expected_cas):
            pool = 1
        pool = min(pool, max_cap)
        if pool <= 0:
            return {a: 0 for a in expected_cas}
        weights = {
            a: exp_val * (0.40 + 1.20 * sample(f"combat_cas/{ctx.world.seed}/{ctx.tick}/{place}/{scope}/{a}"))
            for a, exp_val in expected_cas.items()
        }
        alloc = {a: 0 for a in expected_cas}
        remaining = pool
        while remaining > 0:
            open_actors = [a for a in expected_cas if alloc[a] < ctx.world.people[a].size]
            if not open_actors:
                break
            total_w = sum(weights[a] for a in open_actors)
            if total_w <= 0:
                total_w = float(len(open_actors))
                shares = {a: remaining / total_w for a in open_actors}
            else:
                shares = {a: remaining * (weights[a] / total_w) for a in open_actors}
            added = 0
            remainders = []
            for a in open_actors:
                cap_left = ctx.world.people[a].size - alloc[a]
                whole = min(cap_left, int(shares[a]))
                alloc[a] += whole
                added += whole
                if alloc[a] < ctx.world.people[a].size:
                    remainders.append((shares[a] - int(shares[a]), weights[a], a))
            remaining -= added
            if remaining > 0:
                remainders.sort(reverse=True)
                progressed = False
                for _, _, a in remainders:
                    if remaining <= 0:
                        break
                    if alloc[a] < ctx.world.people[a].size:
                        alloc[a] += 1
                        remaining -= 1
                        progressed = True
                if not progressed:
                    break
        return alloc

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
