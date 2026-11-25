import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from ..model import PRESETS, load
from ..runtime.backends import ChatAPI, Scripted, load_backends
from ..runtime.simulation import Simulation
from ..runtime.storage import Store


def build_parser():
    parser = argparse.ArgumentParser(description="Persistent social dynamics simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    sub.add_parser("scenarios", help="list bundled scenarios and their default scale")
    init.add_argument("--residents", type=int, help="override the scenario's population")
    init.add_argument("--seed", type=int, default=7)
    init.add_argument(
        "--scenario",
        default=None,
        help="bundled scenario ID or path to a scenario JSON file",
    )
    init.add_argument("--personas", type=Path, help="JSON mapping resident IDs to descriptions")
    run = sub.add_parser("run")
    run.add_argument("--ticks", type=int, default=1)
    run.add_argument(
        "--decision-budget",
        type=int,
        help="at most this many new resident decisions across this invocation",
    )
    backend_options = run.add_mutually_exclusive_group()
    backend_options.add_argument("--backend", choices=["scripted", "api"], default="api")
    backend_options.add_argument("--backends", type=Path, help="JSON profile and routing configuration")
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--anchors", type=int, default=64, help="recent prompts used in prefix scheduling")
    run.add_argument(
        "--window",
        type=int,
        default=0,
        help="periodic compression window; 0 disables periodic folding until --context-bytes",
    )
    run.add_argument("--keep", type=int, default=4)
    run.add_argument("--context-bytes", type=int, default=180000, help="prompt content byte budget")
    run.add_argument("--base-url", default=os.environ.get("HERD_BASE_URL"))
    run.add_argument("--model", default=os.environ.get("HERD_MODEL"))
    run.add_argument(
        "--mode",
        choices=["agent_loop", "json"],
        default="agent_loop",
        help="API decision protocol (default: native tool calls)",
    )
    run.add_argument(
        "--max-steps",
        type=int,
        default=6,
        help="maximum model calls per resident per tick in agent_loop mode",
    )
    run.add_argument("--max-tokens", type=int, default=1536)
    run.add_argument("--tokenizer-path", help="same-host chat-template-aware /tokenize endpoint")
    run.add_argument("--cache-key", help="optional routing hint for compatible providers")
    stats = sub.add_parser("stats")
    replay = sub.add_parser("replay")
    fork = sub.add_parser("fork")
    fork.add_argument("--to", type=Path, required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("resident")
    serve_cmd = sub.add_parser("serve", help="launch the zero-dependency live Story-First WebUI")
    serve_cmd.add_argument("--runs", type=Path, default=Path("runs"), help="directory containing .sqlite archives")
    serve_cmd.add_argument("--db", type=Path, help="optional single .sqlite archive to monitor")
    serve_cmd.add_argument("--host", default="0.0.0.0")
    serve_cmd.add_argument("--port", type=int, default=8765)
    for child in [init, run, stats, inspect, replay, fork]:
        child.add_argument("--db", type=Path, required=True)
    return parser


def run_simulation(store, args, parser):
    if args.ticks < 0 or args.max_tokens < 1 or args.max_steps < 1:
        parser.error("require nonnegative ticks and positive max-tokens and max-steps")
    remaining = args.decision_budget
    if remaining is not None and remaining < 1:
        parser.error("--decision-budget must be positive")

    if args.backends is not None:
        backend = load_backends(args.backends)
    elif args.backend == "scripted":
        backend = Scripted()
    else:
        if not args.base_url or not args.model:
            parser.error("API requires --base-url and --model (or HERD_BASE_URL and HERD_MODEL)")
        backend = ChatAPI(
            args.base_url,
            args.model,
            args.max_tokens,
            args.tokenizer_path,
            args.cache_key,
            mode=args.mode,
            max_steps=args.max_steps,
        )
    sim = Simulation(
        store,
        backend,
        workers=args.workers,
        anchors=args.anchors,
        window=args.window,
        keep=args.keep,
        context_bytes=args.context_bytes,
    )
    for _ in range(args.ticks):
        if remaining == 0 or not sim.step(remaining):
            break
        if remaining is not None:
            remaining -= sim.last_decisions


def execute(args, parser):
    if args.command == "serve":
        from ..web import serve

        serve(runs_dir=args.runs, db_path=args.db, host=args.host, port=args.port)
        return {"stopped": True}

    if args.command == "scenarios":
        entries = []
        for path in sorted(PRESETS.glob("*.json")):
            spec = load(path)
            entries.append({k: spec[k] for k in ("id", "engine", "title", "default_residents")})
        return entries

    if args.command == "init":
        personas = json.loads(args.personas.read_text()) if args.personas else None
        store = Store.create(args.db, args.residents, args.seed, personas, args.scenario)
    else:
        if not args.db.is_file():
            parser.error("database not found; run init first")
        store = Store(args.db)
    try:
        if args.command == "run":
            run_simulation(store, args, parser)
        if args.command == "replay":
            return store.replay()
        if args.command == "fork":
            store.fork(args.to)
            return {"copy": str(args.to)}
        if args.command == "inspect":
            with store.read_snapshot():
                world = store.world()
                if args.resident not in world.people:
                    parser.error("unknown resident")
                return {
                    "state": world.people[args.resident].to_dict(),
                    **store.mind(args.resident),
                    "events": store.events(args.resident, 0, world.tick - 1),
                }
        return store.stats()
    finally:
        store.db.close()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        print(json.dumps(execute(args, parser), ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        sys.exit(str(exc))
