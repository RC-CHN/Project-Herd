import json
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from herd.model import load
from herd.runtime.backends import Scripted
from herd.runtime.simulation import Simulation
from herd.runtime.storage import Store
from herd.web import RunIndex, create_server
from tests.support import PolityCase, fixture


class WebObservatoryTests(PolityCase):
    def test_scenario_theme_validation_and_bundled_presets(self):
        bay = load("bayport-crisis")
        cs = load("citystate-coup")
        self.assertEqual(bay["theme"]["preset"], "maritime-1894")
        self.assertEqual(cs["theme"]["preset"], "classical-parchment")

        bad = fixture()
        bad["theme"] = 123
        with self.assertRaises(ValueError):
            load(bad)

    def test_readonly_incremental_replay_and_http_api(self):
        db_path = self.folder / "demo.sqlite"
        spec = fixture()
        spec["theme"] = {"preset": "classical-parchment"}
        store = Store.create(db_path, 6, 7, scenario=spec)
        self.addCleanup(store.db.close)

        index = RunIndex(runs_dir=self.folder)
        runs = index.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["name"], "demo.sqlite")
        self.assertEqual(runs[0]["tick"], 0)

        # Opening tick before any committed tick works cleanly.
        cache = index.get("demo.sqlite")
        self.assertTrue(cache.tick_data(0)["opening"])

        # Advance 3 ticks while cache replays incrementally on next read.
        sim = Simulation(store, Scripted(), window=0, keep=0)
        for _ in range(3):
            sim.step()
        meta = cache.meta()
        self.assertEqual(meta["committed_ticks"], 3)
        self.assertEqual(meta["theme"]["preset"], "classical-parchment")
        self.assertEqual(len(meta["tick_summaries"]), 3)
        t2 = cache.tick_data(2)
        self.assertFalse(t2["opening"])
        self.assertEqual(t2["tick"], 2)

        # Spin up ephemeral HTTP server and verify static + JSON API + traversal protection.
        server = create_server(runs_dir=self.folder, host="127.0.0.1", port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        base = f"http://127.0.0.1:{port}"
        with urlopen(f"{base}/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("Project Herd", resp.read().decode("utf-8"))
        with urlopen(f"{base}/themes/maritime-1894.css") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("--bg:", resp.read().decode("utf-8"))
        with urlopen(f"{base}/api/runs") as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(payload["runs"][0]["tick"], 3)
        with urlopen(f"{base}/api/run/demo.sqlite/meta") as resp:
            meta_api = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(meta_api["committed_ticks"], 3)
        with urlopen(f"{base}/api/run/demo.sqlite/tick/1") as resp:
            tk1 = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(tk1["tick"], 1)
        with urlopen(f"{base}/api/run/demo.sqlite/all_ticks") as resp:
            all_tk = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(all_tk["ticks"]), 3)
        with self.assertRaises(HTTPError) as ctx:
            urlopen(f"{base}/api/run/..%2Fsecret/meta")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
