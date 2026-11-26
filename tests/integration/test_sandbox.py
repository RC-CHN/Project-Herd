import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from herd.runtime.backends import ChatAPI, Scripted
from herd.runtime.memory import Request, compress
from herd.runtime.prefix import PrefixQueue, common_prefix, message_key
from herd.runtime.simulation import Simulation
from herd.runtime.storage import Store
from herd.model import ACTIONS, Intent, POLITY_FIELDS, encode
from herd.model.intent import WORLD_RULES
from tests.support import PolityCase


class SandboxRuntimeTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "polity.sqlite"

    def open_store(self, n=24):
        store = (
            Store.create(self.path, n, 7, scenario="citystate-coup")
            if not self.path.exists()
            else Store(self.path)
        )
        self.addCleanup(store.db.close)
        return store

    def test_longest_prefix_matches_brute_force_for_tokens_and_bytes(self):
        for keys in (
            [b"sys|a|1", b"sys|a|2", b"sys|b|1", b"sys|a|10", b"other"],
            [(1, 2, 3), (1, 2, 4), (1, 3, 1), (1, 2, 3, 9), (9,)],
        ):
            anchors = [keys[0], keys[2]]
            items = [(k, Request(f"r{i:03}", [], {}, -1, [], False)) for i, k in enumerate(keys)]
            queue = PrefixQueue(items, anchors)
            remaining = dict(enumerate(keys))
            while remaining:
                req, key, length = queue.pop()
                best = max(common_prefix(a, k) for a in anchors for k in remaining.values())
                self.assertEqual(length, best)
                idx = int(req.actor[1:])
                self.assertEqual(remaining.pop(idx), key)

    def test_prefixes_survive_append_and_system_survives_compression(self):
        store = self.open_store(16)
        sim = Simulation(store, Scripted(), window=0, keep=1)
        first = sim.agents[0].prepare(store, store.world(), 3, 1, 180000)
        sim.step()
        second = sim.agents[0].prepare(store, store.world(), 3, 1, 180000)
        self.assertTrue(message_key(second.messages).startswith(message_key(first.messages)))
        for _ in range(2):
            sim.step()
        compressed = sim.agents[0].prepare(store, store.world(), 3, 1, 180000)
        self.assertTrue(compressed.compressed)
        self.assertEqual(compressed.messages[0], first.messages[0])
        self.assertTrue(message_key(compressed.messages).startswith(message_key([first.messages[0]])))

    def test_window_zero_stays_append_only_until_context_budget(self):
        store = self.open_store(12)
        sim = Simulation(store, Scripted(), window=0, keep=2, context_bytes=180000)
        for _ in range(6):
            sim.step()
        self.assertEqual(store.stats()["compressions"], 0)
        full = sim.agents[0].prepare(store, store.world(), 0, 2, 180000)
        full_bytes = sum(len(m["content"].encode()) + 12 for m in full.messages)
        # Tight context budget below the full append-only history forces on-demand folding.
        budget = full_bytes - 500
        tight = sim.agents[0].prepare(store, store.world(), 0, 2, budget)
        self.assertTrue(tight.compressed)
        self.assertLessEqual(sum(len(m["content"].encode()) + 12 for m in tight.messages), budget)

    def test_restart_restores_bounded_prefix_hints_with_backend_isolation(self):
        store = self.open_store(16)
        sim = Simulation(store, Scripted(), anchors=3)
        sim.step()
        reloaded = Simulation(store, Scripted(), anchors=3)
        self.assertEqual(len(reloaded.anchors_by_scope[reloaded.backend.scope]), 3)
        other_api = ChatAPI("http://localhost:8000/v1", "other-model")
        self.assertEqual(store.prefix_history(other_api.scope, 3), [])

    def test_retry_after_partial_failure_never_repeats_staged_decisions(self):
        store = self.open_store(12)
        seen = []

        class Flaky(Scripted):
            def decide(self, req):
                seen.append(req.actor)
                if len(seen) == 5:
                    raise RuntimeError("transient outage")
                return super().decide(req)

        with self.assertRaisesRegex(RuntimeError, "transient outage"):
            Simulation(store, Flaky(), workers=1).step()
        self.assertEqual(len(store.staged(0)), 4)
        self.assertEqual(store.world().tick, 0)
        Simulation(store, Scripted(), workers=2).step()
        self.assertEqual(store.world().tick, 1)
        self.assertEqual(store.replay()["verified_ticks"], 1)

    def test_crowded_local_speech_is_archived_and_fits_prompt(self):
        store = self.open_store(16)

        class Loud(Scripted):
            def decide(self, req):
                return encode({"action": "wait", "speech": "甲" * 200}), {}

        Simulation(store, Loud(), workers=2, window=0, context_bytes=180000).step()
        full = store.agents()[0].prepare(store, store.world(), 0, 0, 180000)
        full_bytes = sum(len(m["content"].encode()) + 12 for m in full.messages)
        # Budget below the full speech burst forces folding and speech omission while staying valid.
        budget = full_bytes - 1000
        req = store.agents()[0].prepare(store, store.world(), 0, 0, budget)
        self.assertLessEqual(sum(len(m["content"].encode()) + 12 for m in req.messages), budget)
        self.assertTrue(req.compressed or req.frame.get("omitted_events", 0) > 0)

    def test_api_token_keys_and_server_metrics_are_independent(self):
        api = ChatAPI("http://localhost:8000/v1", "test", tokenizer_path="/tokenize", mode="json")
        req = Request("r001", [{"role": "user", "content": "hi"}], {}, -1, [], False)
        responses = [
            {"tokens": [10, 20, 30]},
            {
                "choices": [{"message": {"content": '{"action":"wait"}'}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 20},
                },
            },
        ]
        with patch.object(api, "post", side_effect=responses):
            self.assertEqual(api.key(req), (10, 20, 30))
            reply, metrics = api.decide(req)
        self.assertEqual(reply, '{"action":"wait"}')
        self.assertEqual(metrics["cached_tokens"], 20)
        self.assertEqual(metrics["prompt_tokens"], 30)

    def test_compression_uses_only_delivered_evidence(self):
        store = self.open_store(12)

        class Speaker(Scripted):
            def decide(self, req):
                return encode(
                    {"action": "wait", "speech": f"msg-{req.actor}-{req.frame['view']['tick']}"}
                ), {}

        sim = Simulation(store, Speaker(), window=2, keep=0)
        sim.step()
        sim.step()
        with patch("herd.runtime.memory.compress", wraps=compress) as spy:
            req = sim.agents[0].prepare(store, store.world(), 1, 0, 180000)
        self.assertTrue(req.compressed)
        prev, delivered = spy.call_args.args
        self.assertEqual(prev, [])
        self.assertTrue(len(delivered) > 0)
        for ev in delivered:
            self.assertIn(ev["tick"], (0, 1))

    def test_three_attempts_send_validation_feedback_only_on_retries(self):
        store = self.open_store(12)
        api = ChatAPI("http://localhost/v1", "test", name="format-test", mode="json")
        sim = Simulation(store, api)
        req = sim.agents[0].prepare(store, store.world(), 0, 4, 180000)
        wrong = ['{"action":"give","qty":2}', '{"action":"wait","whisper":"single field"}']
        replies = [
            {"choices": [{"message": {"content": reply}}]} for reply in [*wrong, '{"action":"wait"}']
        ]
        with patch.object(api, "post", side_effect=replies) as calls:
            reply, metrics = sim.decision(req)
        self.assertEqual(json.loads(reply), {"action": "wait"})
        self.assertEqual(
            (metrics["attempts"], metrics["profile"], metrics["model"]), (3, "format-test", "test")
        )
        bodies = [c.args[1] for c in calls.call_args_list]
        self.assertEqual(
            [b["response_format"]["type"] for b in bodies], ["json_object", "json_schema", "json_object"]
        )
        self.assertEqual(bodies[0]["messages"], req.messages)
        for body, previous in zip(bodies[1:], wrong):
            self.assertEqual(body["messages"][:-2], req.messages)
            self.assertEqual(body["messages"][-2], {"role": "assistant", "content": previous})
            self.assertEqual(body["messages"][-1]["role"], "user")
        self.assertEqual(store.staged(0), set())

    def test_replay_checks_original_archive_and_tick_bounds(self):
        store = self.open_store(12)
        simulation = Simulation(store, Scripted())
        self.assertTrue(simulation.step())
        self.assertFalse(simulation.step(2))
        self.assertEqual(store.replay()["verified_ticks"], 1)
        data = json.loads(store.db.execute("SELECT data FROM world").fetchone()[0])
        cases = [
            ("UPDATE world SET data=?", (encode(dict(data, unexpected=True)),), "world state"),
            ("UPDATE world SET tick=2", (), "world tick"),
            ("UPDATE turns SET tick=2 WHERE tick=1", (), "outside replay interval"),
            ("UPDATE turns SET tick=-1 WHERE tick=1", (), "outside replay interval"),
            ("INSERT INTO events(tick) VALUES (1)", (), "outside replay interval"),
            ("INSERT INTO events(tick) VALUES (-1)", (), "outside replay interval"),
        ]
        for sql, parameters, error in cases:
            with self.subTest(sql=sql):
                store.db.execute(sql, parameters)
                try:
                    with self.assertRaisesRegex(ValueError, error):
                        store.replay()
                finally:
                    store.db.rollback()
        self.assertEqual(store.replay()["verified_ticks"], 1)

    def test_hysteresis_batch_fold_avoids_per_tick_thrashing(self):
        store = self.open_store(8)
        sim = Simulation(store, Scripted(), window=0, keep=2, context_bytes=180000)
        for _ in range(8):
            sim.step()
        agent = sim.agents[0]
        full = agent.prepare(store, store.world(), 0, 2, 180000, mode="agent_loop")
        from herd.runtime.memory import message_bytes

        full_bytes = message_bytes(full.messages)
        # Budget just below the full 8-turn prompt triggers batch folding down to watermark / keep.
        budget = full_bytes - 200
        folded = agent.prepare(store, store.world(), 0, 2, budget, mode="agent_loop")
        self.assertTrue(folded.compressed)
        folded_bytes = message_bytes(folded.messages)
        self.assertLessEqual(folded_bytes, budget)
        # Batch folding should fold more than 1 turn so subsequent ticks have append-only headroom.
        self.assertGreater(folded.cut, 0)
        self.assertLess(folded_bytes, budget - 800)

        # Commit this folded turn and advance one tick; next turn must NOT re-compress and must stay append-only.
        sim2 = Simulation(store, Scripted(), window=0, keep=2, context_bytes=budget)
        sim2.step()
        next_req = agent.prepare(store, store.world(), 0, 2, budget, mode="agent_loop")
        self.assertFalse(next_req.compressed)
        self.assertEqual(next_req.cut, store.mind(agent.actor)["cut"])

    def test_mru_prefix_queue_reuses_most_recent_continuations_first(self):
        # r000 has a longer prefix than r001, but r001 finished more recently (last in anchors).
        anchors = [b"sys|r000|long_history_0000000000", b"sys|r001|short"]
        items = [
            (b"sys|r000|long_history_0000000000|turn2", Request("r000", [], {}, -1, [], False)),
            (b"sys|r001|short|turn2", Request("r001", [], {}, -1, [], False)),
            (b"sys|r002|cold", Request("r002", [], {}, -1, [], False)),
        ]
        queue = PrefixQueue(items, anchors, mru=True)
        order = [queue.pop()[0].actor for _ in range(3)]
        self.assertEqual(order, ["r001", "r000", "r002"])

    def test_compact_loop_frame_preserves_prefix_and_omits_redundant_fields(self):
        store = self.open_store(8)
        sim = Simulation(store, Scripted(), window=0, keep=2, context_bytes=180000)
        agent = sim.agents[0]
        req0 = agent.prepare(store, store.world(), 0, 2, 180000, mode="agent_loop")
        other = sim.agents[-1].prepare(store, store.world(), 0, 2, 180000, mode="agent_loop")
        # All residents share 100% identical system prompts in agent_loop mode for maximum KV sharing.
        self.assertEqual(req0.messages[0], other.messages[0])
        sys_text = req0.messages[0]["content"]
        self.assertIn("force_modes=", sys_text)
        self.assertIn("item_catalog=", sys_text)
        first_id_line, first_frame_line = req0.messages[1]["content"].split("\n", 1)
        self.assertIn("role_brief", json.loads(first_id_line))
        self.assertIn("identity", json.loads(first_id_line))
        first_user_payload = json.loads(first_frame_line)
        self.assertNotIn("force_modes", first_user_payload["view"])
        self.assertNotIn("item_info", first_user_payload["view"])
        for peer in first_user_payload["view"].get("near", []):
            self.assertFalse(any(v is False or v == [] for v in peer.values()))
        sim.step()
        req1 = agent.prepare(store, store.world(), 0, 2, 180000, mode="agent_loop")
        # Append-only prefix invariance must hold byte-for-byte in agent_loop mode too.
        self.assertTrue(message_key(req1.messages).startswith(message_key(req0.messages)))
        second_user_payload = json.loads(req1.messages[-1]["content"])
        # Unchanged contacts/notes are omitted on subsequent frames.
        if req1.frame["view"]["contacts"] == req0.frame["view"]["contacts"]:
            self.assertNotIn("contacts", second_user_payload["view"])
        if req1.frame["notes"] == req0.frame["notes"]:
            self.assertNotIn("notes", second_user_payload)


