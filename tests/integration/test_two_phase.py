"""Two-phase ticks: same-tick deliberation, then actions, through barrier, replay and audit."""

import json

from experiments.report_polity import report
from herd.model import Intent
from herd.runtime.memory import SILENT_DELIBERATION
from herd.runtime.simulation import Simulation
from tests.integration.test_agent_loop import OfflineChat, tool_call
from tests.support import Decisions, PolityCase, fixture


def two_phase_fixture():
    spec = fixture()
    spec["rules"]["comm_rounds"] = 1
    return spec


def phase_of(messages):
    """The latest phase marker: the opening frame (comm) or the deliberation summary (action)."""
    for m in reversed(messages):
        if m["role"] == "user" and m["content"].startswith("{"):
            # messages[1] prefixes the resident identity line before its frame.
            return json.loads(m["content"].split("\n")[-1]).get("phase")
    return None


class TwoPhaseTests(PolityCase):
    def test_order_given_in_deliberation_is_acted_on_in_the_same_tick(self):
        store = self.store(two_phase_fixture(), count=2)
        leader, follower = "r000", "r001"
        seen = {}

        def respond(actor, step, messages):
            phase = phase_of(messages)
            seen.setdefault((actor, phase), []).append(messages)
            if phase == "comm" and actor == leader:
                return [tool_call("stage_speech", text="跟我去 annex")]
            if phase == "comm" and actor == follower:
                # A small model reaching for a world action during deliberation gets a phase error,
                # then stays silent.
                return [tool_call("move", place="annex")] if step == 0 else []
            if phase == "action" and actor == follower and len(seen[actor, phase]) == 1:
                heard = json.loads(messages[-1]["content"])["heard"]
                self.assertEqual([(e["kind"], e["source"], e["detail"]) for e in heard], [("speech", leader, "跟我去 annex")])
                return [tool_call("move", place="annex")]
            return [tool_call("wait")]

        api = OfflineChat(respond)
        sim = Simulation(store, api)
        self.assertTrue(sim.step())
        world = store.world()
        self.assertEqual((world.tick, world.people[follower].place), (1, "annex"))

        # The follower's deliberation: one phase error, then silence; nothing staged.
        comm_reply, comm_metrics = store.db.execute(
            "SELECT reply,metrics FROM comm_turns WHERE tick=0 AND actor=?", (follower,)
        ).fetchone()
        self.assertEqual(Intent.parse(comm_reply), Intent("wait"))
        metrics = json.loads(comm_metrics)
        self.assertEqual((metrics["phase"], metrics["phase_errors"], metrics["loop_steps"]), ("comm", 1, 2))
        error = json.loads(seen[follower, "comm"][1][-1]["content"])
        self.assertFalse(error["ok"])
        self.assertIn("商议阶段", error["error"])

        # Same-tick delivery is archived separately and not re-delivered as next tick's events.
        heard = store.db.execute(
            "SELECT viewer,kind,source,detail FROM comm_events WHERE tick=0 ORDER BY id"
        ).fetchall()
        self.assertIn((follower, "speech", leader, "跟我去 annex"), [tuple(r) for r in heard])
        self.assertFalse(
            [e for e in store.events(follower, 0, 0) if e["kind"] == "speech" and e["source"] == leader]
        )

        # The action prompt continues the deliberation prompt append-only.
        comm_prompt = seen[follower, "comm"][0]
        action_prompt = seen[follower, "action"][0]
        self.assertEqual(action_prompt[: len(comm_prompt)], comm_prompt)

        # Next tick: history re-renders the deliberation before the action.
        self.assertTrue(sim.step())
        history = seen[follower, "comm"][-1]
        contents = [m.get("content") for m in history]
        self.assertIn(SILENT_DELIBERATION, contents)
        leader_history = seen[leader, "comm"][-1]
        self.assertTrue(
            any(
                c["function"]["name"] == "stage_speech" and c["id"].startswith("c-0-")
                for m in leader_history
                for c in m.get("tool_calls", [])
            )
        )
        self.assertEqual(store.replay()["verified_ticks"], 2)
        result = report(store.db)
        self.assertEqual(result["audit"]["replay"], "passed")

    def test_deliberation_whisper_connects_and_replays(self):
        store = self.store(two_phase_fixture(), count=4)

        def respond(actor, step, messages):
            phase = phase_of(messages)
            if phase == "comm" and actor == "r000":
                return [
                    tool_call("write_note", key="plan", text="r001 守门"),
                    tool_call("stage_whisper", targets=["r001"], text="你守住门口"),
                ]
            if phase == "comm":
                return [tool_call("wait")]
            return [tool_call("wait")]

        sim = Simulation(store, OfflineChat(respond))
        self.assertTrue(sim.step())
        whispers = store.db.execute(
            "SELECT viewer,kind,source FROM comm_events WHERE kind IN ('whisper','whisper_sent')"
        ).fetchall()
        self.assertEqual(sorted(map(tuple, whispers)), [("r000", "whisper_sent", "r001"), ("r001", "whisper", "r000")])
        self.assertIn("r001", store.world().people["r000"].contacts)
        # Notes written while deliberating commit at the tick barrier with the action.
        self.assertEqual(store.mind("r000")["notes"], {"plan": {"tick": 0, "text": "r001 守门"}})
        self.assertTrue(sim.step())
        self.assertEqual(store.replay()["verified_ticks"], 2)
        self.assertEqual(report(store.db)["audit"]["replay"], "passed")

        # Tampering with a same-tick delivery is caught by replay.
        store.db.execute("UPDATE comm_events SET detail='篡改' WHERE kind='whisper'")
        try:
            with self.assertRaisesRegex(ValueError, "deliberation mismatch"):
                store.replay()
        finally:
            store.db.rollback()

    def test_json_adapters_stay_silent_in_deliberation_and_runs_resume_mid_tick(self):
        store = self.store(two_phase_fixture(), count=6)

        def choose(actor, view):
            return {"action": "wait", "speech": f"{actor} 在此"} if view["tick"] % 2 else {"action": "wait"}

        sim = Simulation(store, Decisions(choose))
        # A budget that ends inside the deliberation, then exactly at its barrier.
        self.assertFalse(sim.step(4))
        self.assertFalse(store.comm_settled(0))
        self.assertFalse(sim.step(2))
        self.assertTrue(store.comm_settled(0))
        self.assertEqual(store.world().tick, 0)
        self.assertTrue(sim.step())
        self.assertTrue(sim.step())
        self.assertEqual(store.world().tick, 2)
        skipped = [json.loads(r[0]) for r in store.db.execute("SELECT metrics FROM comm_turns")]
        self.assertTrue(all(m["skipped"] for m in skipped))
        self.assertEqual(store.replay()["verified_ticks"], 2)
        self.assertEqual(report(store.db)["audit"]["replay"], "passed")

    def test_single_phase_archives_have_no_deliberation(self):
        store = self.store(count=3)
        sim = Simulation(store, Decisions(lambda actor, view: {"action": "wait", "speech": "hi"}))
        self.assertTrue(sim.step())
        self.assertEqual(store.db.execute("SELECT count(*) FROM comm_turns").fetchone()[0], 0)
        self.assertNotIn("comm_rounds", store.world().spec["rules"])
        self.assertEqual(store.replay()["verified_ticks"], 1)
