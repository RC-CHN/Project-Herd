import unittest

from herd.model import Intent
from tests.support import world, turn


class DocumentsTests(unittest.TestCase):
    def test_press_copy_post_and_endorse_toggle(self):
        w = world()
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
