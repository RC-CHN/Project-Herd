"""Opening huddles, local speech and deterministic small-circle whispers."""

from collections import defaultdict
import random

from ..model.time import diurnal
from .context import sample
from .gates import GateSystem


class CommSystem:
    def __init__(self, world):
        self.present = defaultdict(list)
        self.huddles = {}
        self.gates = GateSystem(world)
        contacts = {a: set(p.contacts) for a, p in world.people.items() if p.is_alive}
        for actor in sorted(contacts):
            self.present[world.people[actor].place].append(actor)
        size = world.spec["rules"]["contact_size"]
        for place, room in self.present.items():
            pool = room.copy()
            if len(pool) > size:
                random.Random(f"huddle/{world.seed}/{world.tick}/{place}").shuffle(pool)
            while pool:
                anchor, *rest = pool
                mutual, single = [], []
                for peer in rest:
                    outgoing, incoming = peer in contacts[anchor], anchor in contacts[peer]
                    if outgoing and incoming:
                        mutual.append(peer)
                    elif outgoing or incoming:
                        single.append(peer)
                friends = (mutual + single)[: max(0, size - 2)]
                selected = set(friends)
                remaining = [a for a in rest if a not in selected]
                slots = size - len(friends) - 1
                table = [anchor, *friends, *remaining[:slots]]
                for actor in table:
                    self.huddles[actor] = table
                pool = remaining[slots:]

    def run(self, ctx):
        for actor in ctx.actors:
            intent = ctx.intents[actor]
            place = ctx.start[actor].place
            if intent.speech:
                table = self.huddles[actor]
                for viewer in table:
                    ctx.event(viewer, "speech", actor, intent.speech)
                if ctx.trace is not None:
                    ctx.record(
                        "comm", kind="speech", place=place, actor=actor, targets=table, detected_by=[]
                    )
            if intent.whisper and intent.whisper_to:
                self.whisper(ctx, actor, place, intent)

    @staticmethod
    def _has_comms(world, actor, snapshot):
        return world.people[actor].can_remote_comm(
            world.places[snapshot.place].comms, world.all_items(), snapshot
        )

    def whisper(self, ctx, actor, place, intent):
        candidates = [peer.strip() for peer in intent.whisper_to.split(",") if peer.strip()]
        seen = list(dict.fromkeys(candidates))
        sender_comms = self._has_comms(ctx.world, actor, ctx.start[actor])
        sender_contacts = set(ctx.world.people[actor].contacts)
        targets, remote_targets, unreachable, failed = [], [], [], []
        for peer in seen:
            if peer == actor:
                continue
            if ctx.colocated(actor, peer):
                targets.append(peer)
            elif sender_comms and peer in sender_contacts:
                if (
                    peer in ctx.start
                    and ctx.world.people[peer].is_alive
                    and self._has_comms(ctx.world, peer, ctx.start[peer])
                ):
                    targets.append(peer)
                    remote_targets.append(peer)
                else:
                    unreachable.append(peer)
            else:
                if peer not in ctx.world.people or not ctx.world.people[peer].is_alive:
                    failed.append((peer, "not present or inactive"))
                elif not sender_comms:
                    failed.append((peer, "not in your room and no remote comms"))
                else:
                    failed.append((peer, "not in your room and not in contacts"))
            if len(targets) == 4:
                break
        for peer in unreachable:
            ctx.result(actor, f"remote contact {peer} unreachable")
        for peer, reason in failed:
            ctx.result(actor, f"whisper to {peer} failed: {reason}")
        if not targets:
            return
        for peer in targets:
            ctx.event(peer, "whisper", actor, intent.whisper)
            ctx.connect(actor, peer)
        label = ",".join(targets)
        ctx.event(actor, "whisper_sent", label, intent.whisper)
        room = self.present[place]
        ref = ctx.rules["whisper_detect_ref_crowd"]
        base = ctx.rules["whisper_detect_base"] * ref / max(ref, len(room) - 1)
        circle = {actor, *(p for p in targets if p not in remote_targets)}
        if ctx.trace is not None:
            detected = []
        _, _, is_night = diurnal(ctx.tick, ctx.rules["clock_interval"])
        night_factor = ctx.rules.get("night_whisper_multiplier", 1.0) if is_night else 1.0
        for viewer in room:
            if viewer in circle:
                continue
            same = any(self.huddles[viewer] is self.huddles[a] for a in circle)
            chance = base + same * ctx.rules["whisper_same_huddle_bonus"] + 0.05 * (len(targets) - 1)
            if night_factor != 1.0:
                chance *= night_factor
            chance = min(ctx.rules["whisper_detect_max"], max(ctx.rules["whisper_detect_min"], chance))
            if sample(f"whisper/{ctx.world.seed}/{ctx.tick}/{actor}/{label}/{viewer}") < chance:
                ctx.event(viewer, "spotted_whisper", actor, f"privately whispered with {label}")
                if ctx.trace is not None:
                    detected.append(viewer)
        if ctx.trace is not None:
            ctx.record(
                "comm",
                kind="whisper",
                place=place,
                actor=actor,
                targets=targets,
                remote=remote_targets,
                detected_by=detected,
            )
