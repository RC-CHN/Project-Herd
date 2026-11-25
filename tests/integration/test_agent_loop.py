"""Native tool calls through the normal simulation, barrier, replay and audit paths."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

from experiments.report_polity import report
from herd.model import Intent, encode
from herd.runtime.agent import AgentToolbox
from herd.runtime.backends import ChatAPI, RoutedBackend
from herd.runtime.simulation import Simulation
from tests.support import Decisions, PolityCase, fixture


def tool_call(name, **arguments):
    return {
        "type": "function",
        "function": {"name": name, "arguments": encode(arguments)},
    }


class OfflineChat(ChatAPI):
    """Mock only the network. Exercise ChatAPI's real request and usage handling."""

    def __init__(self, respond, **kwargs):
        super().__init__("http://offline.invalid/v1", "offline-model", **kwargs)
        self.respond = respond
        self.calls = []

    def post(self, url, body):
        assert body.get("tools") and "response_format" not in body
        messages = body["messages"]
        # Previous turns contain a flat intent. Only the current turn has tool calls.
        step = sum(bool(m.get("tool_calls")) for m in messages)
        actor = json.loads(messages[1]["content"].split("\n")[0])["id"]
        self.calls.append((actor, step))
        calls = self.respond(actor, step, messages)
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [dict(call, id=f"call-{step}-{i}") for i, call in enumerate(calls)],
        }
        return {
            "choices": [{"message": message, "finish_reason": "tool_calls"}],
            "usage": {
                "prompt_tokens": 9,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 2},
            },
        }


def route_actor(backend, actor):
    others = Decisions(lambda actor, view: {"action": "wait"})
    return RoutedBackend(
        others.name, {b.name: b for b in (others, backend)}, [{"actors": [actor], "profile": backend.name}]
    )


