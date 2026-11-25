import unittest

from herd.model import Document, Intent, World, load
from tests.support import fixture, turn


class ClockTests(unittest.TestCase):
    def test_starvation_shock_and_item_drop(self):
        spec = load("citystate-coup")
        w = World.create(spec, None, 7)
        people = w.people
        a = next(iter(people))
        p = people[a]
        p.energy = 1
        p.inv = {"silver": 5}
        p.docs = {"paper": 2}
        w.documents["paper"] = Document(id="paper", author=a, tick=0, text="hi", signers=[], seal="")
        room = w.places[p.place]
        before = room.items.get("silver", 0)
        interval = spec["rules"]["clock_interval"]
        storm_shock = next(s for s in spec["shocks"] if s["kind"] == "regen_multiplier")
        storm_tick = storm_shock["tick"]
        post_storm_rate = int(10 * storm_shock["multiplier"])
        cold_shock = next(s for s in spec["shocks"] if s["kind"] == "energy_decay_set")
        cold_tick, cold_decay = cold_shock["tick"], cold_shock["value"]
        base_decay = spec["rules"]["base_energy_decay"]
        # Ticks before the end of the natural day do not decay energy; the final tick closes the day.
        for step in range(interval - 1):
            self.assertEqual(w.observe(a)["ticks_to_decay"], interval - step)
            turn(w, {a: Intent("guard", "path:*")})
            self.assertEqual(p.energy, 1)
        self.assertEqual(w.observe(a)["ticks_to_decay"], 1)
        events = turn(w, {a: Intent("guard", "path:*")})
        self.assertIn((a, interval - 1, "death", a, "starvation"), events)
        self.assertEqual(room.items["silver"], before + 5)
        self.assertEqual(room.docs, {"paper": 2})
        self.assertIsNone(p.posture)
        self.assertNotIn(a, w.active_residents())
        for tick in range(interval, cold_tick + 1):
            events = turn(w, {})
            rate = w.places["port"].regen["grain"]["rate"]
            self.assertEqual(rate, 10 if tick < storm_tick else post_storm_rate)
            self.assertEqual(w.energy_decay, base_decay if tick < cold_tick else cold_decay)
            if tick in (storm_tick, cold_tick):
                recipients = {v for v, _, k, s, _ in events if k == "notice" and s == "weather"}
                expected = {
                    a
                    for a, p in people.items()
                    if p.is_alive and (tick == cold_tick or p.place in ("port", "plaza"))
                }
                self.assertEqual(recipients, expected)
        spec = fixture()
        spec["shocks"] = [
            dict(tick=0, place="annex", kind="drop_doc", source="desk", text="message", announce_places=[]),
            dict(
                tick=0,
                place="atrium",
                kind="items_delta",
                item="ration",
                delta=-99,
                source="desk",
                text="loss",
                announce_places=["atrium"],
            ),
        ]
        w = World.create(spec, 4, 1)
        turn(w, {})
        self.assertNotIn("ration", w.places["atrium"].items)
        self.assertEqual(w.documents[w.places["annex"].board[0]].text, "message")

    def test_clock_removes_zero_stock_after_renewal_and_items_delta(self):
        spec = fixture()
        spec["rules"]["clock_interval"] = 2
        spec["places"]["atrium"].update(
            initial_items={"ration": 8, "token": 2},
            regen={"ration": {"rate": 0, "cap": 12}, "tile": {"rate": 0, "cap": 10}},
        )
        spec["shocks"] = [
            dict(
                tick=0,
                kind="items_delta",
                item="ration",
                delta=-99,
                place="atrium",
                source="desk",
                text="loss",
                announce_places=[],
            ),
            dict(
                tick=1,
                kind="items_delta",
                item="token",
                delta=-2,
                place="atrium",
                source="desk",
                text="exchange",
                announce_places=[],
            ),
        ]
        simulation = World.create(spec, 4, 7)
        room = simulation.places["atrium"]
        turn(simulation, {})
        self.assertEqual(room.items, {"token": 2})
        turn(simulation, {})
        self.assertEqual(room.items, {})
        self.assertEqual(simulation.observe("r000")["items"], {})
        self.assertEqual(simulation.to_dict()["places"]["atrium"]["items"], {})

    def test_well_fed_recovery_and_exertion_energy_cost(self):
        spec = fixture()
        spec["rules"]["clock_interval"] = 2
        spec["rules"]["base_energy_decay"] = 1
        spec["rules"]["exertion_energy_cost"] = 1
        spec["rules"]["carry_cap"] = 10
        spec["items"]["ration"]["weight"] = 4
        spec["places"]["atrium"]["initial_items"] = {"ration": 5}
        spec["places"]["annex"]["initial_items"] = {"ration": 5}
        w = World.create(spec, 4, 7)
        # r000 is well-fed and wounded; r001 is hungry and wounded.
        w.people["r000"].health, w.people["r000"].energy = 7, 14
        w.people["r001"].health, w.people["r001"].energy = 7, 6
        # r002 harvests at annex (exertion -1); r003 moves heavy (>5 weight, exertion -1).
        w.people["r002"].place, w.people["r002"].energy, w.people["r002"].inv = "annex", 10, {}
        w.people["r003"].energy, w.people["r003"].inv = 10, {"ration": 2}
        turn(w, {"r002": Intent("produce"), "r003": Intent("move", "annex")})
        self.assertEqual(w.people["r002"].energy, 9)
        self.assertEqual(w.people["r003"].energy, 9)
        # Light move costs 0 exertion.
        w.people["r003"].inv = {}
        turn(w, {"r003": Intent("move", "atrium")})
        # Day 1 closed on tick 1: decay=1 -> r000 energy=13 (>6) heals +1; r001 energy=5 (<=6) does not heal.
        self.assertEqual(w.people["r000"].health, 8)
        self.assertEqual(w.people["r001"].health, 7)
        self.assertEqual(w.people["r003"].energy, 8)

    def test_diurnal_cycle_and_night_whisper_visibility(self):
        from herd.model import diurnal

        expected = ["黎明", "上午", "上午", "正午", "下午", "下午", "黄昏", "夜晚", "深夜"]
        self.assertEqual([diurnal(t, 9) for t in range(9)], [(1, label, i >= 7) for i, label in enumerate(expected)])
        self.assertEqual(diurnal(9, 9), (2, "黎明", False))
        self.assertEqual(diurnal(0, 1), (1, "全日", False))

        spec = fixture()
        spec["rules"].update(
            clock_interval=9,
            whisper_detect_base=1.0,
            whisper_detect_min=0.0,
            whisper_detect_max=1.0,
            night_whisper_multiplier=0.0,
        )
        w = World.create(spec, 4, 7)
        # Tick 0 is 黎明 (daytime): bystander detects whisper.
        self.assertEqual((w.observe("r000")["day"], w.observe("r000")["time_of_day"]), (1, "黎明"))
        day_events = turn(w, {"r000": Intent("wait", whisper_to="r001", whisper="day chat")})
        self.assertTrue(any(e[2] == "spotted_whisper" for e in day_events))
        # Advance to Tick 7 (夜晚): night_whisper_multiplier=0.0 hides whisper from bystanders.
        w.tick = 7
        self.assertEqual((w.observe("r000")["day"], w.observe("r000")["time_of_day"]), (1, "夜晚"))
        night_events = turn(w, {"r000": Intent("wait", whisper_to="r001", whisper="night chat")})
        self.assertFalse(any(e[2] == "spotted_whisper" for e in night_events))

    def test_exhaustion_mode_incapacitation_self_rescue_and_kv_safe_tools(self):
        from herd.runtime.agent.schemas import TOOL_SCHEMAS
        from herd.runtime.agent.toolbox import AgentToolbox
        from herd.runtime.memory import Request

        spec = fixture()
        spec["rules"].update(
            clock_interval=1,
            base_energy_decay=2,
            write_energy_cost=3,
            exertion_energy_cost=1,
            zero_energy_mode="exhaust",
            starvation_health_loss=0,
            collective_starvation_health_loss=2,
        )
        spec["places"]["atrium"]["initial_items"] = {"ration": 2}
        spec["groups"]["crowd"] = {**spec["groups"]["circle"], "size": 10, "discipline": 0.4, "exact_count": 1}
        w = World.create(spec, 4, 7)
        indiv, helper = [p for p in w.people.values() if not p.is_collective][:2]
        crowd = next(p for p in w.people.values() if p.is_collective)
        indiv.energy = 1
        indiv.inv = {"token": 2}
        helper.energy = 10
        crowd.energy = 1
        crowd.health = 2

        # Opening view discloses energy costs and zero_energy_mode.
        v0 = w.observe(indiv.id)
        self.assertEqual(v0["write_energy_cost"], 3)
        self.assertEqual(v0["exertion_energy_cost"], 1)
        self.assertEqual(v0["zero_energy_mode"], "exhaust")
        self.assertFalse(v0["exhausted"])
        self.assertNotIn("status_hint", v0)

        # Tick 0: indiv guards, then clock decays energy -> 0: indiv enters exhaustion (alive!), crowd disperses.
        events = turn(w, {indiv.id: Intent("guard", "path:*")})
        self.assertTrue(indiv.is_alive)
        self.assertTrue(indiv.is_exhausted)
        self.assertEqual(indiv.energy, 0)
        self.assertEqual(indiv.health, spec["rules"]["max_health"])
        self.assertIsNone(indiv.posture)
        self.assertIn(indiv.id, w.active_residents())
        self.assertTrue(any(e[0] == indiv.id and e[2] == "notice" and "力竭失能" in e[4] for e in events))
        self.assertTrue(any(e[0] == helper.id and e[2] == "notice" and "目击力竭" in e[4] for e in events))
        self.assertFalse(crowd.is_alive)
        self.assertTrue(any(e[0] == crowd.id and e[2] == "death" and e[4] == "starvation" for e in events))

        # Exhausted view shows exhausted=True, status_hint, and peer sees exhausted=True in near.
        ve = w.observe(indiv.id)
        self.assertTrue(ve["exhausted"])
        self.assertIn("自救方式", ve["status_hint"])
        helper_near = {p["id"]: p for p in w.observe(helper.id)["near"]}
        self.assertTrue(helper_near[indiv.id]["exhausted"])

        # KV-safe tool check: TOOL_SCHEMAS stays identical, while calling blocked tools returns ok=False.
        req = Request(indiv.id, [], {"view": ve, "events": [], "notes": {}}, -1, [], False)
        box = AgentToolbox(req, None)
        self.assertGreater(len(TOOL_SCHEMAS), 0)
        for bad_name, bad_args in (
            ("move", {"place": "annex"}),
            ("guard", {"target": "path:*", "allow_pass": "none"}),
            ("force", {"target": f"person:{helper.id}"}),
            ("write_document", {"text": "help"}),
            ("give", {"target": helper.id, "item": "token", "qty": 1}),
        ):
            res = box.call(bad_name, bad_args)
            self.assertFalse(res["ok"], f"{bad_name} should fail when exhausted")
            self.assertIn("自救方式", res["error"])
        # Dropping to ground ('here') and take/wait stay allowed in toolbox.
        res_drop = box.call("give", {"target": "here", "item": "token", "qty": 1})
        self.assertTrue(res_drop["ok"])

        # Settlement engine also rejects blocked actions and allows take + eat self-rescue.
        ev_block = turn(w, {indiv.id: Intent("move", "annex", speech="救命，给我口吃的！")})
        self.assertEqual(indiv.place, "atrium")
        self.assertTrue(any(e[0] == indiv.id and e[2] == "result" and "exhausted" in e[4] for e in ev_block))
        self.assertTrue(any(e[0] == helper.id and e[2] == "speech" and "救命" in e[4] for e in ev_block))

        # Self-rescue: take ration from floor on Tick 2, eat on Tick 3 -> recovers from exhaustion!
        turn(w, {indiv.id: Intent("take", item="ration", qty="1")})
        self.assertEqual(indiv.inv.get("ration"), 1)
        ev_eat = turn(w, {indiv.id: Intent("eat", item="ration")})
        self.assertFalse(indiv.is_exhausted)
        self.assertGreater(indiv.energy, 0)
        self.assertTrue(any(e[0] == indiv.id and e[2] == "notice" and "体力恢复" in e[4] for e in ev_eat))