class JsonIntentProtocolTests(PolityCase):
    def test_resident_prompt_uses_the_protocol_of_its_backend(self):
        store = self.store()
        agent = store.agents()[0]
        world = store.world()
        prompts = {
            mode: agent.prepare(store, world, 0, 0, 24000, mode=mode).messages[0]["content"]
            for mode in ("json", "agent_loop")
        }
        for prompt in prompts.values():
            self.assertIn(WORLD_RULES, prompt)
        for field in ("note_key", "whisper_to", "qty", "target", "detail"):
            self.assertIn(field, prompts["json"])
            self.assertNotIn(field, prompts["agent_loop"])
        self.assertIn("stage_whisper", prompts["agent_loop"])
        self.assertNotIn("stage_whisper", prompts["json"])

    def test_chat_api_retry_schema_accepts_polity_fields(self):
        store = self.store()
        req = store.agents()[0].prepare(store, store.world(), 4, 1, 24000)
        api = ChatAPI("http://localhost/v1", "test", mode="json")
        example = dict(
            action="write",
            target="keep",
            item="badge",
            qty="999",
            detail="a note",
            whisper_to="r001",
            whisper="question",
            note_key="x",
            note="y",
        )
        with patch.object(api, "_complete", return_value=(encode(example), {})) as post:
            reply, _ = api.retry_format(req)
        schema = post.call_args.args[1]["json_schema"]["schema"]
        fields = schema["properties"]
        self.assertEqual(
            set(fields), {"id", "action", "target", "speech", "note_key", "note", *POLITY_FIELDS}
        )
        self.assertEqual(set(fields["action"]["enum"]), set(ACTIONS))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["action"])
        self.assertNotIn("allOf", schema)
        self.assertEqual(Intent.parse(reply, req.actor, req.frame["view"]).qty, "999")
        for qty in ("1", "00001", "999", ""):
            self.assertRegex(qty, fields["qty"]["pattern"])
            Intent.parse(encode(dict(action="wait", qty=qty)))
        for invalid in (
            {"qty": "1000"},
            {"qty": "0"},
            {"qty": "-1"},
            {"qty": 2},
            {"whisper": "alone"},
            {"whisper_to": "r001"},
            {"offer": "fair"},
        ):
            with self.assertRaises(ValueError):
                Intent.parse(encode(dict(action="wait", **invalid)))
        local = self.store(name="retry_check", count=4)

        class BadFirst(Scripted):
            def decide(self, req):
                return encode(dict(action="wait", qty="0")), {}

            def retry_format(self, req):
                return encode(dict(action="wait")), {}

        Simulation(local, BadFirst()).step()
        self.assertTrue(
            all(json.loads(r[0])["attempts"] == 2 for r in local.db.execute("SELECT metrics FROM turns"))
        )
