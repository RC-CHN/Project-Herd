"""Replay a frozen polity run, verify resident evidence and export IEMP measurements."""

import argparse
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile

from herd.model import World, ENGINE_POLITY, Intent, encode
from herd.runtime.storage.notes import extra_note_events
from herd.runtime.storage.schema import has_comm
from herd.runtime.storage.replay import same_json
from .audit import Audit, BackendAttribution
from .audit.provenance import check
from .metrics import Spread, iemp


def snapshot(world):
    return dict(
        tick=world.tick,
        energy_decay=world.energy_decay,
        places={
            k: {
                "items": p.items,
                "docs": p.docs,
                "board": p.board,
                "regen": p.regen,
                "comms": p.comms,
                "recent_room": p.last_summary,
            }
            for k, p in world.places.items()
        },
        people={
            k: {
                "place": p.place,
                "size": p.size,
                "roe": p.roe,
                "energy": p.energy,
                "health": p.health,
                "inv": p.inv,
                "docs": p.docs,
                "posture": p.posture.to_dict() if p.posture else None,
            }
            for k, p in world.people.items()
        },
        documents={k: doc.to_dict() for k, doc in world.documents.items()},
    )


def replay(db, output):
    saved_tick, raw = db.execute("SELECT tick,data FROM world").fetchone()
    comm_ledger = has_comm(db)
    saved = json.loads(raw)
    check(type(saved_tick) is int and saved_tick >= 0 and type(saved["tick"]) is int, "invalid world tick")
    check(saved["tick"] == saved_tick and saved.get("scenario") == ENGINE_POLITY, "expected polity world")
    archived = World.from_dict(saved)
    w = World.create(archived.spec, len(archived.people), archived.seed)
    # Vary only private bodies in an independent replay. Any change to an
    # unrelated event demonstrates a leak, even in a result or a notice.
    shadow = World.create(archived.spec, len(archived.people), archived.seed)
    audit, spread = Audit(), Spread()
    attribution = BackendAttribution()
    timeline, focus = [], []
    markers = w.spec.get("analysis_markers")
    # Include already staged frames in the audit without committing a partial tick.
    for tick in range(saved_tick + 1):
        opening = {a: w.people[a].place for a in w.active_residents()}
        opening_comms = {
            a: (
                w.people[a].can_remote_comm(w.places[w.people[a].place].comms, w.all_items()),
                set(w.people[a].contacts),
            )
            for a in opening
        }
        index = w.build_index()
        intents, decisions, operations, frames = {}, {}, {}, {}
        for row in db.execute("SELECT * FROM turns WHERE tick=? ORDER BY actor", (tick,)):
            a = row["actor"]
            check(a in opening, f"{a}/{tick}: inactive resident")
            decisions[a] = json.loads(row["metrics"])
            frame, intents[a], operations[a] = audit.frame(row, w, index, decisions[a])
            frames[a] = frame
            spread.delivered(a, frame)
        comm_events = None
        if comm_ledger and db.execute("SELECT 1 FROM comm_ticks WHERE tick=?", (tick,)).fetchone():
            comm_intents = {
                a: Intent.parse(reply, a)
                for a, reply in db.execute("SELECT actor,reply FROM comm_turns WHERE tick=? ORDER BY actor", (tick,))
            }
            check(comm_intents.keys() == opening.keys(), f"missing deliberations at {tick}")
            comm_events = w.settle_comm(comm_intents)
            recorded_comm = [
                tuple(row)
                for row in db.execute(
                    "SELECT viewer,tick,kind,source,detail FROM comm_events WHERE tick=? ORDER BY id", (tick,)
                )
            ]
            check(comm_events == recorded_comm, f"deliberation replay mismatch at {tick}")
            audit.whispers(comm_intents, opening, recorded_comm, opening_comms)
            probe = {
                a: replace(i, whisper=("x" if i.whisper != "x" else "y")) if i.whisper else i
                for a, i in comm_intents.items()
            }
            shadow.settle_comm(probe)
            check(shadow == w, f"deliberation whisper content affected third-party state at {tick}")
            # Same-tick acquaintances may extend remote reach for the action phase.
            opening_comms = {
                a: (
                    w.people[a].can_remote_comm(w.places[w.people[a].place].comms, w.all_items()),
                    set(w.people[a].contacts),
                )
                for a in opening
            }
        for a, frame in frames.items():
            if comm_events is not None or "comm" in frame:
                audit.heard(frame, a, tick, comm_events or [])
        if tick == saved_tick:
            break
        check(intents.keys() == opening.keys(), f"missing decisions at {tick}")
        trace = []
        events = w.settle(intents, trace=trace)
        note_events = extra_note_events(tick, intents, decisions)
        events.extend(note_events)
        recorded = [
            tuple(row)
            for row in db.execute(
                "SELECT viewer,tick,kind,source,detail FROM events WHERE tick=? ORDER BY id", (tick,)
            )
        ]
        audit.whispers(intents, opening, recorded, opening_comms)
        check(events == recorded, f"event replay mismatch at {tick}")
        audit.verify_whisper_isolation(shadow, w, intents, opening, events, note_events, tick, opening_comms)
        audit.close_tick(operations, events, tick)
        attribution.record_tick(w, decisions, trace)
        political = iemp.political(w)
        political.update(spread.measure(w, trace, intents))
        timeline.append(
            dict(
                tick=tick,
                I=iemp.intellectual(w, trace),
                E=iemp.economic(w, trace),
                M=iemp.military(w, trace),
                P=political,
            )
        )
        if markers:
            focus.append(iemp.focal(w, political, markers))
        if output is not None:
            output.write(encode({"tick": tick, "snapshot": snapshot(w), "events": trace}) + "\n")
    check(same_json(w.to_dict(), saved), "final world mismatch")
    audit.verify_notes(db.execute("SELECT id,notes FROM agents"), w.people)
    check(
        not db.execute(
            "SELECT 1 FROM turns WHERE typeof(tick) != 'integer' OR tick<0 OR tick>? LIMIT 1",
            (saved_tick,),
        ).fetchone()
        and not db.execute(
            "SELECT 1 FROM events WHERE typeof(tick) != 'integer' OR tick<0 OR tick>=? LIMIT 1",
            (saved_tick,),
        ).fetchone(),
        "records outside replay interval",
    )
    result = dict(
        scenario=w.spec["id"],
        engine=w.scenario,
        seed=w.seed,
        completed_rounds=saved_tick,
        audit=dict(
            replay="passed",
            observation_provenance="passed",
            whisper_privacy="passed",
            private_notes="passed",
            verified_ticks=saved_tick,
            **audit.counts,
        ),
        iemp_timeline=timeline,
        backend_attribution=attribution.summary(w),
    )
    if markers:
        result["focal_summary"] = focus
    return result


def report(db, export_trace=None):
    original_factory = db.row_factory
    db.row_factory = sqlite3.Row
    own_transaction = not db.in_transaction
    tmp = None
    try:
        if own_transaction:
            db.execute("BEGIN")
        if export_trace is None:
            return replay(db, None)
        dest = Path(export_trace)
        databases = {Path(r["file"]).resolve() for r in db.execute("PRAGMA database_list") if r["file"]}
        sqlite_paths = {
            Path(str(db_path) + suffix)
            for db_path in databases
            for suffix in ("", "-wal", "-shm", "-journal")
        }
        check(dest.resolve() not in sqlite_paths, "trace must have its own path")
        with NamedTemporaryFile("w", encoding="utf-8", dir=dest.parent, delete=False) as stream:
            tmp = Path(stream.name)
            result = replay(db, stream)
        tmp.replace(dest)
        return result
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        if own_transaction:
            db.rollback()
        db.row_factory = original_factory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--export-trace", type=Path)
    args = parser.parse_args()
    with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        print(json.dumps(report(conn, args.export_trace), ensure_ascii=False, indent=2))
