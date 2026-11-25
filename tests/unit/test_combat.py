from hashlib import sha256

from experiments.report_polity import report
from herd.model import Document, Intent, World
from herd.runtime.memory import compress
from herd.runtime.simulation import Simulation
from tests.support import Decisions, PolityCase, fixture, turn, world


class CombatTests(PolityCase):
    def test_bodyguard_protects_target_until_desertion(self):
        w = world()
        a, b = "r000", "r001"
        people = w.people
        people[a].inv["tile"] = 1
        people[b].health = 3
        people[b].inv = {"badge": 1}
        people[b].docs = {"letter": 2}
        del w.places["atrium"].items["badge"]
        w.documents["letter"] = Document(id="letter", author=b, text="record", tick=0, seal="", signers=[b])
        moves = {g: Intent("guard", "person:" + b) for g in ("r002", "r003", "r004")}
        for g in moves:
            people[g].inv["tile"] = 1
        trace = []
        turn(w, {**moves, a: Intent("force", "person:" + b)}, trace)
        fight = next(e for e in trace if e["type"] == "combat")
        self.assertEqual(fight["winner"], "defenders")
        self.assertEqual(set(fight["defenders"]), {b, *moves})
        self.assertGreaterEqual(fight["damage"][a], 4)
        self.assertIn(b, w.active_residents())
        trace.clear()
        events = turn(
            w, {a: Intent("force", "person:" + b), b: Intent("give", "r005", item="badge")}, trace
        )
        self.assertIn((b, 1, "death", b, "combat"), events)
        self.assertNotIn(b, w.active_residents())
        self.assertEqual(people[b].inv, {})
        self.assertEqual(people[b].docs, {})
        self.assertEqual(w.places["atrium"].docs, {"letter": 2})
        self.assertEqual(w.places["atrium"].items["badge"], 1)
        self.assertFalse(people["r005"].inv.get("badge"))
        self.assertEqual(next(e["dropped_docs"] for e in trace if e["type"] == "death"), {"letter": 2})
        self.assertEqual({e["item"] for e in trace if e["type"] == "transfer"}, {"badge", "letter"})
        turn(w, {"r005": Intent("take", item="badge"), "r006": Intent("take", item="letter", qty="2")})
        self.assertEqual(people["r005"].inv["badge"], 1)
        self.assertEqual(people["r006"].docs["letter"], 2)

    def test_force_group_damage_energy_and_nonlethal_scale(self):
        w = world()
        people = w.people
        people["r000"].inv["tile"] = 9
        people["r001"].energy = 4
        people["r001"].health = 8
        people["r002"].place = "annex"
        actions = dict(
            r000=Intent("force", "path:annex"),
            r001=Intent("force", "path:annex"),
            r002=Intent("guard", "path:atrium"),
            r003=Intent("guard", "path:*"),
            r004=Intent("move", "annex"),
        )
        # Scale-invariant strength (size=1, disc=1.0 -> alpha=0.9) and Lanchester damage (p=1.0).
        epsilon = (
            0.3 * int.from_bytes(sha256(b"combat/7/0/force/atrium/path:annex").digest()[:4], "big") / 2**32
            - 0.15
        )
        alpha = 0.9
        attack = ((2.5 ** (1 / alpha) + (0.5 * 0.8) ** (1 / alpha)) ** alpha) * (1 + epsilon)
        defend = (1.0 ** (1 / alpha) + 1.35 ** (1 / alpha)) ** alpha
        self.assertGreater(attack, defend)
        disadvantage = defend / attack
        t = []
        turn(w, actions, t)
        clash = next(e for e in t if e["type"] == "combat")
        self.assertEqual(clash["kind"], "path")
        self.assertEqual(set(clash["attackers"]), {"r000", "r001"})
        self.assertEqual(set(clash["defenders"]), {"r002", "r003"})
        self.assertEqual(clash["damage"]["r000"], round(3.0 * disadvantage))
        self.assertEqual(clash["damage"]["r003"], max(1, round(2.5 + 3.5 * (1.0 - disadvantage))))
        self.assertEqual(people["r001"].energy, 3)
        self.assertEqual(w.places["atrium"].last_summary["guards_dropped"], ["r003"])
        self.assertEqual(w.places["annex"].last_summary["guards_dropped"], ["r002"])
        self.assertEqual([people[a].place for a in ("r000", "r001", "r004")], ["annex"] * 3)
        before = (people["r004"].health, people["r004"].energy)
        turn(w, {"r004": Intent("force", "path:atrium")})
        self.assertEqual((people["r004"].health, people["r004"].energy), before)
        people["r004"].energy = 1
        events = turn(w, {"r004": Intent("force", "person:r003")})
        self.assertIn(("r004", 2, "death", "r004", "combat"), events)
        self.assertNotIn("r004", w.active_residents())
        # Non-lethal scenario (combat_damage_scale=0): crowd breaks a blockade without health damage.
        mild = fixture()
        mild["rules"]["combat_damage_scale"] = 0.0
        mw = World.create(mild, 6, 7)
        mt = []
        turn(
            mw,
            {
                "r000": Intent("guard", "path:*"),
                "r001": Intent("force", "path:annex"),
                "r002": Intent("force", "path:annex"),
                "r003": Intent("force", "path:annex"),
            },
            mt,
        )
        mclash = next(e for e in mt if e["type"] == "combat")
        self.assertEqual(mclash["winner"], "attackers")
        self.assertEqual(set(mclash["damage"].values()), {0})
        self.assertEqual(mw.people["r000"].health, 10)
        self.assertIsNone(mw.people["r000"].posture)
        self.assertEqual(mw.people["r001"].place, "annex")

    def test_force_does_not_hit_already_permitted_actors(self):
        w = world()
        w.people["r000"].inv["badge"] = w.places["atrium"].items.pop("badge")
        actions = {
            "r000": Intent("force", "path:annex"),
            "r001": Intent("force", "path:annex"),
            "r002": Intent("guard", "path:*", detail="seal:badge"),
            "r003": Intent("guard", "path:*", detail="seal:badge"),
        }
        trace = []
        health, energy = w.people["r000"].health, w.people["r000"].energy
        turn(w, actions, trace)
        self.assertEqual(
            (w.people["r000"].place, w.people["r000"].health, w.people["r000"].energy),
            ("annex", health, energy),
        )
        fights = [e for e in trace if e["type"] == "combat"]
        self.assertEqual(len(fights), 1)
        self.assertEqual(fights[0]["attackers"], ["r001"])
        self.assertEqual(set(fights[0]["defenders"]), {"r002", "r003"})
        self.assertNotIn("r000", fights[0]["damage"])
        sole = world()
        trace = []
        events = turn(sole, {"r000": Intent("force", "path:annex")}, trace)
        self.assertFalse(any(e["type"] == "combat" for e in trace))
        self.assertFalse(any(e[2] == "combat_witness" for e in events))

    def test_combat_is_witnessed_by_bystanders_and_arms_wounds_are_visible(self):
        spec = fixture()
        spec["rules"]["contact_size"] = 12
        spec["places"]["loft"] = {"label": "Loft", "paths": ["atrium"]}
        spec["places"]["atrium"]["paths"].append("loft")

        def choose(actor, view):
            orders = [
                {
                    "r000": dict(action="take", item="tile"),
                    "r005": dict(action="move", target="annex"),
                    "r007": dict(action="move", target="loft"),
                },
                {
                    "r000": dict(action="force", target="person:r001"),
                    "r002": dict(action="guard", target="person:r001"),
                },
                {
                    "r004": dict(action="force", target="path:annex"),
                    "r003": dict(action="guard", target="path:*"),
                },
            ]
            return (
                orders[view["tick"]].get(actor, dict(action="wait"))
                if view["tick"] < len(orders)
                else dict(action="wait")
            )

        store = self.store(spec)
        sim = Simulation(store, Decisions(choose), window=2, keep=0)
        sim.step()
        sim.step()
        view = store.world().observe("r006")
        neighbors = {p["id"]: p for p in view["near"]}
        self.assertTrue(neighbors["r000"]["armed"])
        self.assertTrue(any(neighbors[a]["wounded"] for a in ("r000", "r001", "r002")))
        self.assertEqual(
            set(neighbors["r000"]),
            {"id", "tag", "size", "disciplined", "roe", "known", "rels", "armed", "wounded", "weak"},
        )
        self.assertEqual(view["guards"]["persons"]["r001"]["armed_count"], 0)

        def witnesses(tick):
            return {
                row[0]
                for row in store.db.execute(
                    "SELECT viewer FROM events WHERE kind='combat_witness' AND tick=?", (tick,)
                )
            }

        self.assertEqual(witnesses(1), {"r003", "r004", "r006"})
        sim.step()
        self.assertEqual(witnesses(2), {"r000", "r001", "r002", "r005", "r006"})
        sim.step()
        recall = store.recall("r006", ["r000"], 2)
        self.assertTrue(any(e["kind"] == "combat_witness" and e["source"] == "r000" for e in recall))
        event = next(e for e in store.events("r006", 1, 1) if e["kind"] == "combat_witness")
        self.assertIn(
            event, compress([], [event] + [dict(tick=2, source="r005", kind="speech", detail="talk")] * 50)
        )
        self.assertEqual(report(store.db)["audit"]["replay"], "passed")

    def test_force_items_breaks_floor_guard_and_defeat_disrupts_take_and_give(self):
        w = world()
        w.people["r001"].inv["tile"] = 1
        w.people["r002"].inv["tile"] = 1
        trace = []
        events = turn(
            w,
            {
                "r000": Intent("guard", "items", detail="none"),
                "r001": Intent("force", "items"),
                "r002": Intent("force", "items"),
                "r003": Intent("take", item="ration"),
            },
            trace,
        )
        clash = next(e for e in trace if e["type"] == "combat")
        self.assertEqual((clash["kind"], clash["winner"]), ("items", "attackers"))
        self.assertIsNone(w.people["r000"].posture)
        self.assertTrue(any(e[0] == "r004" and e[2] == "combat_witness" for e in events))
        # Once the guard is broken in Phase 3, r003's Phase 5 take succeeds.
        self.assertEqual(w.people["r003"].inv["ration"], 3)

        # Losing a person assault and taking damage interrupts same-tick take/give.
        w2 = world()
        w2.people["r000"].inv["tile"] = 1
        w2.people["r001"].inv["tile"] = 1
        before_ration = w2.people["r002"].inv["ration"]
        events2 = turn(
            w2,
            {
                "r000": Intent("force", "person:r002"),
                "r001": Intent("force", "person:r002"),
                "r002": Intent("take", item="ration"),
            },
        )
        self.assertIn(("r002", 0, "result", "r002", "failed: disrupted by combat"), events2)
        # Defeat in person assault both disrupted r002's take and disarmed 1 ration to the floor.
        self.assertEqual(w2.people["r002"].inv["ration"], before_ration - 1)

    def test_person_assault_disarms_surviving_loser_to_room_floor(self):
        w = world()
        w.people["r000"].inv["tile"] = 1
        w.people["r001"].inv["tile"] = 1
        w.people["r002"].inv = {"tile": 1, "ration": 2}
        before_floor_tile = w.places["atrium"].items.get("tile", 0)
        trace = []
        events = turn(
            w,
            {
                "r000": Intent("force", "person:r002"),
                "r001": Intent("force", "person:r002"),
                "r002": Intent("wait"),
            },
            trace,
        )
        self.assertTrue(w.people["r002"].is_alive)
        self.assertNotIn("tile", w.people["r002"].inv)
        self.assertEqual(w.places["atrium"].items["tile"], before_floor_tile + 1)
        self.assertIn(("r002", 0, "result", "r002", "dropped 1 tile in combat"), events)
        self.assertTrue(any(e.get("kind") == "disarm" and e.get("item") == "tile" for e in trace))
        self.assertIn(dict(actor="r002", item="tile", qty=1), w.places["atrium"].last_summary["placed"])
