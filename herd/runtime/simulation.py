"""Stage concurrent inference independently per backend, then commit one world transition."""

from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

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
        if req.comm_ops:
            # Notes written while deliberating commit with the action at the tick barrier.
            note_ops = [*req.comm_ops, *note_ops]
        return reply, dict(metrics, note_ops=note_ops)

    def _run_scope(self, tick, backend, requests, stage):
        anchors = self.anchors_by_scope[backend.scope]
        workers = backend.workers or self.workers
        staged = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            queue = PrefixQueue(list(zip(pool.map(backend.key, requests), requests)), anchors, mru=True)
            in_flight = {}
            errors = []
            while queue.keys or in_flight:
                limit = workers if anchors else 1
                while queue.keys and not errors and len(in_flight) < limit:
                    req, key, length = queue.pop()
                    in_flight[pool.submit(self.decision, req)] = (req, key, length)
                if not in_flight:
                    break
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    req, key, length = in_flight.pop(future)
                    try:
                        reply, metrics = future.result()
                    except Exception as exc:
                        errors.append(exc)
                        continue
                    metrics[f"matched_prefix_{backend.unit}"] = length
                    if stage(tick, req, reply, metrics, backend.scope, key, anchors.maxlen):
                        staged += 1
                        anchors.append(key)
            if errors:
                raise errors[0]
        return staged

    def step(self, decision_budget=None):
        """Advance one tick: [opening deliberation barrier ->] action barrier -> settlement."""
        if decision_budget is not None and decision_budget < 1:
            raise ValueError("decision budget must be positive")
        self.last_decisions = 0
        with self.store.read_snapshot():
            world = self.store.world()
            tick = world.tick
            two_phase = bool(world.spec["rules"].get("comm_rounds", 0))
        if two_phase and not self.store.comm_settled(tick):
            if not self._phase(tick, "comm", decision_budget):
                return False
            self.store.settle_comm(tick)
            if decision_budget is not None:
                decision_budget -= self.last_decisions
                if decision_budget < 1:
                    return False
        return self._phase(tick, "action" if two_phase else "", decision_budget) and self.store.advance(tick)

    def _phase(self, tick, phase, decision_budget):
        groups = defaultdict(list)
        with self.store.read_snapshot():
            world = self.store.world()
            if world.tick != tick:
                return False
            staged = self.store.comm_staged(tick) if phase == "comm" else self.store.staged(tick)
            remaining = set(world.active_residents()) - staged
            agents = [a for a in self.agents if a.actor in remaining][:decision_budget]
            index = world.build_index()
            for agent in agents:
                backend = self.assignment[agent.actor]
                req = agent.prepare(
                    self.store,
                    world,
                    self.window,
                    self.keep,
                    backend.context_bytes or self.context_bytes,
                    index,
                    mode=backend.mode,
                    phase=phase,
                    comm=self.store.comm_turn(agent.actor, tick) if phase == "action" else None,
                )
                groups[backend.scope].append(req)

        stage = self.store.stage_comm if phase == "comm" else self.store.stage
        if len(groups) == 1:
            scope, requests = next(iter(groups.items()))
            self.last_decisions += self._run_scope(tick, self.backends_by_scope[scope], requests, stage)
        elif groups:
            with ThreadPoolExecutor(max_workers=len(groups)) as coordinators:
                futures = [
                    coordinators.submit(self._run_scope, tick, self.backends_by_scope[scope], requests, stage)
                    for scope, requests in groups.items()
                ]
                self.last_decisions += sum(f.result() for f in futures)
        return len(agents) == len(remaining)
