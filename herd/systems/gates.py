"""Directional gate control, room-item protection, physical permissions and movement."""

from collections import defaultdict

from ..model.entities import GuardPosture
from .context import EXHAUSTED_FAILURE


class GateSystem:
    def __init__(self, world):
        self.world = world
        self._index_postures()

    def _index_postures(self):
        self._directed = defaultdict(list)
        self._escorts = defaultdict(lambda: defaultdict(list))
        self._items = defaultdict(list)
        for actor, person in self.world.people.items():
            posture = person.posture
            if not person.is_alive or not posture:
                continue
            place, target = person.place, posture.scope_target
            if posture.scope_kind == "person":
                self._escorts[place][target].append(actor)
            elif posture.scope_kind == "items":
                self._items[place].append(actor)
            elif target == "*":
                # Full room lockdown: blocks all doorways bidirectionally and protects floor items.
                self._items[place].append(actor)
                for other in self.world.places[place].paths:
                    self._directed[other, place].append(actor)
                    self._directed[place, other].append(actor)
            else:
                # Guarding a specific doorway from inside blocks entry from that neighbor.
                self._directed[target, place].append(actor)

    def edge_guards(self, source, target):
        return [
            a
            for a in self._directed.get((source, target), ())
            if self.world.people[a].is_alive and self.world.people[a].posture
        ]

    def items_guards(self, place):
        return [
            a
            for a in self._items.get(place, ())
            if self.world.people[a].is_alive and self.world.people[a].posture
        ]

    def escorts(self, place, target):
        return [
            a
            for a in self._escorts.get(place, {}).get(target, ())
            if self.world.people[a].is_alive and self.world.people[a].posture
        ]

    def team_strength(self, team, opening=None, *, terrain=False, mode_spec=None):
        world = self.world
        by_disc = defaultdict(float)
        for actor in team:
            person = world.people[actor]
            if person.size <= 0:
                continue
            alpha = 0.3 + 0.6 * person.discipline
            defense = world.places[person.place].defense if terrain else 0.0
            quality = person.combat_quality(
                world.all_items(),
                world.spec["rules"],
                opening[actor] if opening else None,
                mode_spec=mode_spec,
                terrain_defense=defense,
            )
            if quality > 0:
                by_disc[person.discipline] += person.size * (quality ** (1.0 / alpha))
        return sum(vol ** (0.3 + 0.6 * disc) for disc, vol in by_disc.items() if vol > 0)

    def effective_blockers(self, guards, actor, credentials=None):
        credentials = self.world.people[actor] if credentials is None else credentials
        blockers, allowers = [], []
        for guard_id in guards:
            guard = self.world.people[guard_id]
            if not guard.is_alive or not guard.posture:
                continue
            passes = guard.posture.pass_rule.permits(
                guard_id, guard.contacts, actor, credentials, self.world.documents
            )
            (allowers if passes else blockers).append(guard_id)
        return blockers if blockers and self.team_strength(blockers) > self.team_strength(allowers) else []

    def trapped(self, actor):
        person = self.world.people[actor]
        paths = self.world.places[person.place].paths
        return (
            person.is_alive
            and bool(paths)
            and all(
                self.effective_blockers(self.edge_guards(person.place, neighbor), actor, person)
                for neighbor in paths
            )
        )

    def view(self, actor):
        world = self.world
        person = world.people[actor]
        known = set(person.contacts) | {actor}

        modes = world.spec["rules"]["force_modes"]
        default_mode = min(modes, key=lambda k: (modes[k]["severity"], k))

        def guard_roe(g):
            r = world.people[g].roe
            return r if r in modes else default_mode

        def visible(guards):
            return dict(
                count=len(guards),
                total_size=sum(world.people[g].size for g in guards),
                armed_count=sum(world.people[g].armed(world.all_items()) for g in guards),
                max_roe=max((guard_roe(g) for g in guards), key=lambda m: (modes[m]["severity"], m), default=None),
                known_guards=[g for g in guards if g in known],
            )

        paths = {}
        for neighbor in world.places[person.place].paths:
            outbound = self.edge_guards(person.place, neighbor)
            inbound = self.edge_guards(neighbor, person.place)
            all_guards = list(dict.fromkeys((*outbound, *inbound)))
            paths[neighbor] = dict(
                **visible(all_guards),
                blocking_entry=len(inbound),
                blocking_exit=len(outbound),
                passes=sorted({world.people[g].posture.pass_rule.raw or "none" for g in all_guards}),
                blocked_for_self=bool(self.effective_blockers(outbound, actor, person)),
            )
        persons = {}
        for target in self._escorts.get(person.place, {}):
            if guards := self.escorts(person.place, target):
                persons[target] = visible(guards)
        item_guards = self.items_guards(person.place)
        items_view = dict(
            **visible(item_guards),
            passes=sorted({world.people[g].posture.pass_rule.raw or "none" for g in item_guards}),
            blocked_for_self=bool(self.effective_blockers(item_guards, actor, person)),
        )
        return dict(paths=paths, persons=persons, items=items_view)

    def register(self, ctx, present):
        for actor in ctx.order:
            person = self.world.people[actor]
            intent = ctx.intents.get(actor)
            posture = None
            if intent and intent.action == "guard":
                if ctx.is_exhausted(actor):
                    ctx.result(actor, EXHAUSTED_FAILURE)
                else:
                    posture = GuardPosture.try_parse(
                        intent.target,
                        intent.detail,
                        valid_actors=self.world.people.keys(),
                        items=ctx.items,
                        paths=self.world.places[person.place].paths,
                        present=present[person.place],
                    )
                    ctx.result(
                        actor,
                        f"guarding {posture.scope}" if posture else "failed: invalid guard target or pass",
                    )
            if person.posture and (not posture or person.posture.scope != posture.scope):
                ctx.drop_guard(person)
            person.posture = posture
            if posture and ctx.trace is not None:
                ctx.record("guard", actor=actor, place=person.place, **posture.to_dict(), active=True)
        self._index_postures()

    def move(self, ctx, actor, target, blocked):
        person = self.world.people[actor]
        source = person.place
        if ctx.trace is not None:
            ctx.record("move", actor=actor, **{"from": source, "to": target}, blocked_by=blocked)
        if blocked:
            ctx.result(actor, f"blocked on path to {target} by {len(blocked)} guards")
            return
        person.place = target
        if (cost := ctx.rules.get("exertion_energy_cost", 0)) and (
            person.carry_weight(ctx.items) > person.carry_cap(ctx.rules["carry_cap"]) // 2
        ):
            person.energy = max(1, person.energy - cost)
        self.world.places[source].last_summary["left"].append(actor)
        self.world.places[target].last_summary["entered"].append(actor)
        ctx.result(actor, f"moved to {target}")

    def peaceful_moves(self, ctx):
        for actor, intent in ctx.actions("move"):
            if ctx.is_exhausted(actor):
                ctx.result(actor, EXHAUSTED_FAILURE)
                continue
            place = ctx.start[actor].place
            if self.world.people[actor].place != place:
                continue
            if intent.target not in self.world.places[place].paths:
                ctx.result(actor, "failed: unreachable place")
                continue
            guards = self.edge_guards(place, intent.target)
            self.move(ctx, actor, intent.target, self.effective_blockers(guards, actor, ctx.start[actor]))
        self.release_absent_escorts(ctx)

    def release_absent_escorts(self, ctx):
        for actor in ctx.actors:
            person = self.world.people[actor]
            if person.posture and person.posture.scope_kind == "person":
                peer = self.world.people[person.posture.scope_target]
                if not peer.is_alive or peer.place != person.place:
                    ctx.drop_guard(person)
