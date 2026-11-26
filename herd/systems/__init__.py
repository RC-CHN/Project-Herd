"""Coordinate a round in deterministic physical order."""

from ..model.intent import encode
from .clock import NaturalClock
from .combat import CombatSystem
from .comm import CommSystem
from .context import RoundContext
from .documents import DocumentSystem
from .economy import EconomySystem


def _apply_roe(ctx, intents):
    modes = ctx.rules["force_modes"]
    for actor in ctx.actors:
        mode = intents[actor].roe
        if not mode:
            continue
        person = ctx.world.people[actor]
        if mode not in modes:
            ctx.result(actor, "failed: invalid roe mode")
            continue
        if person.roe != mode:
            old = person.roe
            person.roe = mode
            room = ctx.world.places[person.place]
            room.last_summary["roe_changes"].append({"actor": actor, "from": old, "to": mode})
            if ctx.trace is not None:
                ctx.record("roe", actor=actor, place=person.place, **{"from": old, "to": mode})
            ctx.result(actor, f"roe set to {mode}")


def settle_comm(world, intents, *, trace=None):
    """Deliver the opening deliberation's speech and whispers inside the same tick.

    Only acquaintance links change; positions, items, postures and the public
    room summaries observed at the opening stay untouched until the action barrier.
    """
    summaries = {pid: room.last_summary for pid, room in world.places.items()}
    ctx = RoundContext(world, intents, trace)
    CommSystem(world, phase="comm").run(ctx)
    for pid, summary in summaries.items():
        world.places[pid].last_summary = summary
    return ctx.events


def settle(world, intents, *, trace=None):
    ctx = RoundContext(world, intents, trace)
    _apply_roe(ctx, intents)
    meeting = CommSystem(world)
    gates = meeting.gates
    documents = DocumentSystem(ctx)

    meeting.run(ctx)
    gates.register(ctx, meeting.present)
    CombatSystem(ctx, gates).run()
    gates.peaceful_moves(ctx)
    EconomySystem(ctx, documents, gates).run()
    documents.run()
    NaturalClock(ctx).run(gates, documents)
    for actor in ctx.actors:
        intent = intents[actor]
        if intent.note_key:
            ctx.event(actor, "private_note", actor, encode([intent.note_key, intent.note]))
    return ctx.events
