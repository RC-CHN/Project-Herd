"""Stage concurrent inference independently per backend, then commit one world transition."""

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from .agent import Agent
from .backends import RoutedBackend
from .prefix import PrefixQueue


class Simulation:
    def __init__(self, store, backend, *, workers=2, anchors=64, window=0, keep=4, context_bytes=180000):
        if not (
            workers >= 1
            and anchors >= 1
            and keep >= 0
            and context_bytes > 0
            and (window == 0 or 0 <= keep < window)
        ):
            raise ValueError(
                "require positive workers, anchors, context-bytes and window == 0 or 0 <= keep < window"
            )
        self.store, self.backend = store, backend
        self.workers, self.window, self.keep = workers, window, keep
        self.context_bytes = context_bytes
        self.agents = store.agents()
        self.assignment = (
            backend.resolve(store.world())
            if isinstance(backend, RoutedBackend)
            else {agent.actor: backend for agent in self.agents}
        )
        self.decision_agents = {actor: Agent(backend, store) for actor, backend in self.assignment.items()}
        self.backends_by_scope = {b.scope: b for b in self.assignment.values()}
        self.anchors_by_scope = {
            scope: deque(store.prefix_history(scope, anchors), maxlen=anchors)
            for scope in self.backends_by_scope
        }

    def decision(self, req):
        reply, note_ops, metrics = self.decision_agents[req.actor].run_turn(req)
        return reply, dict(metrics, note_ops=note_ops)

    def _run_scope(self, tick, backend, requests):
        anchors = self.anchors_by_scope[backend.scope]
        workers = backend.workers or self.workers
        staged = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            queue = PrefixQueue(list(zip(pool.map(backend.key, requests), requests)), anchors)
            while queue.keys:
                size = min(workers if anchors else 1, len(queue.keys))
                batch = [queue.pop() for _ in range(size)]
                futures = [pool.submit(self.decision, req) for req, _, _ in batch]
                errors = []
                for (req, key, length), future in zip(batch, futures):
                    try:
                        reply, metrics = future.result()
                    except Exception as exc:
                        errors.append(exc)
                        continue
                    metrics[f"matched_prefix_{backend.unit}"] = length
                    if self.store.stage(tick, req, reply, metrics, backend.scope, key, anchors.maxlen):
                        staged += 1
                        anchors.append(key)
                if errors:
                    raise errors[0]
        return staged

    def step(self, decision_budget=None):
        if decision_budget is not None and decision_budget < 1:
            raise ValueError("decision budget must be positive")
        self.last_decisions = 0
        groups = defaultdict(list)
        with self.store.read_snapshot():
            world = self.store.world()
            tick = world.tick
            remaining = set(world.active_residents()) - self.store.staged(tick)
            agents = [a for a in self.agents if a.actor in remaining][:decision_budget]
            index = world.build_index()
            for agent in agents:
                backend = self.assignment[agent.actor]
                req = agent.prepare(
                    self.store,
                    world,
                    self.window,
                    self.keep,
                    self.context_bytes,
                    index,
                    mode=backend.mode,
                )
                groups[backend.scope].append(req)

        if len(groups) == 1:
            scope, requests = next(iter(groups.items()))
            self.last_decisions = self._run_scope(tick, self.backends_by_scope[scope], requests)
        elif groups:
            with ThreadPoolExecutor(max_workers=len(groups)) as coordinators:
                futures = [
                    coordinators.submit(self._run_scope, tick, self.backends_by_scope[scope], requests)
                    for scope, requests in groups.items()
                ]
                self.last_decisions = sum(f.result() for f in futures)
        return len(agents) == len(remaining) and self.store.advance(tick)
