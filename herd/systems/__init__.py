"""Coordinate a round in deterministic physical order."""

from ..model.intent import encode
from .clock import NaturalClock
from .combat import CombatSystem
from .comm import CommSystem
from .context import RoundContext
from .documents import DocumentSystem
from .economy import EconomySystem


def settle(world, intents, *, trace=None):
    ctx = RoundContext(world, intents, trace)
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
