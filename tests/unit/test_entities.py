from collections import Counter
from copy import deepcopy

from herd.model import Intent, World, load
from tests.support import PolityCase, fixture, world, turn


class EntitiesTests(PolityCase):
    def test_citystate_scenario_loads_and_allocates_exact_and_weighted_groups(self):
        spec = load("citystate-coup")
        for n in (24, 54, 128):
            w = World.create(spec, n, 7)
            counts = Counter(p.role for p in w.people.values())
            self.assertEqual(sum(counts.values()), n)
            for g in ("ruler", "minister", "officer"):
                self.assertEqual(counts[g], spec["groups"][g]["exact_count"])
            for g, group in spec["groups"].items():
                if "exact_count" not in group:
                    self.assertLess(abs(counts[g] - (n - 7) * group["weight"] / 47), 1)
                home = group["home"]
                weights = {home: 1} if isinstance(home, str) else home
                actual = Counter(p.place for p in w.people.values() if p.role == g)
                for place, weight in weights.items():
                    self.assertLess(abs(actual[place] - counts[g] * weight / sum(weights.values())), 1)
            if n == 54:
                self.assertEqual(
                    counts, dict(ruler=1, minister=3, officer=3, guard=15, priest=3, merchant=5, citizen=24)
                )
                officers = [p for p in w.people.values() if p.role == "officer"]
                for officer in officers:
                    local_guards = {
                        a for a, p in w.people.items() if p.role == "guard" and p.place == officer.place
                    }
                    self.assertTrue(local_guards <= set(officer.contacts))
                    self.assertGreaterEqual(sum(w.people[a].role == "guard" for a in officer.contacts), 5)
                guards = [p for p in w.people.values() if p.role == "guard"]
                for g1 in guards:
                    for g2 in guards:
                        if g1.id != g2.id and g1.place == g2.place:
                            self.assertIn(g2.id, g1.contacts)
                assigned = w.personas()
                guard_traits = Counter(assigned[a] for a, p in w.people.items() if p.role == "guard")
                self.assertEqual(sorted(guard_traits.values()), [2, 13])
                citizen_traits = Counter(assigned[a] for a, p in w.people.items() if p.role == "citizen")
                self.assertEqual(sorted(citizen_traits.values()), [2, 4, 18])
            self.assertTrue(all(a in w.people[b].contacts for a, p in w.people.items() for b in p.contacts))
        bad = [
            lambda s: s["places"]["port"]["paths"].clear(),
            lambda s: s["places"]["port"].update(pos=[0, 2]),
            lambda s: s["places"]["port"]["initial_items"].update(missing=1),
            lambda s: s["groups"]["minister"].update(home={"elsewhere": 2}),
            lambda s: s["groups"]["ruler"].update(archetypes=[]),
            lambda s: s["groups"]["guard"].update(archetypes=[{"text": "bad", "weight": 0}]),
            lambda s: s["ties"][0].update(mode="imaginary"),
            lambda s: s["shocks"][0].update(multiplier=-1),
            lambda s: s["analysis_markers"].update(focal_groups=["missing"]),
            lambda s: s["analysis_markers"].update(focal_seals=["silver"]),
        ]
        for change in bad:
            broken = deepcopy(spec)
            change(broken)
            with self.assertRaises(ValueError):
                self.store(broken)
            self.assertFalse((self.folder / "run").exists())
        with self.assertRaisesRegex(ValueError, "exact_count"):
            self.store(spec, count=6)

    def test_huddle_prefers_contacts_over_pure_random(self):
        w = world(40)
        for i, p in enumerate(w.people.values()):
            p.contacts = [f"r{j:03}" for j in range(i // 5 * 5, i // 5 * 5 + 5) if j != i]
        matches = pairs = 0
        for tick in range(40):
            w.tick = tick
            idx = w.build_index()
            for a, table in idx.huddles.items():
                self.assertLessEqual(len(table), 4)
                self.assertEqual(len(set(table)), len(table))
                matches += sum(peer in w.people[a].contacts for peer in table)
                pairs += len(table) - 1
        self.assertGreater(matches / pairs, 2 * 4 / 39)
        self.assertLess(matches / pairs, 1)

    def test_settlement_is_independent_of_role_and_analysis_metadata(self):
        original = world()
        other = deepcopy(original)
        for person in other.people.values():
            person.role = "changed"
        other.spec["analysis_markers"] = {"focal_places": ["annex"]}
        other.spec["places"]["atrium"]["pos"] = [1, 0]
        actions = dict(
            r000=Intent("guard", "path:*"),
            r001=Intent("force", "path:annex"),
            r002=Intent("force", "person:r003"),
            r004=Intent("take", item="badge"),
            r005=Intent("wait", whisper_to="r006", whisper="message"),
        )
        left, right = [], []
        self.assertEqual(turn(original, actions, left), turn(other, actions, right))
        self.assertEqual(left, right)
        for a, p in original.people.items():
            self.assertEqual({**p.to_dict(), "role": "changed"}, other.people[a].to_dict())

    def test_resident_tools_receive_only_readable_documents_and_local_people(self):
        from herd.model.entities import Document
        from herd.model.observation import local_context

        simulation = world(6)
        me = simulation.people["r000"]
        room = simulation.places[me.place]
        simulation.people["r001"].place = "annex"
        for doc_id in ("posted", "held", "floor", "remote", "private", "zero"):
            simulation.documents[doc_id] = Document(doc_id, "r002", 0, doc_id, signers=["r003"])
        room.board.append("posted")
        room.docs.update(floor=1, zero=0)
        me.docs["held"] = 1
        simulation.places["annex"].board.append("remote")
        simulation.people["r002"].docs["private"] = 1
        context = local_context(simulation, me.id, simulation.build_index())
        self.assertEqual(context["present"], ["r000", "r002", "r003", "r004", "r005"])
        self.assertEqual(set(context["documents"]), {"held", "posted", "floor"})
        self.assertEqual(context["documents"]["floor"]["signers"], ["r003"])
        context["documents"]["posted"]["signers"].clear()
        self.assertEqual(simulation.documents["posted"].signers, ["r003"])

    def test_same_home_tie_connects_colocated_members_only(self):
        spec = fixture()
        spec["groups"]["circle"]["home"] = {"atrium": 1, "annex": 1}
        spec["ties"] = [{"from_group": "circle", "to_group": "circle", "mode": "same_home"}]
        w = World.create(spec, 8, 7)
        circle = [p for p in w.people.values() if p.role == "circle"]
        for a in circle:
            for b in circle:
                if a.id == b.id:
                    continue
                if a.place == b.place:
                    self.assertIn(b.id, a.contacts)
                else:
                    self.assertNotIn(b.id, a.contacts)

    def test_observation_exposes_room_people_weak_contacts_own_offers_board_tick_and_gate_directions(self):
        spec = fixture()
        spec["rules"]["contact_size"] = 4
        w = World.create(spec, 10, 7)
        w.people["r000"].contacts = ["r001"]
        w.people["r001"].contacts = ["r000"]
        for actor, person in w.people.items():
            if actor != "r000":
                person.energy = spec["rules"]["low_energy_threshold"]
        w.people["r009"].place = "annex"
        turn(
            w,
            {
                "r000": Intent("offer", "token:1", item="ration", detail="cheat"),
                "r001": Intent("write", target="post", detail="bulletin"),
                "r002": Intent("guard", "path:annex", detail="none"),
                "r009": Intent("guard", "path:atrium", detail="none"),
                "r003": Intent("guard", "items", detail="none"),
                "r004": Intent("take", item="ration"),
                "r005": Intent("give", "r006", item="ration"),
            },
        )
        view = w.observe("r000")
        self.assertEqual(view["contacts"], [dict(id="r001", tag="Neighbor", rels=["known"])])
        self.assertEqual(len(view["own_offers"]), 1)
        self.assertEqual(view["own_offers"][0]["terms"], "cheat")
        self.assertEqual(w.observe("r001")["own_offers"], [])
        self.assertNotIn("terms", w.observe("r001")["offers"][0])
        self.assertEqual(view["board"][0]["tick"], 0)
        near_ids = {p["id"] for p in view["near"]}
        other_ids = {p["id"] for p in view["others_in_room"]}
        self.assertFalse(near_ids & other_ids)
        self.assertEqual(len(near_ids) + len(other_ids) + 1, view["room_count"])
        self.assertTrue(view["near"] and all(p["weak"] for p in view["near"]))
        annex_gate = view["guards"]["paths"]["annex"]
        self.assertEqual((annex_gate["blocking_entry"], annex_gate["blocking_exit"]), (1, 1))
        # Outbound guard fields reflect only guards blocking r000's exit to annex (r009),
        # not friendly/inbound guards in atrium blocking entry from annex (r002).
        self.assertEqual((annex_gate["count"], annex_gate["inbound_size"]), (1, 1))
        self.assertEqual(annex_gate["known_guards"], [])
        self.assertEqual(view["recent_room"]["blocked_takes"], [dict(actor="r004", item="ration")])
        self.assertEqual(
            view["recent_room"]["given"], [{"from": "r005", "to": "r006", "item": "ration", "qty": 1}]
        )
