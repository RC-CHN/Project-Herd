from collections import Counter
from copy import deepcopy

from experiments.metrics.iemp import intellectual
from experiments.report_polity import report
from herd.model import Intent, World, encode, load
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
        # Undisciplined loser (0.2 -> 1.4x multiplier) takes >= 5 damage, suffers casualties, and routs to annex at hp=1.
        self.assertGreaterEqual(clash["damage"][crowd.id], 5)
        self.assertTrue(crowd.is_alive)
        self.assertEqual((crowd.place, crowd.health), ("annex", 1))
        self.assertLess(crowd.size, crowd.initial_size)
        self.assertEqual(clash["routed"].get(crowd.id), "annex")
        self.assertTrue(
            any(
                e[2] == "notice"
                and crowd.id in e[4]
                and "casualties" in e[4]
                and "routed from atrium to annex" in e[4]
                for e in events
            )
        )
        # Hitting an already-routed collective (opening health == 1) permanently disperses it.
        unit.place = "annex"
        events2 = turn(
            w,
            {
                unit.id: Intent("force", f"person:{crowd.id}"),
                crowd.id: Intent("wait"),
            },
        )
        self.assertFalse(crowd.is_alive)
        self.assertTrue(
            any(
                e[2] == "notice"
                and crowd.id in e[4]
                and "dispersed (routed in combat)" in e[4]
                for e in events2
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
            any(e[2] == "notice" and f"{crowd.id}({crowd.tag}) dispersed (exhausted)" in e[4] for e in events)
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
        self.assertIn("建制集体单元（初始编制=4", p_unit0)
        self.assertIn("自发集体单元（初始编制=9", p_crowd)
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
        self.assertEqual(len(w.people), 67)
        self.assertEqual(sum(p.size for p in w.people.values()), 771)
        self.assertEqual(
            counts,
            dict(
                conciliator=1,
                hardliner=1,
                councilor=1,
                praetorian=2,
                inner_guard=1,
                envoy=2,
                garrison_chief=1,
                line_officer=2,
                field_commander=1,
                police_chief=1,
                police_guard=1,
                north_guard=1,
                outer_guard=1,
                organizer=4,
                journalist=4,
                liaison=2,
                syndicate_agent=1,
                labor_leader=2,
                worker_picket=4,
                student_bloc=8,
                civic_crowd=14,
                constabulary=6,
                garrison_unit=4,
                field_unit=2,
            ),
        )
        # Police HQ connects plaza and bridgeway with guarded reserve armory & command chain.
        self.assertEqual(spec["places"]["police_hq"]["paths"], ["plaza", "bridgeway"])
        self.assertIn("police_hq", spec["places"]["plaza"]["paths"])
        self.assertIn("police_hq", spec["places"]["bridgeway"]["paths"])
        self.assertEqual(w.places["police_hq"].items.get("rifle"), 4)
        self.assertEqual(w.places["police_hq"].items.get("baton"), 6)
        self.assertEqual(w.places["north_camp"].items.get("rifle"), 6)
        self.assertEqual(w.places["outer_garrison"].items.get("rifle"), 6)
        p_chief = next(p for p in w.people.values() if p.role == "police_chief")
        self.assertEqual(p_chief.place, "police_hq")
        for role, expected_place in (("police_guard", "police_hq"), ("north_guard", "north_camp"), ("outer_guard", "outer_garrison")):
            sq = next(p for p in w.people.values() if p.role == role)
            self.assertEqual(sq.place, expected_place)
        constab_ids = {p.id for p in w.people.values() if p.role == "constabulary"}
        self.assertTrue(constab_ids <= set(p_chief.relations))
        # Foundry connects to south_ward with organized 4x15 worker pickets and 2 labor leaders.
        self.assertEqual(spec["places"]["foundry"]["paths"], ["south_ward"])
        self.assertIn("foundry", spec["places"]["south_ward"]["paths"])
        # Covert syndicate agent disguises public_tag while maintaining asymmetric cross-faction ties.
        agent = next(p for p in w.people.values() if p.role == "syndicate_agent")
        self.assertEqual(agent.tag, "外埠商客")
        self.assertEqual(len(agent.relations), 5)
        for peer_id, rels in agent.relations.items():
            self.assertNotEqual(rels, w.people[peer_id].rels_for(agent.id))
        # Physical fiscal_ledger starts in hall without being spoiled in global briefing; inner_gate buffers hall.
        self.assertEqual(w.places["hall"].items.get("fiscal_ledger"), 1)
        self.assertNotIn("fiscal_ledger", spec["briefing"])
        self.assertEqual(spec["places"]["hall"]["paths"], ["inner_gate"])
        self.assertEqual(spec["places"]["inner_gate"]["paths"], ["antechamber", "hall"])
        self.assertEqual(spec["places"]["outer_garrison"]["paths"], ["highland_post"])
        self.assertEqual(spec["places"]["highland_post"]["paths"], ["east_relay", "outer_garrison"])
        self.assertEqual(spec["places"]["east_relay"]["paths"], ["outer_gate", "highland_post"])
        self.assertEqual(spec["places"]["outer_gate"]["paths"], ["bridgeway", "east_relay"])
        # Dedicated 1x8 lethal inner_guard at inner_gate holds off massive unarmed crowds (278 people) while yielding to regular troops.
        from herd.systems.gates import GateSystem
        gs = GateSystem(w)
        lethal_mode = spec["rules"]["force_modes"]["lethal"]
        ig = next(p for p in w.people.values() if p.role == "inner_guard")
        self.assertEqual((ig.size, ig.roe, ig.place), (8, "lethal", "inner_gate"))
        all_students = [p.id for p in w.people.values() if p.role == "student_bloc"]
        six_crowds = [p.id for p in w.people.values() if p.role == "civic_crowd"][:6]
        self.assertGreater(
            gs.team_strength([ig.id], terrain=True, mode_spec=lethal_mode),
            gs.team_strength(all_students + six_crowds, mode_spec=lethal_mode),
        )
        # Outer corps starts lethal with zero ties to Bayport civilians.
        outer_cmd = next(p for p in w.people.values() if p.role == "field_commander")
        outer_units = [p for p in w.people.values() if p.role == "field_unit"]
        self.assertEqual(outer_cmd.roe, "lethal")
        self.assertTrue(all(u.roe == "lethal" and u.size == 25 for u in outer_units))
        self.assertTrue(
            all(
                w.people[cid].role in ("councilor", "hardliner", "field_unit", "outer_guard")
                for cid in outer_cmd.relations
            )
        )
        # Verify three unique seals exist once each and combat power hierarchy matches design intent.
        for seal in ("cabinet_seal", "emergency_seal", "garrison_seal"):
            self.assertEqual(sum(p.inv.get(seal, 0) for p in w.people.values()), 1)
        by_role = lambda role: next(p for p in w.people.values() if p.role == role)
        p_crowd = by_role("civic_crowd").combat_power(spec["items"], spec["rules"])
        p_student = by_role("student_bloc").combat_power(spec["items"], spec["rules"])
        p_constab = by_role("constabulary").combat_power(spec["items"], spec["rules"])
        p_worker = by_role("worker_picket").combat_power(spec["items"], spec["rules"])
        p_praet = by_role("praetorian").combat_power(spec["items"], spec["rules"])
        p_garrison = by_role("garrison_unit").combat_power(spec["items"], spec["rules"])
        self.assertLess(p_crowd, p_student)
        self.assertLess(p_student, p_constab)
        self.assertLess(p_constab, p_worker)
        self.assertLess(p_worker, p_garrison)
        self.assertLess(p_constab, p_praet)
        self.assertLess(p_praet, p_garrison)
        self.assertGreater(2 * p_crowd, p_constab)
        self.assertGreater(p_garrison, 5 * p_crowd)
        # Inner cabinet hall connects only to inner_gate, insulating leaders from plaza.
        self.assertEqual(spec["places"]["hall"]["paths"], ["inner_gate"])

        # Full multi-tick scripted simulation + replay audit passes cleanly.
        store = self.store(spec, count=spec["default_residents"])
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
        self.assertIn(dict(id=remote_radio.id, tag=remote_radio.tag, rels=["acquaintance"]), view["contacts"])
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
        bay_store = self.store(bay, count=bay["default_residents"], name="bayport")
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

    def test_agent_loop_renders_history_as_native_tool_calls_with_prefix_stability(self):
        import json
        from herd.runtime.prefix import message_key

        spec = collective_spec()
        store = self.store(spec, count=5, name="tool_hist")
        w0 = store.world()
        leader = next(p for p in w0.people.values() if p.role == "leader")
        peer = next(p for p in w0.people.values() if p.role == "unit")

        def decide(actor, v):
            if actor == leader.id and v["tick"] == 0:
                return dict(
                    action="guard",
                    target="path:annex",
                    detail=f"allow:{peer.id}",
                    speech="holding doorway",
                    whisper_to=peer.id,
                    whisper="stay ready",
                    note_key="plan",
                    note="guard annex",
                )
            if actor == leader.id and v["tick"] == 1:
                return dict(action="move", target="annex")
            return dict(action="wait")

        sim = Simulation(store, Decisions(decide), window=0, keep=0)
        sim.step()
        req1 = ResidentAgent(leader.id, "p").prepare(store, store.world(), 0, 0, 60000, mode="agent_loop")
        # Turn 0 history must be rendered as standard assistant(tool_calls) + tool responses, NOT raw JSON text.
        asst0 = req1.messages[2]
        self.assertEqual(asst0["role"], "assistant")
        self.assertEqual(asst0["content"], "")
        names0 = [c["function"]["name"] for c in asst0["tool_calls"]]
        self.assertEqual(names0, ["write_note", "stage_speech", "stage_whisper", "guard"])
        guard_args = json.loads(asst0["tool_calls"][3]["function"]["arguments"])
        self.assertEqual(guard_args, {"target": "path:annex", "allow_pass": f"allow:{peer.id}"})
        tool_msgs = req1.messages[3:7]
        self.assertTrue(all(m["role"] == "tool" for m in tool_msgs))
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], [c["id"] for c in asst0["tool_calls"]])

        # Appending Turn 1 must preserve exact prefix monotonicity in message_key (KV cache safe!).
        sim.step()
        req2 = ResidentAgent(leader.id, "p").prepare(store, store.world(), 0, 0, 60000, mode="agent_loop")
        self.assertTrue(message_key(req2.messages).startswith(message_key(req1.messages[:-1])))
        asst1 = req2.messages[8]
        self.assertEqual([c["function"]["name"] for c in asst1["tool_calls"]], ["move"])
        self.assertEqual(json.loads(asst1["tool_calls"][0]["function"]["arguments"]), {"place": "annex"})

        # json mode continues rendering flat JSON assistant content untouched.
        req_json = ResidentAgent(leader.id, "p").prepare(store, store.world(), 0, 0, 60000, mode="json")
        self.assertNotIn("tool_calls", req_json.messages[2])
        self.assertEqual(json.loads(req_json.messages[2]["content"])["action"], "guard")

        # Long documents in agent_loop views are compacted to previews while preserving full text in local/frame & prefix stability.
        long_text = "甲" * 160
        sim_doc = Simulation(
            store,
            Decisions(lambda a, v: dict(action="write", target="post", detail=long_text) if a == leader.id and v["tick"] == 2 else dict(action="wait")),
            window=0,
            keep=0,
        )
        sim_doc.step()
        w3 = store.world()
        req_loop3 = ResidentAgent(leader.id, "p").prepare(store, w3, 0, 0, 60000, mode="agent_loop")
        req_json3 = ResidentAgent(leader.id, "p").prepare(store, w3, 0, 0, 60000, mode="json")
        self.assertIn("用read_document查全文", req_loop3.messages[-1]["content"])
        self.assertNotIn(long_text, req_loop3.messages[-1]["content"])
        self.assertIn(long_text, req_json3.messages[-1]["content"])
        self.assertEqual(req_loop3.frame["view"]["board"][0]["text"], long_text)
        doc_id = w3.places[w3.people[leader.id].place].board[0]
        self.assertEqual(req_loop3.local["documents"][doc_id]["text"], long_text)
        sim_doc.step()
        req_loop4 = ResidentAgent(leader.id, "p").prepare(store, store.world(), 0, 0, 60000, mode="agent_loop")
        self.assertTrue(message_key(req_loop4.messages).startswith(message_key(req_loop3.messages[:-1])))
        self.assertEqual(report(store.db)["audit"]["replay"], "passed")

    def test_multitag_directed_relations_merge_preserve_and_roundtrip(self):
        bay = load("bayport-crisis")
        w = World.create(bay, bay["default_residents"], 42)
        conc = next(p for p in w.people.values() if p.role == "conciliator")
        hard = next(p for p in w.people.values() if p.role == "hardliner")
        elder = next(p for p in w.people.values() if p.role == "councilor")
        praet = next(p for p in w.people.values() if p.role == "praetorian")
        chief = next(p for p in w.people.values() if p.role == "garrison_chief")
        officers = [p for p in w.people.values() if p.role == "line_officer"]
        units = [p for p in w.people.values() if p.role == "garrison_unit"]

        # Multi-tag symmetric tie: conciliator <-> hardliner are both colleague and rival.
        self.assertEqual(conc.rels_for(hard.id), ["colleague", "rival"])
        self.assertEqual(hard.rels_for(conc.id), ["colleague", "rival"])
        # Praetorian guard owes direct personal allegiance to the councilor.
        self.assertEqual(elder.rels_for(praet.id), ["personal_guard"])
        self.assertEqual(praet.rels_for(elder.id), ["patron", "master"])
        # Directed multi-tag tie: councilor sees chief as protege; chief sees councilor as patron + elder_mentor.
        self.assertEqual(elder.rels_for(chief.id), ["protege"])
        self.assertEqual(chief.rels_for(elder.id), ["patron", "elder_mentor"])
        # Chain of command: each garrison_unit sees its partitioned line_officer as superior+camp_officer and chief as commander+comrade.
        for unit in units:
            self.assertEqual(unit.rels_for(chief.id), ["commander", "comrade"])
            superiors = [o.id for o in officers if "superior" in unit.rels_for(o.id)]
            self.assertEqual(len(superiors), 1)
            self.assertTrue(all("camp_officer" in unit.rels_for(o.id) for o in officers))
        # Dynamic whisper/connect does not clobber formal relations, while new strangers get ["acquaintance"].
        conc.connect(hard.id)
        self.assertEqual(conc.rels_for(hard.id), ["colleague", "rival"])
        stranger = next(p for p in w.people.values() if p.id not in conc.relations and p.id != conc.id)
        conc.connect(stranger.id)
        self.assertEqual(conc.rels_for(stranger.id), ["acquaintance"])
        # Legacy snapshot without `relations` migrates cleanly from `contacts`.
        legacy = w.to_dict()
        for data in legacy["people"].values():
            rels = data.pop("relations")
            data["contacts"] = list(rels.keys())
        restored = World.from_dict(legacy)
        self.assertEqual(restored.people[conc.id].rels_for(hard.id), ["known"])

    def test_scale_invariance_persistent_roe_lanchester_and_pop_casualties(self):
        from herd.runtime.agent.toolbox import AgentToolbox
        from herd.systems.gates import GateSystem

        bay = load("bayport-crisis")
        store = self.store(bay, count=bay["default_residents"], name="roe_test")
        w = store.world()

        # 1. Exact Pop scale invariance: 4 x size=25 crowds == 1 x size=100 crowd in team_strength.
        crowds = [p for p in w.people.values() if p.role == "civic_crowd"][:4]
        for c in crowds:
            c.place = "bridgeway"
            c.energy = 10
            c.health = 10
        s_four = GateSystem(w).team_strength([c.id for c in crowds])
        crowds[0].size = 100
        s_one = GateSystem(w).team_strength([crowds[0].id])
        self.assertAlmostEqual(s_four, s_one, places=6)
        crowds[0].size = 25

        # 2. Persistent ROE defaults: garrison_unit starts at push, praetorian starts at lethal.
        unit = next(p for p in w.people.values() if p.role == "garrison_unit")
        praet = next(p for p in w.people.values() if p.role == "praetorian")
        self.assertEqual(unit.roe, "push")
        self.assertEqual(praet.roe, "lethal")
        self.assertIn("push", w.observe(unit.id)["force_modes"])
        self.assertIn("lethal", w.observe(unit.id)["force_modes"])

        # 3. set_roe via AgentToolbox validates mode and stages parallel roe update.
        req = ResidentAgent(unit.id, "p").prepare(store, w, 0, 0, 60000, mode="agent_loop")
        box = AgentToolbox(req, store)
        self.assertFalse(box.call("set_roe", {"mode": "imaginary"})["ok"])
        self.assertEqual(
            box.call("set_roe", {"mode": "lethal"}),
            {"ok": True, "result": {"staged": "roe", "mode": "lethal"}},
        )
        # Guard tool rejects literal 'self' in allow_pass so small models self-correct immediately.
        bad_guard = box.call("guard", {"target": "path:bridgeway", "allow_pass": f"allow:{praet.id},self"})
        self.assertFalse(bad_guard["ok"])
        self.assertIn("self", bad_guard["error"])

        # 4. Non-lethal push clash causes 0 casualties and never kills (floor health=1).
        unit.place = "north_camp"
        for c in crowds:
            c.place = "bridgeway"
            c.health = 2
            c.energy = 2
            c.roe = "push"
        trace = []
        turn(
            w,
            {
                **{c.id: Intent("guard", "path:north_camp", detail="none") for c in crowds},
                unit.id: Intent("force", "path:bridgeway"),
            },
            trace,
        )
        clash_push = next(e for e in trace if e["type"] == "combat")
        self.assertEqual(clash_push["mode"], "push")
        self.assertEqual(sum(clash_push["casualties"].values()), 0)
        self.assertTrue(all(c.is_alive and c.size == 25 and c.health >= 1 for c in crowds))

        # 5. Escalating ROE to lethal persists, appears in recent_room/guards, inflicts Lanchester casualties & rout!
        unit.place = "north_camp"
        unit.health = 10
        unit.energy = 10
        for c in crowds:
            c.place = "bridgeway"
            c.health = 10
            c.energy = 10
        trace2 = []
        ev2 = turn(
            w,
            {
                **{c.id: Intent("guard", "path:north_camp", detail="none") for c in crowds},
                unit.id: Intent("force", "path:bridgeway", roe="lethal"),
            },
            trace2,
        )
        self.assertEqual(unit.roe, "lethal")
        self.assertTrue(any(r["actor"] == unit.id and r["to"] == "lethal" for r in w.places["north_camp"].last_summary["roe_changes"]))
        clash_lethal = next(e for e in trace2 if e["type"] == "combat")
        self.assertEqual(clash_lethal["mode"], "lethal")
        self.assertEqual(clash_lethal["winner"], "attackers")
        self.assertEqual(clash_lethal["escalated_by"], [unit.id])
        # Rifle company takes minimal Lanchester square-law damage while inflicting varied casualties & routing broken crowds off bridgeway!
        self.assertLessEqual(clash_lethal["damage"][unit.id], 2)
        self.assertGreater(sum(clash_lethal["casualties"][c.id] for c in crowds), 0)
        self.assertGreater(len(clash_lethal["routed"]), 0)
        self.assertTrue(
            all(
                w.people[cid].is_alive
                and w.people[cid].health == 1
                and w.people[cid].place == dest
                and dest not in ("bridgeway", "north_camp")
                for cid, dest in clash_lethal["routed"].items()
            )
        )
        self.assertTrue(any("routed from bridgeway" in e[4] for e in ev2 if e[2] == "notice"))

        # Second lethal hit on an already-routed crowd (opening health == 1) permanently disperses it!
        hunted = w.people[next(iter(clash_lethal["routed"]))]
        unit.place = hunted.place
        ev_hunt = turn(w, {unit.id: Intent("force", f"person:{hunted.id}"), hunted.id: Intent("wait")})
        self.assertFalse(hunted.is_alive)
        self.assertTrue(any("dispersed (routed in combat)" in e[4] for e in ev_hunt if e[2] == "notice"))

        # 6. Depleted collective down to size=1 remains collective (immutable prompt, no remote comm for low-disc crowd).
        survivor = next(p for p in w.people.values() if p.role == "civic_crowd" and p.is_alive)
        before_prompt = ResidentAgent(survivor.id, "p").prepare(store, w, 0, 0, 60000).messages[0]["content"]
        survivor.size = 1
        survivor.place = "south_ward"
        after_prompt = ResidentAgent(survivor.id, "p").prepare(store, w, 0, 0, 60000).messages[0]["content"]
        self.assertTrue(survivor.is_collective)
        self.assertEqual(before_prompt, after_prompt)
        self.assertFalse(w.observe(survivor.id)["can_remote_comm"])

        # 7. Collective reduced to size=0 is annihilated in combat.
        praet.place = "hall"
        praet.size = 1
        praet.health = 10
        unit.place = "hall"
        unit.size = 10
        unit.health = 10
        unit.energy = 10
        ev3 = turn(w, {unit.id: Intent("force", f"person:{praet.id}"), praet.id: Intent("wait")})
        self.assertEqual(praet.size, 0)
        self.assertFalse(praet.is_alive)
        self.assertTrue(any(f"{praet.id}({praet.tag}) was annihilated in combat" in e[4] for e in ev3 if e[2] == "notice"))

    def test_dynamic_remains_item_transport_guarding_adjacent_acoustics_and_replay_audit(self):
        from herd.runtime.agent.toolbox import AgentToolbox

        bay = load("bayport-crisis")
        # Scenario-defined item weights: scrip is weightless (0), body_weight is 6.
        self.assertEqual(bay["items"]["scrip"]["weight"], 0)
        self.assertEqual(bay["rules"]["body_weight"], 6)
        store = self.store(bay, count=bay["default_residents"], name="bodies_test")
        w = store.world()

        conc = next(p for p in w.people.values() if p.role == "conciliator")
        hard = next(p for p in w.people.values() if p.role == "hardliner")
        praet = next(p for p in w.people.values() if p.role == "praetorian")
        ig = next(p for p in w.people.values() if p.role == "inner_guard")
        conc.health = 1
        conc.inv.clear()
        praet.inv = {"rifle": 1}

        # 1. Lethal kill in hall creates a regular dynamic item (kind="remains") on hall's floor & alerts adjacent inner_gate!
        ev0 = turn(w, {praet.id: Intent("force", f"person:{conc.id}"), conc.id: Intent("wait")})
        self.assertFalse(conc.is_alive)
        body_id = f"body_{conc.id}"
        self.assertIn(body_id, w.dynamic_items)
        self.assertEqual(w.places["hall"].items.get(body_id), 1)
        self.assertEqual(w.dynamic_items[body_id]["kind"], "remains")
        self.assertEqual(w.dynamic_items[body_id]["weight"], 6)
        self.assertTrue(
            any(e[0] == hard.id and e[2] == "notice" and f"{conc.id}({conc.tag}) was killed in combat" in e[4] for e in ev0)
        )
        ig_notices = [e[4] for e in ev0 if e[0] == ig.id and e[2] == "notice"]
        self.assertTrue(any("密集枪声与惨叫" in m and "hall" in m and conc.id not in m for m in ig_notices))

        # 2. Everyone in hall sees body_id in regular view["items"] + view["item_info"]; guard items blocks unauthorized take!
        view_p = w.observe(praet.id)
        self.assertEqual(view_p["items"].get(body_id), 1)
        self.assertEqual(view_p["item_info"][body_id]["tag"], conc.tag)
        ev_blk = turn(w, {hard.id: Intent("guard", "items", detail="none"), praet.id: Intent("take", item=body_id)})
        self.assertEqual(w.places["hall"].items.get(body_id), 1)
        self.assertTrue(any(e[0] == praet.id and "blocked from taking items" in e[4] for e in ev_blk))

        # AgentToolbox validates take for dynamic remains with zero special-casing!
        req = ResidentAgent(praet.id, "p").prepare(store, w, 0, 0, 60000, mode="agent_loop")
        box = AgentToolbox(req, store)
        self.assertEqual(box.call("take", {"item": body_id}), {"ok": True, "result": {"submitted": "take"}})

        turn(w, {praet.id: Intent("take", item=body_id)})
        self.assertEqual(praet.inv.get(body_id), 1)
        self.assertEqual(w.places["hall"].items.get(body_id, 0), 0)
        self.assertEqual(w.observe(praet.id)["carry_weight"], 4 + 6)

        # Move to inner_gate and place body on the floor with regular give(target='here')
        turn(w, {praet.id: Intent("move", "inner_gate")})
        turn(w, {praet.id: Intent("give", "here", item=body_id)})
        self.assertEqual(praet.inv.get(body_id, 0), 0)
        self.assertEqual(w.observe(ig.id)["items"].get(body_id), 1)

        # 3. Full end-to-end Simulation + replay & provenance audit passes cleanly with era-neutral sound fallback!
        spec = collective_spec()
        spec["rules"]["body_weight"] = 4
        spec["groups"]["leader"]["initial_health"] = 1
        spec["groups"]["unit"]["home"] = {"atrium": 1, "annex": 1}
        sim_store = self.store(spec, count=5, name="bodies_audit")
        sw = sim_store.world()
        leader = next(p for p in sw.people.values() if p.role == "leader")
        unit_atrium = next(p for p in sw.people.values() if p.role == "unit" and p.place == "atrium")
        unit_annex = next(p for p in sw.people.values() if p.role == "unit" and p.place == "annex")

        b_id = f"body_{leader.id}"

        def script(actor, v):
            if actor == unit_atrium.id and v["tick"] == 0:
                return dict(action="force", target=f"person:{leader.id}")
            if actor == unit_atrium.id and v["tick"] == 1:
                return dict(action="take", item=b_id)
            if actor == unit_atrium.id and v["tick"] == 2:
                return dict(action="move", target="annex")
            if actor == unit_atrium.id and v["tick"] == 3:
                return dict(action="give", target="here", item=b_id)
            return dict(action="wait")

        sim = Simulation(sim_store, Decisions(script), window=0, keep=0)
        for _ in range(4):
            sim.step()
        rep = report(sim_store.db)
        self.assertEqual(rep["audit"]["replay"], "passed")
        self.assertEqual(sim_store.world().observe(unit_annex.id)["items"].get(b_id), 1)
        annex_ev0 = sim_store.events(unit_annex.id, 0, 0)
        self.assertTrue(any(e["kind"] == "notice" and "邻室动静" in e["detail"] and "枪声" not in e["detail"] for e in annex_ev0))
