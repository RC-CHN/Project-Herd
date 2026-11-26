from copy import deepcopy

from herd.model import Intent
from experiments.metrics.iemp import political
from tests.support import PolityCase, world, turn


class GatesTests(PolityCase):
    def test_gate_uses_physical_majority_instead_of_single_veto(self):
        w = world()
        traveller = "r000"
        w.people[traveller].inv["badge"] = w.places["atrium"].items.pop("badge")
        guards = ("r001", "r002", "r003", "r004")
        for actor in guards:
            w.people[actor].inv["tile"] = 1
        w.people["r002"].place = "annex"

        def orders(num_passes):
            return {
                actor: Intent("guard", "path:*", detail="seal:badge" if i < num_passes else "")
                for i, actor in enumerate(guards)
            }

        for passes, blocked in ((3, False), (1, True), (2, False)):
            turn(w, orders(passes))
            with self.subTest(passes=passes):
                self.assertEqual(
                    w.observe(traveller)["guards"]["paths"]["annex"]["blocked_for_self"], blocked
                )
                self.assertEqual(traveller in political(w)["trapped"], blocked)
                moving = deepcopy(w)
                turn(moving, {**orders(passes), traveller: Intent("move", "annex")})
                self.assertEqual(moving.people[traveller].place, "atrium" if blocked else "annex")

        # Physical control depends on strength as well as headcount.
        w.people["r001"].inv.pop("tile")
        w.people["r002"].inv.pop("tile")
        turn(w, orders(2))
        self.assertTrue(w.observe(traveller)["guards"]["paths"]["annex"]["blocked_for_self"])

    def test_guard_known_pass_permits_contacts_only(self):
        from herd.model import PassRule

        w = world()
        w.people["r007"].contacts = ["r000"]
        rule = PassRule.parse(
            "seal:badge;known;allow:r002", valid_actors=set(w.people), items=w.spec["items"]
        )
        self.assertTrue(rule.allow_known)
        self.assertEqual(rule.allow_ids, {"r002"})
        self.assertEqual(PassRule.parse(rule.raw), rule)
        for bad in ("allow:", "allow:r002,", "seal:a b", "unknown", "known;known"):
            with self.subTest(rule=bad), self.assertRaises(ValueError):
                PassRule.parse(bad)
        for bad in ("known:yes", "known;known", "allow:missing", "seal:tile", "seal:badge;seal:badge"):
            with self.assertRaises(ValueError):
                PassRule.parse(bad, valid_actors=set(w.people), items=w.spec["items"])
        turn(w, {"r007": Intent("guard", "path:*", detail="known")})
        self.assertFalse(w.observe("r000")["guards"]["paths"]["annex"]["blocked_for_self"])
        self.assertTrue(w.observe("r001")["guards"]["paths"]["annex"]["blocked_for_self"])
        moves = {actor: Intent("move", "annex") for actor in ("r000", "r001", "r002", "r003")}
        w.people["r003"].inv["badge"] = w.places["atrium"].items.pop("badge")
        turn(w, {**moves, "r007": Intent("guard", "path:*", detail=rule.raw)})
        self.assertEqual([w.people[a].place for a in moves], ["annex", "atrium", "annex", "annex"])

    def test_seal_and_signed_pass_remain_physical_credentials(self):
        w = world()
        turn(w, {"r000": Intent("take", item="badge")})
        turn(w, {"r000": Intent("write", detail="pass", item="badge")})
        turn(w, {"r000": Intent("give", "r001", item="d1-r000")})
        actors = ("r000", "r001", "r002", "r003")
        turn(
            w,
            {
                "r004": Intent("guard", "path:*", detail="seal:badge;allow:r003"),
                **{actor: Intent("move", "annex") for actor in actors},
            },
        )
        self.assertEqual([w.people[a].place for a in actors], ["annex", "annex", "atrium", "annex"])

    def test_directional_path_guard_blocks_entry_not_exit_and_items_guard_protects_floor(self):
        w = world()
        w.people["r001"].place = "annex"
        # Guarding path:annex from inside atrium blocks annex -> atrium entry, not atrium -> annex exit.
        turn(w, {"r000": Intent("guard", "path:annex", detail="none")})
        self.assertTrue(w.observe("r001")["guards"]["paths"]["atrium"]["blocked_for_self"])
        self.assertFalse(w.observe("r002")["guards"]["paths"]["annex"]["blocked_for_self"])
        self.assertEqual(w.observe("r002")["guards"]["paths"]["annex"]["count"], 0)
        self.assertEqual(w.observe("r002")["guards"]["paths"]["annex"]["inbound_size"], 1)
        turn(
            w,
            {
                "r000": Intent("guard", "path:annex", detail="none"),
                "r001": Intent("move", "atrium"),
                "r002": Intent("move", "annex"),
            },
        )
        self.assertEqual(w.people["r001"].place, "annex")
        self.assertEqual(w.people["r002"].place, "annex")

        # Guarding items blocks unauthorized floor takes without blocking doorways.
        w2 = world()
        w2.people["r000"].contacts = ["r003"]
        turn(w2, {"r000": Intent("guard", "items", detail="known")})
        self.assertFalse(w2.observe("r003")["guards"]["items"]["blocked_for_self"])
        self.assertTrue(w2.observe("r004")["guards"]["items"]["blocked_for_self"])
        self.assertFalse(w2.observe("r004")["guards"]["paths"]["annex"]["blocked_for_self"])
        events = turn(
            w2,
            {
                "r000": Intent("guard", "items", detail="known"),
                "r003": Intent("take", item="ration"),
                "r004": Intent("take", item="ration"),
                "r005": Intent("move", "annex"),
            },
        )
        self.assertEqual(w2.people["r003"].inv["ration"], 3)
        self.assertEqual(w2.people["r004"].inv["ration"], 2)
        self.assertTrue(any(e[0] == "r004" and "blocked from taking items" in e[4] for e in events))
        self.assertEqual(w2.people["r005"].place, "annex")

        # Full room lockdown (path:*) protects floor items AND blocks exits bidirectionally.
        w3 = world()
        events3 = turn(
            w3,
            {
                "r000": Intent("guard", "path:*", detail="none"),
                "r001": Intent("take", item="ration"),
                "r002": Intent("move", "annex"),
            },
        )
        self.assertEqual(w3.people["r001"].inv["ration"], 2)
        self.assertTrue(any(e[0] == "r001" and "blocked from taking items" in e[4] and "r000(" in e[4] for e in events3))
        self.assertTrue(any(e[0] == "r002" and "blocked on path to annex" in e[4] and "r000(" in e[4] for e in events3))
        self.assertTrue(
            any(e[0] == "r000" and e[2] == "notice" and "拦阻记录" in e[4] and "r001(" in e[4] and "ration" in e[4] for e in events3)
        )
        self.assertTrue(
            any(e[0] == "r000" and e[2] == "notice" and "拦阻记录" in e[4] and "r002(" in e[4] and "atrium→annex" in e[4] for e in events3)
        )
        self.assertEqual(w3.people["r002"].place, "atrium")

        # Escort guard receives notice when protected peer leaves the room; failed whispers notify sender.
        w4 = world()
        w4.people["r003"].place = "annex"
        ev4 = turn(
            w4,
            {
                "r000": Intent("guard", "person:r001", detail="none"),
                "r001": Intent("move", "annex"),
                "r002": Intent("wait", whisper_to="r003", whisper="hello?"),
            },
        )
        self.assertIsNone(w4.people["r000"].posture)
        self.assertTrue(any(e[0] == "r000" and e[2] == "notice" and "护卫解除" in e[4] and "r001(" in e[4] for e in ev4))
        self.assertTrue(any(e[0] == "r002" and e[2] == "result" and "whisper to r003 failed" in e[4] for e in ev4))

    def test_persistent_guard_survives_in_place_actions_and_drops_on_drop_guard_move_or_force(self):
        from herd.runtime.agent.toolbox import AgentToolbox
        from herd.runtime.memory import Request, render_tool_history

        w = world()
        w.people["r001"].place = "annex"
        # 1. Set guard on T0; on T1 r000 eats in place and guard stays active, still blocking r001!
        turn(w, {"r000": Intent("guard", "path:annex", detail="none")})
        self.assertIsNotNone(w.people["r000"].posture)
        turn(w, {"r000": Intent("eat"), "r001": Intent("move", "atrium")})
        self.assertIsNotNone(w.people["r000"].posture)
        self.assertEqual(w.people["r001"].place, "annex")

        # 2. AgentToolbox drop_guard tool maps to canonical guard(target='none') and renders back as drop_guard.
        req = Request("r000", [], {"view": w.observe("r000"), "events": [], "notes": {}}, -1, [], False)
        box = AgentToolbox(req, store=None)
        res = box.call("drop_guard", {})
        self.assertTrue(res["ok"], res)
        self.assertEqual((box.intent.action, box.intent.target), ("guard", "none"))
        hist = render_tool_history(2, '{"action":"guard","target":"none"}')
        self.assertEqual(hist[0]["tool_calls"][0]["function"]["name"], "drop_guard")

        # 3. Applying drop_guard clears posture and lets r001 enter.
        ev = turn(w, {"r000": box.intent, "r001": Intent("move", "atrium")})
        self.assertIsNone(w.people["r000"].posture)
        self.assertIn(("r000", 2, "result", "r000", "dropped guard"), ev)
        self.assertEqual(w.people["r001"].place, "atrium")

        # 4. Calling drop_guard when not guarding fails cleanly in toolbox.
        req_idle = Request("r000", [], {"view": w.observe("r000"), "events": [], "notes": {}}, -1, [], False)
        self.assertFalse(AgentToolbox(req_idle, store=None).call("drop_guard", {})["ok"])

        # 5. Moving or forcing automatically drops persistent guard.
        turn(w, {"r000": Intent("guard", "items", detail="none")})
        self.assertIsNotNone(w.people["r000"].posture)
        turn(w, {"r000": Intent("move", "annex")})
        self.assertIsNone(w.people["r000"].posture)
        turn(w, {"r000": Intent("guard", "items", detail="none")})
        self.assertIsNotNone(w.people["r000"].posture)
        turn(w, {"r000": Intent("force", "path:atrium")})
        self.assertIsNone(w.people["r000"].posture)
