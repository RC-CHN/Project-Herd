"""Read-only SQLite snapshot reader and incremental deterministic replay cache."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import threading

from ..model import Intent, World
from ..model.time import diurnal
from ..runtime.storage.notes import apply_note_ops, note_ops_for

CN_NUMS = (
    "一",
    "二",
    "三",
    "四",
    "五",
    "六",
    "七",
    "八",
    "九",
    "十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
)


def _open_memory_copy(db_path: Path) -> sqlite3.Connection:
    """Safely snapshot a live SQLite archive into memory without blocking active writers."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        mem = sqlite3.connect(":memory:")
        src.backup(mem)
        mem.row_factory = sqlite3.Row
        return mem
    finally:
        src.close()


def _file_sig(db_path: Path) -> tuple[int, int, int, int]:
    st = db_path.stat()
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.is_file():
        wst = wal.stat()
        return (st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size)
    return (st.st_mtime_ns, st.st_size, 0, 0)


def _quick_header(db_path: Path) -> dict | None:
    """Read minimal metadata for /api/runs listing."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute("SELECT tick, data FROM world WHERE id=1").fetchone()
            if not row:
                return None
            tick, raw = row
            world_data = json.loads(raw)
            spec = world_data["spec"]
            people = world_data["people"]
            alive = sum(
                1
                for p in people.values()
                if p.get("health", 0) > 0 and p.get("energy", 0) > 0 and p.get("size", 1) > 0
            )
            staged = conn.execute("SELECT COUNT(*) FROM turns WHERE tick=?", (tick,)).fetchone()[0]
            wal = db_path.with_name(db_path.name + "-wal")
            mtime = max(db_path.stat().st_mtime, wal.stat().st_mtime if wal.is_file() else 0.0)
            return dict(
                name=db_path.name,
                title=spec.get("title", db_path.stem),
                scenario_id=spec.get("id", "unknown"),
                theme=spec.get("theme", {}),
                tick=tick,
                staged=staged,
                alive=alive,
                residents=len(people),
                mtime=mtime,
            )
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, KeyError, OSError):
        return None


def _build_roles(spec: dict, world: World) -> dict[str, dict]:
    counts = Counter(p.role for p in world.people.values())
    seen = Counter()
    roles = {}
    for pid in sorted(world.people):
        person = world.people[pid]
        group = spec["groups"][person.role]
        seen[person.role] += 1
        label = group["label"]
        if counts[person.role] == 1:
            display = label
        else:
            idx = seen[person.role]
            suffix = CN_NUMS[idx - 1] if idx <= len(CN_NUMS) else str(idx)
            display = f"{label}·{suffix}"
        roles[pid] = dict(
            id=pid,
            role=person.role,
            label=label,
            display=display,
            tag=person.tag,
            initial_size=person.initial_size,
            discipline=person.discipline,
        )
    return roles


def _snapshot_world(world: World, opening_places: dict[str, str] | None = None) -> tuple[dict, dict, dict]:
    people_snap = {}
    for pid, person in world.people.items():
        people_snap[pid] = dict(
            place=person.place,
            from_place=opening_places[pid] if opening_places else person.place,
            hp=person.health,
            en=person.energy,
            size=person.size,
            roe=person.roe,
            alive=person.is_alive,
            exhausted=person.is_exhausted,
            inv=person.inv.copy(),
            docs=person.docs.copy(),
            posture=person.posture.to_dict() if person.posture else None,
            rels={peer: list(tags) for peer, tags in person.relations.items()},
        )
    places_snap = {
        pl_id: dict(
            items=pl.items.copy(),
            docs=pl.docs.copy(),
            board=list(pl.board),
            comms=pl.comms,
        )
        for pl_id, pl in world.places.items()
    }
    docs_snap = {
        did: dict(
            id=did,
            author=doc.author,
            tick=doc.tick,
            seal=doc.seal,
            text=doc.text,
            signers=list(doc.signers),
            signed_weight=sum(world.people[a].size for a in doc.signers if a in world.people),
        )
        for did, doc in world.documents.items()
    }
    return people_snap, places_snap, docs_snap


class RunCache:
    """Incrementally replayed in-memory view of one SQLite simulation archive."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.spec: dict = {}
        self.seed: int = 0
        self.count: int = 0
        self.world: World | None = None
        self.roles: dict[str, dict] = {}
        self.notes_state: dict[str, dict] = {}
        self.ticks: list[dict] = []
        self.tick_summaries: list[dict] = []
        self.opening_tick: dict | None = None
        self.committed_tick: int = 0
        self.staged: int = 0
        self.alive: int = 0
        self.telemetry: dict = {}
        self._last_sig: tuple[int, int] | None = None

    def _init_from_world_data(self, world_data: dict) -> None:
        self.spec = world_data["spec"]
        self.seed = world_data["seed"]
        self.count = len(world_data["people"])
        self.world = World.create(self.spec, self.count, self.seed)
        self.roles = _build_roles(self.spec, self.world)
        self.notes_state = {pid: {} for pid in self.world.people}
        self.ticks = []
        self.tick_summaries = []
        self.committed_tick = 0
        people_snap, places_snap, docs_snap = _snapshot_world(self.world)
        interval = self.spec["rules"]["clock_interval"]
        day, tod, _ = diurnal(0, interval)
        self.opening_tick = dict(
            tick=0,
            day=day,
            slot=0,
            time_of_day=tod,
            opening=True,
            people=people_snap,
            places=places_snap,
            docs=docs_snap,
            dynamic_items={},
            decisions={},
            note_ops={},
            notes_snapshot={},
            trace=[],
            notices=[],
        )

    def refresh(self) -> None:
        with self.lock:
            sig = _file_sig(self.db_path)
            if self._last_sig == sig and self.world is not None:
                return
            mem = _open_memory_copy(self.db_path)
            try:
                row = mem.execute("SELECT tick, data FROM world WHERE id=1").fetchone()
                if not row:
                    raise ValueError(f"empty archive: {self.db_path.name}")
                cur_tick = row["tick"]
                world_data = json.loads(row["data"])
                if self.world is None or cur_tick < self.committed_tick:
                    self._init_from_world_data(world_data)
                assert self.world is not None

                interval = self.spec["rules"]["clock_interval"]
                for t in range(self.committed_tick, cur_tick):
                    rows = mem.execute(
                        "SELECT actor, reply, metrics FROM turns WHERE tick=?", (t,)
                    ).fetchall()
                    intents = {}
                    raw_decisions = {}
                    tick_note_ops = {}
                    for turn_row in rows:
                        actor = turn_row["actor"]
                        parsed = json.loads(turn_row["reply"])
                        metrics = json.loads(turn_row["metrics"]) if turn_row["metrics"] else {}
                        intents[actor] = Intent.parse(turn_row["reply"], actor, None)
                        raw_decisions[actor] = {
                            **parsed,
                            "model": metrics.get("model", ""),
                            "steps": metrics.get("loop_steps", 1),
                            "tool_calls": metrics.get("tool_calls", []),
                        }
                        ops = note_ops_for(intents[actor], metrics)
                        if ops:
                            tick_note_ops[actor] = ops
                            apply_note_ops(self.notes_state[actor], ops, t)

                    opening_places = {pid: p.place for pid, p in self.world.people.items()}
                    trace: list[dict] = []
                    events = self.world.settle(intents, trace=trace)

                    people_snap, places_snap, docs_snap = _snapshot_world(self.world, opening_places)
                    day, tod, _ = diurnal(t, interval)
                    dedup_notices = []
                    seen_notice = set()
                    for _, _, kind, source, detail in events:
                        if kind == "notice":
                            key = (source, detail)
                            if key not in seen_notice:
                                seen_notice.add(key)
                                dedup_notices.append(dict(source=source, detail=detail))

                    combats = [e for e in trace if e["type"] == "combat"]
                    deaths = [e for e in trace if e["type"] == "death"]
                    writes = [e for e in trace if e["type"] == "doc" and e["kind"] == "write"]
                    casualties = sum(sum(c.get("casualties", {}).values()) for c in combats)
                    routed_count = sum(len(c.get("routed", {})) for c in combats)
                    alive_pop = sum(p["size"] for p in people_snap.values() if p["alive"])
                    alive_units = sum(1 for p in people_snap.values() if p["alive"])
                    weighted_sigs = sum(d["signed_weight"] for d in docs_snap.values())
                    active_guards = sum(1 for p in people_snap.values() if p["alive"] and p["posture"])
                    edible_keys = {
                        k for k, v in self.spec["items"].items() if v.get("kind") == "edible"
                    }
                    total_edibles = sum(
                        sum(pl["items"].get(k, 0) for k in edible_keys) for pl in places_snap.values()
                    ) + sum(
                        sum(p["inv"].get(k, 0) for k in edible_keys)
                        for p in people_snap.values()
                        if p["alive"]
                    )

                    tick_payload = dict(
                        tick=t,
                        day=day,
                        slot=t % interval,
                        time_of_day=tod,
                        opening=False,
                        people=people_snap,
                        places=places_snap,
                        docs=docs_snap,
                        dynamic_items=dict(self.world.dynamic_items),
                        decisions=raw_decisions,
                        note_ops=tick_note_ops,
                        notes_snapshot={
                            pid: {k: dict(v) for k, v in ns.items()}
                            for pid, ns in self.notes_state.items()
                            if ns
                        },
                        trace=trace,
                        notices=dedup_notices,
                    )
                    self.ticks.append(tick_payload)
                    self.tick_summaries.append(
                        dict(
                            tick=t,
                            day=day,
                            slot=t % interval,
                            time_of_day=tod,
                            combats=len(combats),
                            deaths=len(deaths),
                            casualties=casualties,
                            routed=routed_count,
                            writes=len(writes),
                            alive_pop=alive_pop,
                            alive_units=alive_units,
                            weighted_sigs=weighted_sigs,
                            active_guards=active_guards,
                            total_edibles=total_edibles,
                        )
                    )

                self.committed_tick = cur_tick
                self.staged = mem.execute(
                    "SELECT COUNT(*) FROM turns WHERE tick=?", (cur_tick,)
                ).fetchone()[0]
                self.alive = sum(1 for p in self.world.people.values() if p.is_alive)
                self.telemetry = self._compute_telemetry(mem)
                self._last_sig = sig
            finally:
                mem.close()

    def _compute_telemetry(self, mem: sqlite3.Connection) -> dict:
        by_model: dict[str, dict] = defaultdict(
            lambda: dict(turns=0, steps=0, prompt_tokens=0, cached_tokens=0, completion_tokens=0)
        )
        for row in mem.execute("SELECT metrics FROM turns WHERE tick < ?", (self.committed_tick,)):
            met = json.loads(row["metrics"]) if row["metrics"] else {}
            model = met.get("model") or "offline"
            bucket = by_model[model]
            bucket["turns"] += 1
            bucket["steps"] += int(met.get("loop_steps") or 1)
            for key in ("prompt_tokens", "cached_tokens", "completion_tokens"):
                if isinstance(met.get(key), int):
                    bucket[key] += met[key]
        models_out = {}
        for model, b in by_model.items():
            pt = b["prompt_tokens"]
            models_out[model] = dict(
                turns=b["turns"],
                avg_steps=round(b["steps"] / b["turns"], 2) if b["turns"] else 0.0,
                prompt_tokens=pt,
                cached_tokens=b["cached_tokens"],
                cache_rate=round(100.0 * b["cached_tokens"] / pt, 1) if pt else 0.0,
                completion_tokens=b["completion_tokens"],
            )
        comp_row = mem.execute("SELECT COALESCE(SUM(compressions), 0) FROM agents").fetchone()
        return dict(
            total_compressions=comp_row[0] if comp_row else 0,
            by_model=models_out,
            cumulative_casualties=sum(s["casualties"] for s in self.tick_summaries),
            total_combats=sum(s["combats"] for s in self.tick_summaries),
            total_deaths=sum(s["deaths"] for s in self.tick_summaries),
        )

    def meta(self) -> dict:
        self.refresh()
        assert self.world is not None
        return dict(
            name=self.db_path.name,
            title=self.spec["title"],
            scenario_id=self.spec["id"],
            briefing=self.spec["briefing"],
            theme=self.spec.get("theme", {}),
            rules=dict(
                clock_interval=self.spec["rules"]["clock_interval"],
                max_energy=self.spec["rules"]["max_energy"],
                max_health=self.spec["rules"]["max_health"],
                force_modes=self.spec["rules"]["force_modes"],
            ),
            places={
                k: dict(
                    id=k,
                    label=v["label"],
                    pos=v.get("pos"),
                    paths=v["paths"],
                    comms=v.get("comms", False),
                )
                for k, v in self.spec["places"].items()
            },
            items={
                k: dict(label=v["label"], kind=v["kind"], weight=v.get("weight", 1))
                for k, v in self.spec["items"].items()
            },
            roles=self.roles,
            committed_ticks=self.committed_tick,
            staged=self.staged,
            alive=self.alive,
            residents=self.count,
            total_initial_pop=sum(r["initial_size"] for r in self.roles.values()),
            tick_summaries=self.tick_summaries,
            telemetry=self.telemetry,
        )

    def tick_data(self, tick_idx: int) -> dict:
        self.refresh()
        if self.committed_tick == 0:
            assert self.opening_tick is not None
            return self.opening_tick
        if tick_idx < 0 or tick_idx >= len(self.ticks):
            raise IndexError(f"tick {tick_idx} out of range (0..{len(self.ticks) - 1})")
        return self.ticks[tick_idx]


class RunIndex:
    """Discover and cache SQLite runs from a directory or explicit DB path."""

    def __init__(self, runs_dir: Path | None = None, db_path: Path | None = None):
        self.runs_dir = runs_dir
        self.db_path = db_path
        self._caches: dict[str, RunCache] = {}
        self._lock = threading.Lock()

    def _discover_paths(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        if self.db_path and self.db_path.is_file():
            found[self.db_path.name] = self.db_path
        if self.runs_dir and self.runs_dir.is_dir():
            for path in sorted(self.runs_dir.glob("*.sqlite")):
                if path.is_file():
                    found[path.name] = path
        return found

    def list_runs(self) -> list[dict]:
        entries = []
        for path in self._discover_paths().values():
            header = _quick_header(path)
            if header:
                entries.append(header)
        entries.sort(key=lambda item: item["mtime"], reverse=True)
        return entries

    def get(self, name: str) -> RunCache:
        if "/" in name or "\\" in name or ".." in name:
            raise KeyError(name)
        paths = self._discover_paths()
        if name not in paths:
            raise KeyError(name)
        with self._lock:
            cache = self._caches.get(name)
            if cache is None:
                cache = RunCache(paths[name])
                self._caches[name] = cache
            return cache
