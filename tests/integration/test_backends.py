"""Offline coverage for routing, scope-local scheduling, and model attribution."""

from collections import Counter
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from experiments.report_polity import report
from herd.runtime.backends import ChatAPI, RoutedBackend, Scripted, load_backends
from herd.runtime.simulation import Simulation
from herd.runtime.storage import Store


class BackendTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "run.sqlite"
        self.store = Store.create(self.path, 24, 71, scenario="citystate-coup")
        self.addCleanup(self.store.db.close)

    def test_routed_backend_deterministic_random_group_and_actor_assignment(self):
        world = self.store.world()
        residents = sorted(world.people)
        groups = sorted(
            world.spec["groups"], key=lambda role: sum(p.role == role for p in world.people.values())
        )
        group = groups[-1]
        candidates = [a for a in residents if world.people[a].role == group]
        config = {
            "default": "primary",
            "profiles": {
                name: {"type": "scripted", "workers": 2} for name in ("primary", "alternate", "third")
            },
            "routes": [
                {"profile": "alternate", "random": 8, "seed": 19},
                {"profile": "third", "group": group, "ratio": 0.6},
                {"profile": "alternate", "group": groups[-2]},
                {"profile": "primary", "actors": [candidates[0]]},
            ],
        }
        path = self.path.with_name("profiles.json")
        path.write_text(json.dumps(config))
        expected = dict.fromkeys(residents, "primary")
        for a in random.Random("route/19/0").sample(residents, 8):
            expected[a] = "alternate"
        for a in random.Random("route/71/1").sample(candidates, round(0.6 * len(candidates))):
            expected[a] = "third"
        for a, p in world.people.items():
            if p.role == groups[-2]:
                expected[a] = "alternate"
        expected[candidates[0]] = "primary"
        router = load_backends(path)
        self.assertEqual({a: b.name for a, b in router.resolve(world).items()}, expected)
        budget = 5
        self.assertFalse(Simulation(self.store, router).step(budget))
        saved = dict(self.store.db.execute("SELECT actor,reply FROM turns WHERE tick=0"))
        self.assertEqual(len(saved), budget)
        output = subprocess.check_output(
            [sys.executable, "-m", "herd", "run", "--backends", str(path), "--db", str(self.path)],
            text=True,
        )
        stats = json.loads(output)
        self.assertEqual(stats["decisions"], len(residents))
        self.assertEqual(self.store.replay()["verified_ticks"], 1)
        self.assertEqual(
            {a: b.name for a, b in load_backends(path).resolve(self.store.world()).items()}, expected
        )
        metrics = self.store.db.execute("SELECT actor,metrics FROM turns ORDER BY actor").fetchall()
        self.assertEqual({a: json.loads(m)["profile"] for a, m in metrics}, expected)
        for actor, reply in saved.items():
            self.assertEqual(
                self.store.db.execute(
                    "SELECT reply FROM turns WHERE tick=0 AND actor=?", (actor,)
                ).fetchone()[0],
                reply,
            )

        invalid = (
            [{"profile": "missing", "random": 1}],
            [{"profile": "third", "actors": ["nobody"]}],
            [{"profile": "third", "group": "missing"}],
            [{"profile": "third", "random": True}],
            [{"profile": "third", "random": 1, "ratio": 0.5}],
            [{"profile": "third", "ratio": 0}],
            [{"profile": "third", "actors": residents[:1], "group": group}],
            [{"profile": "third", "random": 100000}],
        )
        for rules in invalid:
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                load_backends(dict(config, routes=rules)).resolve(world)
        for broken in (
            dict(config, default="absent"),
            dict(config, extra=[]),
            dict(config, profiles={"primary": {"type": "api", "model": "x"}}),
            dict(config, profiles={"primary": {"type": "scripted", "workers": False}}),
        ):
            with self.assertRaises(ValueError):
                load_backends(broken)

    def test_simulation_isolates_prefix_queues_and_workers_per_backend(self):
        both_started = threading.Barrier(2, timeout=5)

        class Metered(Scripted):
            def __init__(self, name, workers):
                super().__init__(name=name, workers=workers)
                self.calls = self.busy = self.peak = 0
                self.lock = threading.Lock()
                self.warm_batch = threading.Barrier(workers, timeout=5)

            def key(self, req):
                return f"{self.name}/{req.actor}/{req.frame['view']['tick']}".encode()

            def first_step(self):
                with self.lock:
                    self.calls += 1
                    call = self.calls
                    self.busy += 1
                    self.peak = max(self.peak, self.busy)
                try:
                    if call == 1:
                        both_started.wait()
                    elif call <= 1 + self.workers:
                        self.warm_batch.wait()
                finally:
                    with self.lock:
                        self.busy -= 1

            def decide(self, req):
                if self.mode != "scripted":
                    raise AssertionError("native agents must run through chat")
                self.first_step()
                return '{"action":"wait"}', {"prompt_tokens": 12, "cached_tokens": 4}

            def chat(self, messages, tools=None, response_format=None):
                if messages[-1]["role"] == "tool":
                    name, prompt, cached = "wait", 5, 1
                else:
                    self.first_step()
                    name, prompt, cached = "inspect_room", 12, 4
                call = {"id": name, "type": "function", "function": {"name": name, "arguments": "{}"}}
                return {"role": "assistant", "tool_calls": [call]}, {
                    "prompt_tokens": prompt,
                    "cached_tokens": cached,
                }

        local, distant = Metered("base", 2), Metered("distant", 3)
        distant.mode = "agent_loop"
        distant.max_steps = 2
        profiles = {b.name: b for b in (local, distant)}
        chosen = sorted(self.store.world().people)[:10]
        router = RoutedBackend(
            default=local.name, profiles=profiles, routes=[{"profile": distant.name, "actors": chosen}]
        )
        sim = Simulation(self.store, router, workers=1, anchors=4)
        for _ in range(2):
            self.assertTrue(sim.step())
        self.assertEqual((local.peak, distant.peak), (2, 3))
        self.assertEqual(set(sim.anchors_by_scope), {local.scope, distant.scope})
        restarted = Simulation(self.store, router, anchors=4)
        for b in (local, distant):
            history = self.store.prefix_history(b.scope, 64)
            self.assertEqual(len(history), 4)
            self.assertTrue(all(key.startswith((b.name + "/").encode()) for key in history))
            self.assertEqual(list(restarted.anchors_by_scope[b.scope]), history)
        stats = self.store.stats()
        summary = report(self.store.db)["backend_attribution"]
        for b in (local, distant):
            actors = sorted(a for a, backend in sim.assignment.items() if backend == b)
            loop = b is distant
            expected = dict(
                actors=actors,
                decisions=2 * len(actors),
                prompt_tokens=2 * len(actors) * (17 if loop else 12),
                cached_tokens=2 * len(actors) * (5 if loop else 4),
                loop_steps=2 * len(actors) * (2 if loop else 0),
            )
            self.assertEqual(stats["by_profile"][b.name], expected)
            self.assertEqual({key: summary["by_profile"][b.name][key] for key in expected}, expected)
            self.assertEqual(summary["by_profile"][b.name]["mean_loop_steps"], 2 if loop else 0)
        by_model = stats["by_model"]["scripted"]
        self.assertEqual((by_model["decisions"], by_model["actors"]), (48, sorted(sim.assignment)))
        self.assertEqual({key: summary["by_model"]["scripted"][key] for key in by_model}, by_model)
        self.assertEqual(self.store.replay()["verified_ticks"], 2)

    def test_api_profile_credentials_and_model_scopes(self):
        config = {
            "default": "first",
            "profiles": {
                "first": {
                    "type": "api",
                    "base_url": "https://example.invalid/v1",
                    "model": "test",
                    "api_key_env": "HERD_FIRST_TEST_KEY",
                    "max_tokens": 2048,
                    "cache_key": "first",
                    "workers": 3,
                }
            },
        }
        config["profiles"]["second"] = deepcopy(config["profiles"]["first"])
        config["profiles"]["second"].update(api_key_env="HERD_SECOND_TEST_KEY", workers=1)
        env = {"HERD_FIRST_TEST_KEY": "fake-first", "HERD_SECOND_TEST_KEY": "fake-second"}
        with patch.dict(os.environ, env):
            profiles = load_backends(config).profiles
            override = ChatAPI(
                "https://example.invalid/v1", "test", api_key_env="HERD_FIRST_TEST_KEY", api_key="explicit"
            )
        first, second = profiles.values()
        self.assertEqual(
            (first.secret, second.secret, override.secret), ("fake-first", "fake-second", "explicit")
        )
        self.assertNotEqual(first.scope, second.scope)
        self.assertEqual((first.max_tokens, first.workers, first.cache_key), (2048, 3, "first"))
        self.assertEqual(second.workers, 1)

    def test_two_api_profiles_send_their_own_requests_and_record_their_models(self):
        selected = sorted(self.store.world().people)[:5]
        config = {
            "default": "left",
            "profiles": {
                "left": {
                    "type": "api",
                    "base_url": "https://left.invalid/v1",
                    "model": "alpha",
                    "mode": "json",
                    "api_key_env": "HERD_TEST_LEFT",
                    "max_tokens": 19,
                    "cache_key": "left/{actor}",
                    "workers": 2,
                    "tokenizer_path": "/tokenize",
                },
                "right": {
                    "type": "api",
                    "base_url": "https://right.invalid/v1",
                    "model": "beta",
                    "mode": "agent_loop",
                    "max_steps": 2,
                    "api_key_env": "HERD_TEST_RIGHT",
                    "max_tokens": 83,
                    "cache_key": "right/{actor}",
                    "workers": 1,
                },
            },
            "routes": [{"profile": "right", "actors": selected}],
        }
        sent = []
        send_lock = threading.Lock()

        def transport(request, timeout):
            body = json.loads(request.data)
            identity = json.loads(body["messages"][1]["content"].split("\n", 1)[0])
            actor = identity["id"]
            name = "right" if actor in selected else "left"
            spec = config["profiles"][name]
            self.assertEqual(request.get_header("Authorization"), f"Bearer test-{name}")
            self.assertEqual(body["model"], spec["model"])
            if request.full_url.endswith("/tokenize"):
                self.assertEqual(request.full_url, "https://left.invalid/tokenize")
                result = {"tokens": [7, 12, int(actor[1:])]}
            else:
                self.assertEqual(request.full_url, spec["base_url"] + "/chat/completions")
                self.assertEqual(body["max_tokens"], spec["max_tokens"])
                self.assertEqual(body["prompt_cache_key"], f"{name}/{actor}")
                if name == "right":
                    self.assertTrue(body["tools"])
                    self.assertNotIn("response_format", body)
                    first = body["messages"][-1]["role"] != "tool"
                    op = "stage_speech" if first else "wait"
                    args = {"text": body["model"]} if first else {}
                    call = {
                        "id": op,
                        "type": "function",
                        "function": {"name": op, "arguments": json.dumps(args)},
                    }
                    message = {"role": "assistant", "tool_calls": [call]}
                else:
                    self.assertEqual(body["response_format"]["type"], "json_object")
                    self.assertNotIn("tools", body)
                    message = {"content": json.dumps({"action": "wait", "speech": body["model"]})}
                result = {
                    "choices": [{"message": message}],
                    "usage": {"prompt_tokens": 11, "prompt_tokens_details": {"cached_tokens": 3}},
                }
                with send_lock:
                    sent.append(actor)
            return io.BytesIO(json.dumps(result).encode())

        with patch.dict(os.environ, {"HERD_TEST_LEFT": "test-left", "HERD_TEST_RIGHT": "test-right"}):
            router = load_backends(config)
        with patch("herd.runtime.backends.http.build_opener") as opener:
            opener.return_value.open.side_effect = transport
            self.assertTrue(Simulation(self.store, router).step())
        self.assertEqual(Counter(sent), {a: 2 if a in selected else 1 for a in self.store.world().people})
        stats = self.store.stats()
        attribution = report(self.store.db)["backend_attribution"]
        for actor, reply, raw in self.store.db.execute("SELECT actor,reply,metrics FROM turns"):
            name = "right" if actor in selected else "left"
            model = config["profiles"][name]["model"]
            metric = json.loads(raw)
            self.assertEqual(
                (metric["profile"], metric["model"], json.loads(reply)["speech"]), (name, model, model)
            )
            self.assertEqual(metric["mode"], config["profiles"][name]["mode"])
            self.assertEqual(metric["loop_steps"], 2 if name == "right" else 0)
            self.assertIn("matched_prefix_bytes" if name == "right" else "matched_prefix_tokens", metric)
        for name, expected in (("left", 19), ("right", 5)):
            model = config["profiles"][name]["model"]
            self.assertEqual(stats["by_model"][model]["actors"], stats["by_profile"][name]["actors"])
            self.assertEqual(
                attribution["by_model"][model]["actors"], attribution["by_profile"][name]["actors"]
            )
            self.assertEqual(attribution["by_model"][model]["decisions"], expected)
            steps = 2 if name == "right" else 1
            self.assertEqual(attribution["by_model"][model]["cached_tokens"], 3 * expected * steps)

    def test_failure_in_one_scope_keeps_other_scope_and_skips_staged_on_resume(self):
        selected = sorted(self.store.world().people)[:8]
        calls = Counter()

        class SometimesUnavailable(Scripted):
            def __init__(self, name):
                super().__init__(name=name, workers=1)
                self.failed_actor = None

            def decide(self, request):
                calls[request.actor] += 1
                if (
                    self.name == "flaky"
                    and self.failed_actor is None
                    and sum(calls[a] for a in selected) == 3
                ):
                    self.failed_actor = request.actor
                    raise RuntimeError("test profile unavailable")
                return super().decide(request)

        healthy = SometimesUnavailable("healthy")
        flaky = SometimesUnavailable("flaky")
        router = RoutedBackend(
            healthy.name,
            {p.name: p for p in (healthy, flaky)},
            [{"profile": flaky.name, "actors": selected}],
        )
        with self.assertRaisesRegex(RuntimeError, "test profile unavailable"):
            Simulation(self.store, router).step()
        staged = self.store.staged(0)
        self.assertEqual(self.store.world().tick, 0)
        self.assertEqual(len(staged & set(selected)), 2)
        self.assertEqual(staged - set(selected), set(self.store.world().people) - set(selected))
        self.assertNotIn(flaky.failed_actor, staged)
        self.assertTrue(Simulation(self.store, router).step())
        self.assertEqual(self.store.replay()["verified_ticks"], 1)
        for actor in self.store.world().people:
            self.assertEqual(calls[actor], 2 if actor == flaky.failed_actor else 1)

    def test_chat_api_configurable_and_nullable_temperature_in_profiles(self):
        config = {
            "default": "t1",
            "profiles": {
                "t1": {
                    "type": "api",
                    "base_url": "http://localhost:8000/v1",
                    "model": "kimi-like",
                    "temperature": 1.0,
                    "preserve_reasoning": True,
                },
                "tnone": {"type": "api", "base_url": "http://localhost:8000/v1", "model": "omit-temp", "temperature": None},
            },
        }
        router = load_backends(config)
        self.assertEqual(router.profiles["t1"].temperature, 1.0)
        self.assertTrue(router.profiles["t1"].preserve_reasoning)
        self.assertIsNone(router.profiles["tnone"].temperature)
        self.assertFalse(router.profiles["tnone"].preserve_reasoning)
        self.assertNotEqual(router.profiles["t1"].scope, router.profiles["tnone"].scope)

        sent_bodies = []

        def fake_post(url, body):
            sent_bodies.append(body)
            return {
                "choices": [{"message": {"role": "assistant", "content": '{"action":"wait"}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }

        for key in ("t1", "tnone"):
            backend = router.profiles[key]
            with patch.object(backend, "post", side_effect=fake_post):
                backend.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(sent_bodies[0]["temperature"], 1.0)
        self.assertNotIn("temperature", sent_bodies[1])

        with self.assertRaises(ValueError):
            ChatAPI("http://localhost:8000/v1", "bad", temperature=3.0)
        with self.assertRaises(ValueError):
            ChatAPI("http://localhost:8000/v1", "bad", temperature=True)
