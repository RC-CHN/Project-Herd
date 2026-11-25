from collections import Counter
from copy import deepcopy

from experiments.metrics.iemp import intellectual
from experiments.report_polity import report
from herd.model import Intent, World, load
from herd.runtime.backends import Scripted
from herd.runtime.memory import ResidentAgent
from herd.runtime.simulation import Simulation
from tests.support import Decisions, PolityCase, fixture, turn


def collective_spec():
    spec = fixture()
    spec["rules"]["carry_cap"] = 10
    spec["rules"]["clock_interval"] = 1
    spec["rules"]["base_energy_decay"] = 1
    spec["rules"]["contact_size"] = 12
    spec["items"]["ration"]["energy_restore"] = 4
    spec["ties"] = []
    spec["groups"] = {
        "leader": {
            "label": "Leader",
            "public_tag": "leader",
            "exact_count": 1,
            "home": "atrium",
            "brief": "Individual decision maker.",
            "initial_energy": 8,
            "initial_health": 10,
            "initial_inv": {"ration": 2},
            "archetypes": ["Calm strategist."],
        },
        "unit": {
            "label": "Unit",
            "public_tag": "unit",
            "exact_count": 2,
            "size": 4,
            "discipline": 0.95,
            "home": "atrium",
            "brief": "Organized formation.",
            "initial_energy": 8,
            "initial_health": 10,
            "initial_inv": {"ration": 2, "tile": 1},
            "archetypes": ["Orderly unit."],
        },
        "crowd": {
            "label": "Crowd",
            "public_tag": "crowd",
            "weight": 1,
            "size": 9,
            "discipline": 0.2,
            "home": "atrium",
            "brief": "Spontaneous crowd.",
            "initial_energy": 8,
            "initial_health": 10,
            "initial_inv": {"ration": 2},
            "archetypes": ["Restless crowd."],
        },
    }
    return spec