class AgentLoopTests(PolityCase):
    def test_agent_loop_observe_feedback_decide_multi_step(self):
        store = self.store(count=4)
        reader, author = "r000", "r001"
        doc_id = "d0-" + author

        def prepare_board(actor, view):
            if view["tick"] == 0 and actor == author:
                return {"action": "write", "target": "post", "detail": "Book club", "speech": "Meet here."}
            return {"action": "wait"}

        seed = Simulation(store, Decisions(prepare_board))
        seed.step()
        seed.step()  # archive delivery of the author's speech to the reader
        operations = [
            {"op": "write", "key": "draft", "text": "reread"},
            {"op": "write", "key": "meeting", "text": "annex"},
            {"op": "delete", "key": "draft"},
        ]
        script = [
            [tool_call("inspect_room")],
            [tool_call("read_document", doc_id=doc_id)],
            [tool_call("recall_person", actor_id=author)],
            [
                tool_call("write_note", key="draft", text="reread"),
                tool_call("write_note", key="meeting", text="annex"),
                tool_call("delete_note", key="draft"),
            ],
            [tool_call("stage_whisper", targets=[author], text="See you there.")],
            [tool_call("move", place="annex")],
        ]
        feedback = []

        def respond(actor, step, messages):
            self.assertEqual(actor, reader)
            if step:
                new = messages[-len(script[step - 1]) :]
                self.assertEqual(
                    [m["tool_call_id"] for m in new], [f"call-{step - 1}-{i}" for i in range(len(new))]
                )
                results = [json.loads(m["content"]) for m in new]
                self.assertTrue(all(m["role"] == "tool" for m in new))
                self.assertTrue(all(r["ok"] for r in results), results)
                feedback.append([r["result"] for r in results])
            return script[step]

        api = OfflineChat(respond)
        router = route_actor(api, reader)
        self.assertFalse(Simulation(store, router).step(1))
        self.assertEqual(store.mind(reader)["notes"], {})
        self.assertEqual(store.world().tick, 2)
        self.assertTrue(Simulation(store, router).step())
        self.assertIn("atrium", encode(feedback[0]))
        doc = feedback[1][0]
        self.assertEqual((doc["id"], doc["text"], doc["signers"]), (doc_id, "Book club", [author]))
        self.assertIn("Meet here.", encode(feedback[2]))
        self.assertEqual(len(api.calls), 6)
        reply, raw = store.db.execute(
            "SELECT reply,metrics FROM turns WHERE tick=2 AND actor=?", (reader,)
        ).fetchone()
        intent = Intent.parse(reply, reader)
        self.assertEqual(
            (intent.action, intent.target, intent.whisper_to, intent.whisper),
            ("move", "annex", author, "See you there."),
        )
        metrics = json.loads(raw)
        self.assertEqual(metrics["note_ops"], operations)
        self.assertEqual(metrics["mode"], "agent_loop")
        self.assertEqual(metrics["tool_calls"], [c["function"]["name"] for step in script[:-1] for c in step])
        self.assertEqual(
            (metrics["loop_steps"], metrics["prompt_tokens"], metrics["cached_tokens"]), (6, 54, 12)
        )
        self.assertEqual(store.mind(reader)["notes"], {"meeting": {"tick": 2, "text": "annex"}})
        self.assertEqual(store.world().people[reader].place, "annex")
        self.assertEqual(
            [json.loads(e["detail"]) for e in store.events(reader, 2, 2) if e["kind"] == "private_note"],
            [["draft", "reread"], ["meeting", "annex"], ["draft", ""]],
        )
        self.assertEqual(store.replay()["verified_ticks"], 3)
        result = report(store.db)
        self.assertEqual(result["audit"]["note_ops"], 3)
        self.assertEqual(result["backend_attribution"]["by_profile"][api.name]["mean_loop_steps"], 6)
        # Even without a subsequent frame, the audit checks the closing private state.
        store.db.execute("UPDATE agents SET notes='{}' WHERE id=?", (reader,))
        try:
            with self.assertRaisesRegex(ValueError, "notes"):
                report(store.db)
        finally:
            store.db.rollback()

        # Changing a draft that was later deleted leaves closing notes identical.
        # Replay and audit must still detect the altered operation in the event sequence.
        metrics["note_ops"][0]["text"] = "changed after the turn"
        store.db.execute(
            "UPDATE turns SET metrics=? WHERE tick=2 AND actor=?", (encode(metrics), reader)
        )
        try:
            with self.assertRaisesRegex(ValueError, "observation mismatch"):
                store.replay()
            with self.assertRaisesRegex(ValueError, "event replay mismatch"):
                report(store.db)
        finally:
            store.db.rollback()

    def test_agent_loop_self_corrects_invalid_tool_call(self):
        store = self.store(count=2)

        def respond(actor, step, messages):
            if step:
                result = json.loads(messages[-1]["content"])
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
            return [
                [tool_call("move", place="nowhere")],
                [tool_call("take", item="ration", qty=1000)],
                [tool_call("take", item="ration", qty=1)],
            ][step]

        sim = Simulation(store, route_actor(OfflineChat(respond), "r000"))
        self.assertTrue(sim.step())
        reply, metrics = store.db.execute("SELECT reply,metrics FROM turns WHERE actor='r000'").fetchone()
        self.assertEqual(Intent.parse(reply), Intent("take", item="ration", qty="1"))
        self.assertEqual(json.loads(metrics)["loop_steps"], 3)
        self.assertEqual(store.world().people["r000"].inv["ration"], 3)

    def test_agent_loop_memory_tools_enforce_strict_local_provenance(self):
        spec = fixture()
        spec["rules"]["contact_size"] = 16
        spec["groups"]["visitors"]["home"] = "annex"
        store = self.store(spec, count=16)
        initial = store.world()
        room = [p.id for p in initial.people.values() if p.place == "atrium"]
        reader, sender, recipient = room[:3]
        remote = next(p.id for p in initial.people.values() if p.place == "annex")

        def talk(actor, view):
            if view["tick"]:
                return {"action": "wait"}
            reply = {"action": "wait", "speech": f"story-{actor} " + "a" * 220}
            if actor == sender:
                reply.update(whisper_to=recipient, whisper="private invitation")
            if actor == reader:
                reply.update(note_key="bookmark", note="favourite story")
            return reply

        Simulation(store, Decisions(talk)).step()
        resident = next(r for r in store.agents() if r.actor == reader)
        full = resident.prepare(store, store.world(), 1, 0, 180000)
        budget = sum(len(m["content"].encode()) + 12 for m in full.messages) - 600
        Simulation(store, Decisions(talk), window=1, keep=0, context_bytes=budget).step()
        archived = json.loads(store.history(reader, 0, 2)[0]["frame"])
        self.assertGreater(archived["omitted_events"], 0)
        delivered = {e["source"] for e in archived["events"] if e["kind"] == "speech"}
        omitted = next(a for a in room if a not in delivered)
        present = next(iter(delivered))
        req = resident.prepare(store, store.world(), 0, 0, 180000, mode="agent_loop")
        box = AgentToolbox(req, store)

        def lookup(name, **args):
            response = box.call(name, args)
            self.assertTrue(response["ok"], response)
            return encode(response["result"])

        def restrict_event_table(action, table, column, db, source):
            if action == sqlite3.SQLITE_READ and table == "events":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        # A future indexed shortcut into the global log must fail this test even
        # when the event it would return happens to have been delivered.
        store.db.set_authorizer(restrict_event_table)
        try:
            self.assertIn(f"story-{present}", lookup("recall_person", actor_id=present))
            self.assertIn("favourite story", lookup("search_memory", query="bookmark"))
            for resident_id in (omitted, remote):
                story = f"story-{resident_id}"
                self.assertNotIn(story, lookup("recall_person", actor_id=resident_id))
                self.assertNotIn(story, lookup("search_memory", query=story))
            self.assertNotIn("private invitation", lookup("search_memory", query="private invitation"))
        finally:
            store.db.set_authorizer(lambda *args: sqlite3.SQLITE_OK)

    def test_agent_loop_max_steps_falls_back_to_wait_preserving_speech(self):
        store = self.store(count=2)
        script = [
            [
                tool_call("stage_speech", text="Back soon."),
                tool_call("stage_whisper", targets=["r001"], text="Hold my book."),
            ],
            [tool_call("inspect_room")],
        ]
        api = OfflineChat(lambda actor, step, messages: script[step], max_steps=2)
        self.assertTrue(Simulation(store, route_actor(api, "r000")).step())
        reply, metrics = store.db.execute("SELECT reply,metrics FROM turns WHERE actor='r000'").fetchone()
        self.assertEqual(
            Intent.parse(reply),
            Intent("wait", speech="Back soon.", whisper_to="r001", whisper="Hold my book."),
        )
        self.assertTrue(json.loads(metrics)["fallback_wait"])
        self.assertEqual(json.loads(metrics)["loop_steps"], 2)
        self.assertEqual(store.replay()["verified_ticks"], 1)

    def test_store_thread_safe_under_concurrent_agent_loops(self):
        store = self.store(count=24)

        def respond(actor, step, messages):
            if step:
                self.assertTrue(all(json.loads(m["content"])["ok"] for m in messages[-2:]))
                return [tool_call("wait")]
            return [tool_call("recall_person", actor_id="r000"), tool_call("search_memory", query="report")]

        api = OfflineChat(respond, workers=6)
        second = OfflineChat(respond, workers=6, name="other")
        router = RoutedBackend(
            api.name, {b.name: b for b in (api, second)}, [{"profile": second.name, "random": 12}]
        )
        sim = Simulation(store, router)
        for _ in range(3):
            self.assertTrue(sim.step())
        self.assertEqual(len(api.calls) + len(second.calls), 24 * 3 * 2)
        self.assertEqual(store.stats()["by_model"]["offline-model"]["loop_steps"], 144)
        self.assertEqual(report(store.db)["audit"]["verified_ticks"], 3)
        self.assertEqual(store.replay()["verified_ticks"], 3)

        # A query from another worker must wait during a write transaction on
        # this connection. SQLite itself otherwise permits dirty reads here.
        started = Event()

        def read_history():
            started.set()
            return store.delivered_events("r000", 3)

        with ThreadPoolExecutor(1) as pool:
            with store._lock:
                future = pool.submit(read_history)
                self.assertTrue(started.wait(2))
                with self.assertRaises(TimeoutError):
                    future.result(timeout=0.05)
            self.assertTrue(future.result(timeout=2))

    def test_agent_loop_parallel_action_and_internal_calls_preserve_speech_and_notes(self):
        store = self.store(count=2)

        def respond(actor, step, messages):
            # Deliberately place the world action before speech and note writes in the same step.
            return [
                tool_call("move", place="annex"),
                tool_call("stage_speech", text="Heading to the annex."),
                tool_call("write_note", key="plan", text="check annex"),
            ]

        api = OfflineChat(respond)
        self.assertTrue(Simulation(store, route_actor(api, "r000")).step())
        reply, raw = store.db.execute("SELECT reply,metrics FROM turns WHERE actor='r000'").fetchone()
        self.assertEqual(Intent.parse(reply), Intent("move", target="annex", speech="Heading to the annex."))
        self.assertEqual(
            json.loads(raw)["note_ops"], [{"op": "write", "key": "plan", "text": "check annex"}]
        )
        self.assertEqual(store.mind("r000")["notes"], {"plan": {"tick": 0, "text": "check annex"}})
        self.assertEqual(store.replay()["verified_ticks"], 1)

    def test_agent_loop_rejects_multiple_world_actions_in_same_step(self):
        store = self.store(count=2)

        def respond(actor, step, messages):
            if step == 0:
                return [tool_call("move", place="annex"), tool_call("wait")]
            results = [json.loads(m["content"]) for m in messages[-2:]]
            self.assertTrue(all(not r["ok"] and "one world action" in r["error"] for r in results))
            self.assertEqual([r["remaining_steps"] for r in results], [2, 2])
            return [tool_call("move", place="annex")]

        api = OfflineChat(respond, max_steps=3)
        self.assertTrue(Simulation(store, route_actor(api, "r000")).step())
        reply, raw = store.db.execute("SELECT reply,metrics FROM turns WHERE actor='r000'").fetchone()
        self.assertEqual(Intent.parse(reply), Intent("move", target="annex"))
        self.assertEqual(json.loads(raw)["loop_steps"], 2)

    def test_agent_loop_strips_reasoning_content_and_reports_remaining_steps(self):
        store = self.store(count=2)

        class ReasoningChat(OfflineChat):
            def post(self, url, body):
                response = super().post(url, body)
                response["choices"][0]["message"]["reasoning_content"] = "private chain of thought"
                return response

        def respond(actor, step, messages):
            if step == 0:
                return [tool_call("inspect_room")]
            assistant_messages = [m for m in messages if m["role"] == "assistant"]
            self.assertTrue(all("reasoning_content" not in m for m in assistant_messages))
            feedback = json.loads(messages[-1]["content"])
            self.assertEqual(feedback["remaining_steps"], 1)
            self.assertIn("hint", feedback)
            return [tool_call("wait")]

        api = ReasoningChat(respond, max_steps=2)
        self.assertTrue(Simulation(store, route_actor(api, "r000")).step())
        self.assertEqual(len(api.calls), 2)

    def test_agent_loop_guard_requires_explicit_allow_pass(self):
        store = self.store(count=2)

        def respond(actor, step, messages):
            if step == 0:
                return [tool_call("guard", target="path:annex")]
            if step == 1:
                feedback = json.loads(messages[-1]["content"])
                self.assertFalse(feedback["ok"])
                self.assertIn("allow_pass", feedback["error"])
                return [tool_call("guard", target="path:annex", allow_pass="")]
            feedback = json.loads(messages[-1]["content"])
            self.assertFalse(feedback["ok"])
            self.assertIn("allow_pass", feedback["error"])
            return [tool_call("guard", target="path:annex", allow_pass="known")]

        api = OfflineChat(respond, max_steps=4)
        self.assertTrue(Simulation(store, route_actor(api, "r000")).step())
        reply, raw = store.db.execute("SELECT reply,metrics FROM turns WHERE actor='r000'").fetchone()
        self.assertEqual(Intent.parse(reply), Intent("guard", target="path:annex", detail="known"))
        self.assertEqual(json.loads(raw)["loop_steps"], 3)

    def test_agent_loop_guard_and_force_items_target(self):
        store = self.store(count=3)
        api_guard = OfflineChat(lambda actor, step, messages: [tool_call("guard", target="items", allow_pass="known")], name="g")
        api_force = OfflineChat(lambda actor, step, messages: [tool_call("force", target="items")], name="f")
        others = Decisions(lambda actor, view: {"action": "wait"})
        router = RoutedBackend(
            others.name,
            {others.name: others, "g": api_guard, "f": api_force},
            [{"actors": ["r000"], "profile": "g"}, {"actors": ["r001"], "profile": "f"}],
        )
        self.assertTrue(Simulation(store, router).step())
        self.assertEqual(store.replay()["verified_ticks"], 1)
        self.assertEqual(report(store.db)["audit"]["replay"], "passed")
