from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import RLock
import zlib

from ...model import Intent, World, encode
from ..memory import ResidentAgent
from .notes import apply_note_ops, extra_note_events, note_ops_for
from .replay import replay
from .schema import COMM_SCHEMA, PREFIX_SCHEMA, RECALL_KINDS, SCHEMA, frame_events


class Store:
    def __init__(self, path):
        self._lock = RLock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(PREFIX_SCHEMA)
        self.db.executescript(COMM_SCHEMA)

    @classmethod
    def create(cls, path, count=None, seed=7, personas=None, scenario=None):
        world = World.create(scenario, count, seed)
        personas = {} if personas is None else personas
        if not isinstance(personas, dict) or set(personas) - world.people.keys():
            raise ValueError("personas must map known resident IDs to personality descriptions")
        if not all(isinstance(v, str) and len(v) <= 1000 for v in personas.values()):
            raise ValueError("each persona must be a string of up to 1000 characters")
        assigned = {**world.personas(), **personas}
        # Exclusive creation protects existing runs, including concurrent init commands.
        with Path(path).open("xb"):
            pass
        store = cls(path)
        with store._lock, store.db:
            store.db.executescript(SCHEMA)
            store.db.execute("INSERT INTO world VALUES (1,0,?)", (encode(world.to_dict()),))
            store.db.executemany(
                "INSERT INTO agents(id,persona) VALUES (?,?)",
                [(p, assigned[p]) for p in world.people],
            )
        return store

    def world(self):
        with self._lock:
            return World.from_dict(json.loads(self.db.execute("SELECT data FROM world").fetchone()[0]))

    @contextmanager
    def read_snapshot(self):
        with self._lock:
            if self.db.in_transaction:
                yield
            else:
                with self.db:
                    self.db.execute("BEGIN")
                    yield

    def agents(self):
        with self._lock:
            return [
                ResidentAgent(*row) for row in self.db.execute("SELECT id,persona FROM agents ORDER BY id")
            ]

    def mind(self, actor):
        with self._lock:
            row = self.db.execute("SELECT cut,digest,notes FROM agents WHERE id=?", (actor,)).fetchone()
            return {"cut": row[0], "digest": json.loads(row[1]), "notes": json.loads(row[2])}

    def history(self, actor, cut, tick):
        with self._lock:
            return self.db.execute(
                "SELECT tick,frame,reply FROM turns WHERE actor=? AND tick>? AND tick<? ORDER BY tick",
                (actor, cut, tick),
            ).fetchall()

    def events(self, viewer, start, end):
        with self._lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT tick,kind,source,detail FROM events WHERE viewer=? AND tick BETWEEN ? AND ? ORDER BY tick,id",
                    (viewer, start, end),
                )
            ]

    def delivered_events(self, actor, before_tick):
        """Return only events present in this resident's earlier archived inputs, in order."""
        with self._lock:
            rows = self.db.execute(
                "SELECT frame FROM turns WHERE actor=? AND tick<? ORDER BY tick",
                (actor, before_tick),
            )
            return [event for row in rows for event in frame_events(json.loads(row["frame"]))]

    def recall(self, viewer, peers, cut):
        with self._lock:
            if cut < 0 or not peers:
                return []
            near = {p["id"] if isinstance(p, dict) else p for p in peers}
            recalled = []
            rows = self.db.execute(
                "SELECT frame FROM turns WHERE actor=? AND tick<=? ORDER BY tick DESC", (viewer, cut)
            )
            for row in rows:
                for event in reversed(frame_events(json.loads(row["frame"]))):
                    if event["source"] in near and event["kind"] in RECALL_KINDS:
                        recalled.append(event)
                        if len(recalled) == 3:
                            return recalled[::-1]
            return recalled[::-1]

    def staged(self, tick):
        with self._lock:
            return {r[0] for r in self.db.execute("SELECT actor FROM turns WHERE tick=?", (tick,))}

    def prefix_history(self, scope, capacity):
        with self._lock:
            rows = self.db.execute(
                "SELECT value FROM recent_prefixes WHERE scope=? ORDER BY id DESC LIMIT ?",
                (scope, capacity),
            ).fetchall()
            history = []
            for (value,) in reversed(rows):
                raw = zlib.decompress(value[1:])
                history.append(raw if value[:1] == b"B" else tuple(json.loads(raw)))
            return history

    def stage(self, tick, request, reply, metrics, scope, key, capacity):
        # Validate before reserving the resident's slot. A malformed note ledger
        # cannot otherwise be retried once every decision has been staged.
        intent = Intent.parse(reply, request.actor)
        note_ops_for(intent, metrics)
        packed = self._pack(key)
        with self._lock, self.db:
            staged = self.db.execute(
                """INSERT OR IGNORE INTO turns
                SELECT ?,?,?,?,?,?,?,? WHERE (SELECT tick FROM world)=?""",
                (
                    request.actor,
                    tick,
                    encode(request.frame),
                    reply,
                    request.cut,
                    encode(request.digest),
                    request.compressed,
                    encode(metrics),
                    tick,
                ),
            )
            if staged.rowcount:
                self._remember_prefix(scope, packed, capacity)
            return bool(staged.rowcount)

    def _remember_prefix(self, scope, packed, capacity):
        self.db.execute("INSERT INTO recent_prefixes(scope,value) VALUES(?,?)", (scope, packed))
        self.db.execute(
            """DELETE FROM recent_prefixes WHERE scope=? AND id NOT IN
            (SELECT id FROM recent_prefixes WHERE scope=? ORDER BY id DESC LIMIT ?)""",
            (scope, scope, capacity),
        )

    @staticmethod
    def _pack(key):
        return (
            b"B" + zlib.compress(key)
            if isinstance(key, bytes)
            else b"T" + zlib.compress(encode(key).encode())
        )

    # ---- Two-phase ticks: opening deliberation barrier -------------------------------

    def comm_staged(self, tick):
        with self._lock:
            return {r[0] for r in self.db.execute("SELECT actor FROM comm_turns WHERE tick=?", (tick,))}

    def comm_settled(self, tick):
        with self._lock:
            return self.db.execute("SELECT 1 FROM comm_ticks WHERE tick=?", (tick,)).fetchone() is not None

    def comm_turn(self, actor, tick):
        with self._lock:
            row = self.db.execute(
                "SELECT frame,reply,cut,digest,compressed,metrics FROM comm_turns WHERE actor=? AND tick=?",
                (actor, tick),
            ).fetchone()
            if row is None:
                return None
            heard = [
                dict(r)
                for r in self.db.execute(
                    "SELECT tick,kind,source,detail FROM comm_events WHERE viewer=? AND tick=? ORDER BY id",
                    (actor, tick),
                )
            ]
            metrics = json.loads(row["metrics"])
            return dict(
                frame=json.loads(row["frame"]),
                reply=row["reply"],
                cut=row["cut"],
                digest=json.loads(row["digest"]),
                compressed=row["compressed"],
                note_ops=metrics.get("note_ops", []),
                heard=heard,
            )

    def stage_comm(self, tick, request, reply, metrics, scope, key, capacity):
        intent = Intent.parse(reply, request.actor)
        if intent.action != "wait" or intent.roe or intent.note_key:
            raise ValueError("deliberation replies carry only speech and whispers")
        note_ops_for(intent, metrics)
        packed = self._pack(key)
        with self._lock, self.db:
            staged = self.db.execute(
                """INSERT OR IGNORE INTO comm_turns
                SELECT ?,?,?,?,?,?,?,? WHERE (SELECT tick FROM world)=?
                AND NOT EXISTS (SELECT 1 FROM comm_ticks WHERE tick=?)""",
                (
                    request.actor,
                    tick,
                    encode(request.frame),
                    reply,
                    request.cut,
                    encode(request.digest),
                    request.compressed,
                    encode(metrics),
                    tick,
                    tick,
                ),
            )
            if staged.rowcount:
                self._remember_prefix(scope, packed, capacity)
            return bool(staged.rowcount)

    def settle_comm(self, tick):
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            world = self.world()
            if world.tick != tick or self.comm_settled(tick):
                return False
            rows = self.db.execute("SELECT actor,reply FROM comm_turns WHERE tick=? ORDER BY actor", (tick,)).fetchall()
            intents = {r["actor"]: Intent.parse(r["reply"], r["actor"]) for r in rows}
            if set(intents) != set(world.active_residents()):
                raise RuntimeError("missing resident deliberations")
            events = world.settle_comm(intents)
            self.db.executemany(
                "INSERT INTO comm_events(viewer,tick,kind,source,detail) VALUES(?,?,?,?,?)", events
            )
            self.db.execute("INSERT INTO comm_ticks(tick) VALUES(?)", (tick,))
            self.db.execute("UPDATE world SET data=? WHERE id=1", (encode(world.to_dict()),))
        return True

    def advance(self, tick):
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            world = self.world()
            if world.tick != tick:
                return False
            rows = self.db.execute("SELECT * FROM turns WHERE tick=? ORDER BY actor", (tick,)).fetchall()
            intents = {r["actor"]: Intent.parse(r["reply"], r["actor"]) for r in rows}
            if set(intents) != set(world.active_residents()):
                raise RuntimeError("missing resident decisions")
            metrics = {r["actor"]: json.loads(r["metrics"]) for r in rows}
            events = world.settle(intents)
            events.extend(extra_note_events(tick, intents, metrics))
            self.db.executemany(
                "INSERT INTO events(viewer,tick,kind,source,detail) VALUES(?,?,?,?,?)",
                events,
            )
            for row in rows:
                actor = row["actor"]
                notes = self.mind(actor)["notes"]
                apply_note_ops(notes, note_ops_for(intents[actor], metrics[actor]), tick)
                self.db.execute(
                    """UPDATE agents SET cut=?,digest=?,notes=?,compressions=compressions+?
                    WHERE id=?""",
                    (row["cut"], row["digest"], encode(notes), row["compressed"], actor),
                )
            self.db.execute(
                "UPDATE world SET tick=?,data=? WHERE id=1", (world.tick, encode(world.to_dict()))
            )
        return True

    def fork(self, destination):
        with Path(destination).open("xb"):
            pass
        copy = sqlite3.connect(destination)
        try:
            with self._lock:
                self.db.backup(copy)
        finally:
            copy.close()

    def replay(self):
        with self.read_snapshot():
            return replay(self.db)

    def stats(self):
        with self.read_snapshot():
            tick = self.db.execute("SELECT tick FROM world").fetchone()[0]
            residents, folds = self.db.execute("SELECT count(*),sum(compressions) FROM agents").fetchone()
            result = {
                "tick": tick,
                "residents": residents,
                "compressions": folds,
                "pending": len(self.staged(tick)),
                "decisions": 0,
                "server_cache_reports": 0,
                "server_cached_tokens": 0,
                "prompt_tokens": 0,
                "loop_steps": 0,
                "matched_prefix_bytes": 0,
                "matched_prefix_tokens": 0,
                "by_profile": {},
                "by_model": {},
            }
            rows = [
                (actor, raw, False)
                for actor, raw in self.db.execute(
                    "SELECT actor,metrics FROM turns WHERE tick<? ORDER BY tick,actor", (tick,)
                )
            ] + [
                (actor, raw, True)
                for actor, raw in self.db.execute(
                    "SELECT actor,metrics FROM comm_turns WHERE tick<? ORDER BY tick,actor", (tick,)
                )
            ]
            result["comm_decisions"] = 0
            for actor, raw, deliberation in rows:
                metric = json.loads(raw)
                if deliberation:
                    result["comm_decisions"] += 1
                    result["loop_steps"] += metric.get("loop_steps", 0)
                    result["prompt_tokens"] += metric.get("prompt_tokens") or 0
                    if metric.get("cached_tokens") is not None:
                        result["server_cache_reports"] += 1
                        result["server_cached_tokens"] += metric["cached_tokens"]
                    for field in ("profile", "model"):
                        bucket = result["by_" + field].setdefault(
                            metric.get(field) or "unknown",
                            dict(decisions=0, prompt_tokens=0, cached_tokens=0, loop_steps=0, actors=set()),
                        )
                        bucket["loop_steps"] += metric.get("loop_steps", 0)
                        bucket["prompt_tokens"] += metric.get("prompt_tokens") or 0
                        bucket["cached_tokens"] += metric.get("cached_tokens") or 0
                    continue
                for field in ("profile", "model"):
                    label = metric.get(field) or "unknown"
                    bucket = result["by_" + field].setdefault(
                        label,
                        dict(decisions=0, prompt_tokens=0, cached_tokens=0, loop_steps=0, actors=set()),
                    )
                    bucket["actors"].add(actor)
                    bucket["decisions"] += 1
                    bucket["loop_steps"] += metric.get("loop_steps", 0)
                    bucket["prompt_tokens"] += metric.get("prompt_tokens") or 0
                    bucket["cached_tokens"] += metric.get("cached_tokens") or 0
                result["decisions"] += 1
                result["loop_steps"] += metric.get("loop_steps", 0)
                result["prompt_tokens"] += metric.get("prompt_tokens") or 0
                for field in ("matched_prefix_bytes", "matched_prefix_tokens"):
                    result[field] += metric.get(field, 0)
                if metric.get("cached_tokens") is not None:
                    result["server_cache_reports"] += 1
                    result["server_cached_tokens"] += metric["cached_tokens"]
            for groups in (result["by_profile"], result["by_model"]):
                for group in groups.values():
                    group["actors"] = sorted(group["actors"])
            result.update(self.world().statistics())
            return result