class CollectivesTests(PolityCase):
    def test_validation_defaults_bounds_and_scaled_initial_carry_cap(self):
        spec = collective_spec()
        # Crowd has size=9 -> carry_cap = round(10 * 3) = 30, so 15 weight fits while 31 fails.
        spec["groups"]["crowd"]["initial_inv"] = {"ration": 15}
        w = World.create(spec, 6, 7)
        crowds = [p for p in w.people.values() if p.role == "crowd"]
        leaders = [p for p in w.people.values() if p.role == "leader"]
        self.assertEqual((leaders[0].size, leaders[0].discipline), (1, 1.0))
        self.assertEqual((crowds[0].size, crowds[0].discipline), (9, 0.2))

        for bad_change in (
            lambda s: s["groups"]["crowd"].update(size=0),
            lambda s: s["groups"]["crowd"].update(size=2.5),
            lambda s: s["groups"]["crowd"].update(discipline=-0.1),
            lambda s: s["groups"]["crowd"].update(discipline=1.2),
            lambda s: s["groups"]["crowd"].update(initial_inv={"ration": 31}),
        ):
            broken = deepcopy(spec)
            bad_change(broken)
            with self.assertRaises(ValueError):
                self.store(broken)

    def test_discipline_scales_combat_power_and_defeat_cohesion_loss(self):
        spec = collective_spec()
        w = World.create(spec, 5, 7)
        units = [p for p in w.people.values() if p.role == "unit"]
        crowds = [p for p in w.people.values() if p.role == "crowd"]
        unit, crowd = units[0], crowds[0]
        # Remove weapon to compare equal-footing organization:
        # size=4, disc=0.95 -> 4**0.87 ≈ 3.34; size=9, disc=0.20 -> 9**0.42 ≈ 2.52
        unit.inv.pop("tile", None)
        self.assertGreater(
            unit.combat_power(w.spec["items"], w.spec["rules"]),
            crowd.combat_power(w.spec["items"], w.spec["rules"]),
        )
        # Armed disciplined unit defeats undisciplined crowd and inflicts higher rout damage.
        unit.inv["tile"] = 1
        crowd.health = 5
        trace = []
        events = turn(
            w,
            {
                unit.id: Intent("force", f"person:{crowd.id}"),
                crowd.id: Intent("wait"),
            },
            trace,
        )
        clash = next(e for e in trace if e["type"] == "combat")
        self.assertEqual(clash["winner"], "attackers")
        # Undisciplined loser (0.2 -> 1.4x multiplier) takes >= 6 damage and disperses from combat.
        self.assertGreaterEqual(clash["damage"][crowd.id], 5)
        self.assertFalse(crowd.is_alive)
        self.assertTrue(
            any(
                e[2] == "notice" and f"{crowd.id} dispersed (routed in combat)" in e[4]
                for e in events
            )
        )

    def test_exhaustion_dispersal_scaled_eat_carry_cap_and_signed_weight(self):
        spec = collective_spec()
        spec["places"]["atrium"]["initial_items"] = {"ration": 40}
        w = World.create(spec, 5, 7)
        leader = next(p for p in w.people.values() if p.role == "leader")
        unit = next(p for p in w.people.values() if p.role == "unit")
        crowd = next(p for p in w.people.values() if p.role == "crowd")

        # Carry cap scales with sqrt(size): leader=10, unit(4)=20, crowd(9)=30.
        self.assertEqual(w.observe(leader.id)["carry_cap"], 10)
        self.assertEqual(w.observe(unit.id)["carry_cap"], 20)
        self.assertEqual(w.observe(crowd.id)["carry_cap"], 30)

        # Eat restore scales inversely with sqrt(size): base 4 -> leader +4, unit +2, crowd +1.
        leader.energy = unit.energy = crowd.energy = 3
        turn(
            w,
            {
                leader.id: Intent("eat", item="ration"),
                unit.id: Intent("eat", item="ration"),
                crowd.id: Intent("eat", item="ration"),
            },
        )
        # After 1-tick base_energy_decay = 1:
        self.assertEqual(leader.energy, 6)
        self.assertEqual(unit.energy, 4)
        self.assertEqual(crowd.energy, 3)

        # Board signed_weight and IEMP weighted_signatures reflect total size of signers.
        trace = []
        turn(w, {leader.id: Intent("write", target="post", detail="petition")}, trace)
        doc_id = w.places["atrium"].board[0]
        turn(
            w,
            {
                unit.id: Intent("endorse", doc_id),
                crowd.id: Intent("endorse", doc_id),
            },
            trace,
        )
        view = w.observe(leader.id)
        self.assertEqual(view["board"][0]["signed"], 3)
        self.assertEqual(view["board"][0]["signed_weight"], 1 + 4 + 9)
        self.assertEqual(intellectual(w, trace)["weighted_signatures"], 14)

        # Exhausted collective disperses with collective phrasing rather than individual starvation.
        crowd.energy = 1
        events = turn(w, {crowd.id: Intent("wait")})
        self.assertFalse(crowd.is_alive)
        self.assertTrue(
            any(e[2] == "notice" and f"{crowd.id} dispersed (exhausted)" in e[4] for e in events)
        )

    def test_observation_prompts_serialization_and_replay_audit_for_collectives(self):
        spec = collective_spec()
        store = self.store(spec, count=6)
        w = store.world()
        leader = next(p for p in w.people.values() if p.role == "leader")
        units = [p for p in w.people.values() if p.role == "unit"]
        crowds = [p for p in w.people.values() if p.role == "crowd"]

        # Observation exposes self size/discipline and visible peer size/disciplined + guard total_size.
        turn(w, {units[0].id: Intent("guard", "path:annex", detail="none")})
        view = w.observe(leader.id)
        self.assertEqual((view["size"], view["discipline"]), (1, 1.0))
        self.assertEqual(view["guards"]["paths"]["annex"]["total_size"], 4)
        peers = {p["id"]: p for p in view["near"] + view["others_in_room"]}
        self.assertEqual((peers[units[0].id]["size"], peers[units[0].id]["disciplined"]), (4, True))
        self.assertEqual((peers[crowds[0].id]["size"], peers[crowds[0].id]["disciplined"]), (9, False))

        # Prompt framing distinguishes individual, disciplined unit, and spontaneous crowd while preserving shared prefix.
        personas = w.personas()
        p_leader = ResidentAgent(leader.id, personas[leader.id]).prepare(store, w, 0, 0, 60000).messages[0]["content"]
        p_unit0 = ResidentAgent(units[0].id, personas[units[0].id]).prepare(store, w, 0, 0, 60000).messages[0]["content"]
        p_unit1 = ResidentAgent(units[1].id, personas[units[1].id]).prepare(store, w, 0, 0, 60000).messages[0]["content"]
        p_crowd = ResidentAgent(crowds[0].id, personas[crowds[0].id]).prepare(store, w, 0, 0, 60000).messages[0]["content"]
        self.assertIn("独立个体（size=1）", p_leader)
        self.assertIn("建制集体单元（size=4", p_unit0)
        self.assertIn("自发集体单元（size=9", p_crowd)
        self.assertEqual(p_unit0, p_unit1)

        # Backward compatibility: legacy serialized snapshots lacking size/discipline default cleanly.
        legacy = w.to_dict()
        for data in legacy["people"].values():
            data.pop("size", None)
            data.pop("discipline", None)
        restored = World.from_dict(legacy)
        self.assertTrue(all((p.size, p.discipline) == (1, 1.0) for p in restored.people.values()))

        # Full replay & provenance audit passes with mixed individuals and collectives.
        sim = Simulation(store, Decisions(lambda actor, view: dict(action="wait")), window=0, keep=0)
        sim.step()
        sim.step()
        self.assertEqual(report(store.db)["audit"]["replay"], "passed")

    def test_bayport_crisis_scenario_loads_scales_and_passes_replay_audit(self):
        spec = load("bayport-crisis")
        w = World.create(spec, spec["default_residents"], 42)
        counts = Counter(p.role for p in w.people.values())
        self.assertEqual(len(w.people), 52)
        self.assertEqual(sum(p.size for p in w.people.values()), 610)
        self.assertEqual(
            counts,
            dict(
                conciliator=1,
                hardliner=1,
                councilor=1,
                garrison_chief=1,
                line_officer=2,
                organizer=4,
                journalist=4,
                liaison=2,
                student_bloc=8,
                civic_crowd=14,
                constabulary=6,
                garrison_unit=8,
            ),
        )
        # Verify three unique seals exist once each and combat power hierarchy matches design intent.
        for seal in ("cabinet_seal", "emergency_seal", "garrison_seal"):
            self.assertEqual(sum(p.inv.get(seal, 0) for p in w.people.values()), 1)
        by_role = lambda role: next(p for p in w.people.values() if p.role == role)
        p_crowd = by_role("civic_crowd").combat_power(spec["items"], spec["rules"])
        p_student = by_role("student_bloc").combat_power(spec["items"], spec["rules"])
        p_constab = by_role("constabulary").combat_power(spec["items"], spec["rules"])
        p_garrison = by_role("garrison_unit").combat_power(spec["items"], spec["rules"])
        self.assertLess(p_crowd, p_student)
        self.assertLess(p_student, p_constab)
        self.assertGreater(2 * p_crowd, p_constab)
        self.assertGreater(p_garrison, 5 * p_crowd)

        # Full multi-tick scripted simulation + replay audit passes cleanly.
        store = self.store(spec, count=52)
        sim = Simulation(store, Scripted(), window=0, keep=0)
        for _ in range(12):
            sim.step()
        rep = report(store.db)
        self.assertEqual(rep["audit"]["replay"], "passed")
        self.assertEqual(rep["audit"]["verified_ticks"], 12)
        self.assertIn("focal_summary", rep)

    def test_remote_comms_wired_portable_unreachable_shock_and_isolation_audit(self):
        from herd.runtime.agent.toolbox import AgentToolbox
        from herd.runtime.memory import format_map

        spec = collective_spec()
        spec["items"]["radio"] = {"label": "便携电台", "kind": "comm", "weight": 1}
        spec["places"]["atrium"]["comms"] = True
        spec["places"]["annex"]["comms"] = False
        spec["shocks"] = [
            {
                "tick": 1,
                "kind": "comms_set",
                "place": "atrium",
                "enabled": False,
                "announce_places": "*",
                "source": "line_cut",
                "text": "中庭通讯线路中断。",
            }
        ]
        self.assertIn("Atrium(atrium)[有通讯]", format_map(spec))
        self.assertNotIn("Annex(annex)[有通讯]", format_map(spec))

        store = self.store(spec, count=5)
        w = store.world()
        caller = next(p for p in w.people.values() if p.role == "leader")
        remote_radio = next(p for p in w.people.values() if p.role == "unit")
        remote_silent = next(p for p in w.people.values() if p.role == "crowd")
        remote_radio.place = "annex"
        remote_radio.inv["radio"] = 1
        remote_silent.place = "annex"
        caller.connect(remote_radio.id)
        remote_radio.connect(caller.id)
        caller.connect(remote_silent.id)
        remote_silent.connect(caller.id)

        # Opening observation exposes contact tags and local comms capability without leaking remote presence.
        view = w.observe(caller.id)
        self.assertTrue(view["room_comms"])
        self.assertTrue(view["can_remote_comm"])
        self.assertIn(dict(id=remote_radio.id, tag=remote_radio.tag), view["contacts"])
        # Spontaneous crowds (size>1, discipline<0.7) cannot use remote comms even in a comms room or holding a radio.
        remote_silent.inv["radio"] = 1
        self.assertFalse(w.observe(remote_silent.id)["can_remote_comm"])
        remote_silent.place = "atrium"
        self.assertFalse(w.observe(remote_silent.id)["can_remote_comm"])
        remote_silent.place = "annex"
        self.assertTrue(w.observe(remote_radio.id)["can_remote_comm"])

        # AgentToolbox allows remote stage_whisper to contacts when can_remote_comm is True, rejects otherwise.
        req_ok = ResidentAgent(caller.id, "p").prepare(store, w, 0, 0, 60000, mode="agent_loop")
        box_ok = AgentToolbox(req_ok, store)
        res = box_ok.call("stage_whisper", {"targets": [remote_radio.id], "text": "hold position"})
        self.assertEqual(res, {"ok": True, "result": {"staged": "whisper", "targets": [remote_radio.id]}})

        req_no = ResidentAgent(remote_silent.id, "p").prepare(store, w, 0, 0, 60000, mode="agent_loop")
        box_no = AgentToolbox(req_no, store)
        err = box_no.call("stage_whisper", {"targets": [caller.id], "text": "hello"})
        self.assertFalse(err["ok"])

        # Direct settlement check on w (with modified places/contacts):
        trace = []
        ev0 = turn(
            w,
            {
                caller.id: Intent(
                    "wait",
                    whisper_to=f"{remote_radio.id},{remote_silent.id}",
                    whisper="order over wire",
                )
            },
            trace,
        )
        self.assertIn((remote_radio.id, 0, "whisper", caller.id, "order over wire"), ev0)
        self.assertIn((caller.id, 0, "whisper_sent", remote_radio.id, "order over wire"), ev0)
        self.assertIn((caller.id, 0, "result", caller.id, f"remote contact {remote_silent.id} unreachable"), ev0)
        comm_rec = next(e for e in trace if e["type"] == "comm" and e["kind"] == "whisper")
        self.assertEqual(comm_rec["remote"], [remote_radio.id])

        # Tick 1 fires comms_set shock cutting atrium wired comms.
        turn(w, {})
        self.assertFalse(w.places["atrium"].comms)
        self.assertFalse(w.observe(caller.id)["can_remote_comm"])

        # End-to-end Simulation + provenance whisper isolation audit with real scenario ties & remote calls!
        bay = load("bayport-crisis")
        bay_store = self.store(bay, count=52, name="bayport")
        bw = bay_store.world()
        hardliner = next(p for p in bw.people.values() if p.role == "hardliner")
        chief = next(p for p in bw.people.values() if p.role == "garrison_chief")
        self.assertNotEqual(hardliner.place, chief.place)
        self.assertIn(chief.id, hardliner.contacts)

        def bay_decide(actor, v):
            if actor == hardliner.id and v["tick"] == 0:
                return dict(action="wait", whisper_to=chief.id, whisper="stand by at north camp")
            return dict(action="wait")

        sim = Simulation(bay_store, Decisions(bay_decide), window=0, keep=0)
        sim.step()
        sim.step()
        rep = report(bay_store.db)
        self.assertEqual(rep["audit"]["replay"], "passed")
        self.assertEqual(rep["audit"]["whisper_privacy"], "passed")
        chief_inbox = bay_store.events(chief.id, 0, 0)
        self.assertTrue(
            any(e["kind"] == "whisper" and e["source"] == hardliner.id and e["detail"] == "stand by at north camp" for e in chief_inbox)
        )
