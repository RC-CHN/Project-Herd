"""Verify a consistent SQLite snapshot using only the frozen scenario and turn ledger."""

import json

from ...model import Intent, World
from .notes import apply_note_ops, extra_note_events, note_ops_for
from .schema import has_comm


def same_json(left, right):
    """Compare archived JSON values without equating true/false with 1/0."""
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def replay(db):
    saved_tick, data = db.execute("SELECT tick,data FROM world").fetchone()
    archived = json.loads(data)
    if type(archived.get("tick")) is not int or archived["tick"] != saved_tick or saved_tick < 0:
        raise ValueError("world tick does not match archive")
    outside_turns = db.execute(
        "SELECT 1 FROM turns WHERE typeof(tick) != 'integer' OR tick<0 OR tick>? LIMIT 1",
        (saved_tick,),
    ).fetchone()
    outside_events = db.execute(
        "SELECT 1 FROM events WHERE typeof(tick) != 'integer' OR tick<0 OR tick>=? LIMIT 1",
        (saved_tick,),
    ).fetchone()
    comm = has_comm(db)
    if comm:
        outside_turns = outside_turns or db.execute(
            "SELECT 1 FROM comm_turns WHERE typeof(tick) != 'integer' OR tick<0 OR tick>? LIMIT 1",
            (saved_tick,),
        ).fetchone()
        outside_events = outside_events or db.execute(
            """SELECT 1 FROM comm_events WHERE typeof(tick) != 'integer' OR tick<0 OR tick>?
            OR tick NOT IN (SELECT tick FROM comm_ticks) LIMIT 1""",
            (saved_tick,),
        ).fetchone() or db.execute(
            "SELECT 1 FROM comm_ticks WHERE typeof(tick) != 'integer' OR tick<0 OR tick>? LIMIT 1",
            (saved_tick,),
        ).fetchone()
    if outside_turns or outside_events:
        raise ValueError("records outside replay interval")
    world = World.create(archived["spec"], len(archived["people"]), archived["seed"])
    notes = {actor: {} for actor in world.people}
    for tick in range(saved_tick + 1):
        if comm:
            replay_comm(db, world, tick)
        if tick == saved_tick:
            break
        rows = db.execute(
            "SELECT actor,reply,metrics FROM turns WHERE tick=? ORDER BY actor", (tick,)
        ).fetchall()
        intents = {actor: Intent.parse(reply, actor) for actor, reply, _ in rows}
        metrics = {actor: json.loads(raw) for actor, _, raw in rows}
        if set(intents) != set(world.active_residents()):
            raise ValueError(f"missing or extra resident at tick {tick}")
        events = world.settle(intents)
        events.extend(extra_note_events(tick, intents, metrics))
        for actor, intent in intents.items():
            apply_note_ops(notes[actor], note_ops_for(intent, metrics[actor]), tick)
        recorded = db.execute(
            """SELECT viewer,tick,kind,source,detail
            FROM events WHERE tick=? ORDER BY id""",
            (tick,),
        )
        if events != [tuple(row) for row in recorded]:
            raise ValueError(f"observation mismatch at tick {tick}")
    if not same_json(world.to_dict(), archived):
        raise ValueError("world state does not match replay")
    saved_notes = {actor: json.loads(raw) for actor, raw in db.execute("SELECT id,notes FROM agents")}
    if not same_json(notes, saved_notes):
        raise ValueError("private notes do not match replay")
    return {"verified_ticks": saved_tick, "residents": len(world.people)}


def replay_comm(db, world, tick):
    """Re-deliver one committed opening deliberation and compare its same-tick observations."""
    if not db.execute("SELECT 1 FROM comm_ticks WHERE tick=?", (tick,)).fetchone():
        if db.execute("SELECT 1 FROM turns WHERE tick=? LIMIT 1", (tick,)).fetchone() and world.spec[
            "rules"
        ].get("comm_rounds", 0):
            raise ValueError(f"action decisions before the deliberation barrier at tick {tick}")
        return []
    rows = db.execute("SELECT actor,reply FROM comm_turns WHERE tick=? ORDER BY actor", (tick,)).fetchall()
    intents = {actor: Intent.parse(reply, actor) for actor, reply in rows}
    if set(intents) != set(world.active_residents()):
        raise ValueError(f"missing or extra deliberation at tick {tick}")
    events = world.settle_comm(intents)
    recorded = db.execute(
        "SELECT viewer,tick,kind,source,detail FROM comm_events WHERE tick=? ORDER BY id", (tick,)
    )
    if events != [tuple(row) for row in recorded]:
        raise ValueError(f"deliberation mismatch at tick {tick}")
    return events
