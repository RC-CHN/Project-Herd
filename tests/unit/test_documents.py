import unittest

from herd.model import Intent, World
from herd.model.intent import WORLD_RULES
from tests.support import fixture, turn, world


class DocumentsTests(unittest.TestCase):
    def test_press_copy_post_and_endorse_toggle(self):
        w = world()
        self.assertEqual(w.observe("r001")["board_slots"], w.spec["rules"]["board_slots"])
        self.assertNotIn("请优先交谈", WORLD_RULES)
        turn(
            w,
            {
                "r000": Intent("write", item="badge", detail="forged"),
                "r001": Intent("write", detail="hello"),
            },
        )
        mid = "d0-r001"
        self.assertEqual(set(w.documents), {mid})
        trace = []
        turn(w, {"r001": Intent("produce", mid)}, trace)
        self.assertEqual(trace[0]["qty"], 3)
        self.assertEqual(w.people["r001"].docs[mid], 4)
        turn(w, {"r001": Intent("post", mid)})
        self.assertEqual(w.people["r001"].docs[mid], 3)
        turn(w, {"r000": Intent("endorse", mid)})
        self.assertEqual(w.observe("r001")["board"][0]["signed"], 2)
        turn(w, {"r000": Intent("endorse", mid)})
        self.assertEqual(w.observe("r001")["board"][0]["signed"], 1)
        turn(w, {"r000": Intent("write", "post", detail="second")})
        turn(w, {"r000": Intent("write", "post", detail="third")})
        self.assertEqual(w.places["atrium"].board, ["d5-r000", "d6-r000"])

    def test_write_energy_cost_deducts_on_success_and_floors_at_one(self):
        spec = fixture()
        spec["rules"]["write_energy_cost"] = 2
        spec["rules"]["base_energy_decay"] = 0
        w = World.create(spec, 6, 7)
        w.people["r000"].energy = 6
        w.people["r001"].energy = 2
        w.people["r002"].energy = 6
        turn(
            w,
            {
                "r000": Intent("write", detail="edict"),
                "r001": Intent("write", detail="last effort"),
                "r002": Intent("write", item="badge", detail="invalid seal"),
            },
        )
        self.assertEqual(w.people["r000"].energy, 4)
        self.assertEqual(w.people["r001"].energy, 1)
        self.assertEqual(w.people["r002"].energy, 6)
