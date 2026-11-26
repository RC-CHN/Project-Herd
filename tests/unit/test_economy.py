from copy import deepcopy

from herd.model import Intent, World
from tests.support import PolityCase, fixture, world, turn


class EconomyTests(PolityCase):
    def test_give_here_puts_items_into_place_and_path_guard_protects_room(self):
        w = world()
        turn(w, {"r000": Intent("write", detail="record")})
        w.people["r006"].place = "annex"
        mid = "d0-r000"
        place = w.places["atrium"]
        before = place.items.copy()
        trace = []
        turn(
            w,
            {
                "r000": Intent("give", "here", item=mid),
                "r001": Intent("give", "here", item="ration", qty="2"),
                "r002": Intent("take", item="tile"),
                "r003": Intent("guard", "path:annex"),
                "r006": Intent("move", "atrium"),
            },
            trace,
        )
        self.assertEqual(place.docs, {mid: 1})
        self.assertNotIn(mid, w.people["r000"].docs)
        self.assertEqual(place.items["tile"], before["tile"] - 1)
        self.assertEqual(before["ration"], place.regen["ration"]["cap"])
        self.assertEqual(place.items["ration"], before["ration"] + 2)
        self.assertEqual(w.people["r006"].place, "annex")
        self.assertEqual({e["item"] for e in trace if e.get("kind") == "put"}, {mid, "ration"})
        for actor in ("r000", "r002", "r004", "r007"):
            summary = w.observe(actor)["recent_room"]
            self.assertCountEqual(
                summary["placed"],
                [dict(actor="r000", item=mid, qty=1), dict(actor="r001", item="ration", qty=2)],
            )
            self.assertEqual(summary["taken"], [dict(actor="r002", item="tile", qty=1)])
        self.assertFalse(w.observe("r006")["recent_room"]["placed"])
        turn(w, {"r004": Intent("take", item=mid)})
        self.assertEqual(w.people["r004"].docs, {mid: 1})
        self.assertEqual(place.last_summary["placed"], [])

    def test_carry_cap_bounds_take_produce_and_transfers(self):
        spec = fixture()
        spec["rules"]["carry_cap"] = 8
        spec["items"]["ration"]["weight"] = 3
        spec["items"]["token"]["weight"] = 0
        spec["places"]["annex"]["initial_items"] = {"ration": 6, "token": 50}
        spec["places"]["annex"]["facility"]["yield_per_action"] = 5
        bad = deepcopy(spec)
        bad["groups"]["circle"]["initial_inv"] = {"ration": 3}
        with self.assertRaises(ValueError):
            self.store(bad)
        self.assertFalse((self.folder / "run").exists())
        w = World.create(spec, 8, 7)
        for actor in ("r000", "r001"):
            w.people[actor].place = "annex"
            w.people[actor].inv = {"ration": 1}
        events = turn(w, {"r000": Intent("take", item="ration", qty="5"), "r001": Intent("produce")})
        self.assertEqual(w.places["annex"].items["ration"], 4)
        self.assertTrue(all(w.people[a].inv["ration"] == 2 for a in ("r000", "r001")))
        v = w.observe("r001")
        self.assertEqual((v["carry_weight"], v["carry_cap"], v["item_info"]["ration"]["weight"]), (6, 8, 3))
        events = turn(w, {"r000": Intent("take", item="ration"), "r001": Intent("produce")})
        self.assertEqual(sum(e[-1] == "failed: carry capacity exceeded" for e in events), 2)
        turn(
            w,
            {"r001": Intent("give", "r000", item="ration"), "r000": Intent("take", item="token", qty="50")},
        )
        self.assertEqual(w.people["r000"].inv, {"ration": 2, "token": 50})
        self.assertEqual(w.people["r001"].inv, {"ration": 2})

        # Overweight credit on either side must reject the entire contract.
        for terms in ("swap", "fair", "cheat"):
            fresh = World.create(spec, 4, 7)
            fresh.people["r000"].inv = {"ration": 2, "token": 2}
            turn(fresh, {"r000": Intent("offer", "ration:1", item="token", detail=terms)})
            before = [p.inv.copy() for p in fresh.people.values()]
            events = turn(fresh, {"r001": Intent("accept", "o0-r000")})
            self.assertEqual(before, [p.inv for p in fresh.people.values()])
            self.assertFalse(any(e[2] == "contract" for e in events))
        fresh = World.create(spec, 4, 7)
        fresh.people["r000"].inv = {"ration": 2}
        fresh.people["r001"].inv = {"tile": 6, "token": 1}
        turn(fresh, {"r000": Intent("offer", "token:1", item="ration", detail="swap")})
        events = turn(fresh, {"r001": Intent("accept", "o0-r000")})
        self.assertEqual(fresh.people["r000"].inv, {"ration": 2})
        self.assertEqual(fresh.people["r001"].inv, {"tile": 6, "token": 1})
        self.assertFalse(any(e[2] == "contract" for e in events))
        fresh.people["r001"].inv = {"tile": 8}
        turn(fresh, {"r000": Intent("offer", "tile:3", item="ration", detail="swap")})
        turn(fresh, {"r001": Intent("accept", "o2-r000")})
        self.assertEqual(fresh.people["r001"].inv, {"tile": 5, "ration": 1})
        self.assertEqual(fresh.people["r000"].inv, {"ration": 1, "tile": 3})

    def test_swap_and_pledge_contracts_no_double_spend(self):
        for mode, outcome in (("swap", "honored"), ("fair", "honored"), ("cheat", "breached")):
            w = world()
            p = w.people
            p["r000"].inv = {"token": 2}
            turn(w, {"r000": Intent("offer", "ration:1", item="token", qty="2", detail=mode)})
            offer = w.observe("r001")["offers"][0]
            self.assertEqual(offer["mode"], "swap" if mode == "swap" else "pledge")
            self.assertEqual(set(offer), {"id", "issuer", "give", "want", "mode"})
            trace = []
            events = turn(
                w,
                {
                    "r001": Intent("accept", offer["id"]),
                    "r002": Intent("accept", offer["id"]),
                    "r000": Intent("give", "r003", item="ration"),
                },
                trace,
            )
            parties = {v for v, _, k, _, _ in events if k == "contract"}
            self.assertEqual(len(parties), 2)
            self.assertIn("r000", parties)
            buyer = (parties - {"r000"}).pop()
            self.assertEqual(p[buyer].inv.get("token", 0), 0 if mode == "cheat" else 2)
            self.assertEqual(p["r000"].inv["ration"], 1)
            self.assertEqual(p["r003"].inv["ration"], 2)
            self.assertTrue(all(e["outcome"] == outcome for e in trace if e["type"] == "transfer"))
            self.assertEqual(w.offers, {})
        for mode in ("fair", "swap"):
            w = world()
            turn(w, {"r000": Intent("offer", "ration:1", item="token", detail=mode)})
            events = turn(w, {"r001": Intent("accept", "o0-r000")})
            self.assertEqual(
                sum(k == "contract" and t.startswith("breached") for _, _, k, _, t in events),
                2 if mode == "fair" else 0,
            )
        w = world()
        w.people["r000"].inv = {"token": 1}
        turn(w, {"r000": Intent("offer", "none", item="token", detail="swap")})
        turn(w, {"r000": Intent("give", "r002", item="token"), "r001": Intent("accept", "o0-r000")})
        self.assertEqual(sum(p.inv.get("token", 0) for p in w.people.values()), 1)
        self.assertNotIn("token", w.people["r000"].inv)
        # A seal transferred via accepted offer in Phase 5 cannot still stamp a document in Phase 6.
        w = world()
        turn(w, {"r000": Intent("take", item="badge")})
        turn(w, {"r000": Intent("offer", "none", item="badge", detail="swap")})
        events = turn(
            w,
            {
                "r001": Intent("accept", "o1-r000"),
                "r000": Intent("write", detail="late stamp", item="badge"),
            },
        )
        self.assertIn(("r000", 2, "result", "r000", "failed: missing seal"), events)
        self.assertEqual(w.documents, {})

        # Give failure reasons distinguish recipient absence/full cap, and expired offers notify issuer.
        w5 = world()
        w5.people["r002"].place = "annex"
        ev_absent = turn(w5, {"r000": Intent("give", "r002", item="ration"), "r001": Intent("offer", "none", item="ration", detail="swap")})
        self.assertTrue(any(e[0] == "r000" and "recipient not in room" in e[4] for e in ev_absent))
        ev_expire = turn(w5, {})
        self.assertEqual(w5.offers, {})
        self.assertTrue(any(e[0] == "r001" and e[2] == "notice" and "邀约失效" in e[4] for e in ev_expire))
