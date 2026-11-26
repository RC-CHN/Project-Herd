import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from experiments.report_polity import report
from herd.model import RULES, Intent, World, encode
from herd.runtime.backends import Scripted
from herd.runtime.memory import compress, format_map, identity_block
from herd.runtime.simulation import Simulation
from herd.runtime.storage import Store
from tests.support import Decisions, PolityCase, fixture, turn, world


class ReplayAuditTests(PolityCase):
    def test_engine_is_scenario_agnostic(self):
        spec = fixture()
        spec["assumptions"] = "analysis-only-assumption"
        spec["notices"] = [dict(tick=3, place="annex", source="clock", text="future-hidden-notice")]
        store = self.store(spec)
        w = store.world()
        by_group = {}
        for agent in store.agents():
            view = w.observe(agent.actor)
            req = agent.prepare(store, w, 4, 1, 24000)
            g = w.people[agent.actor].role
            by_group.setdefault(g, set()).add(agent.persona)
            self.assertEqual(
                req.messages[0]["content"],
                "\n".join(
                    [
                        RULES,
                        spec["briefing"],
                        format_map(spec),
                        spec["groups"][g]["brief"],
                        identity_block(w.people[agent.actor], agent.persona),
                    ]
                ),
            )
            for forbidden in ("pos", "analysis_markers", "shocks", "assumptions"):
                self.assertNotIn(forbidden, view)
            self.assertNotIn("future-hidden-notice", str(req.messages))
            self.assertNotIn(spec["assumptions"], str(req.messages))
        self.assertTrue(all(len(traits) == 2 for traits in by_group.values()))
        sim = Simulation(store, Scripted())
        for _ in range(5):
            sim.step()
        r = report(store.db)
        self.assertEqual(r["audit"]["verified_ticks"], 5)
        self.assertNotIn("focal_summary", r)
        last = r["iemp_timeline"][-1]
        self.assertEqual(set(last), {"tick", "I", "E", "M", "P"})
        self.assertEqual(last["P"]["seals"].keys(), {"badge"})
        self.assertEqual(last["M"]["guards"].keys(), spec["places"].keys())
        self.assertEqual(last["E"]["group_inventory"].keys(), spec["groups"].keys())
        root = Path(__file__).resolve().parents[2]
        for package in ("herd/model", "herd/systems", "herd/runtime", "experiments"):
            for path in (root / package).rglob("*.py"):
                source = path.read_text()
                for name in ("citadel", "treasury", "royal_seal", "ruler", "grain"):
                    self.assertNotIn(name, source, path)
        self.assertEqual(World.from_dict(w.to_dict()).to_dict(), w.to_dict())

    def test_whisper_privacy_and_probabilistic_detection(self):
        rates = []
        for n in (5, 32):
            w = world(n)
            trials = 60
            observations = 0
            for tick in range(trials):
                pair = ("r000", "r001")
                idx = w.build_index()
                expected = set()
                for obs in set(w.people) - set(pair):
                    same = any(idx.huddles[obs] is idx.huddles[a] for a in pair)
                    chance = min(0.65, max(0.05, 0.35 * 6 / max(6, n - 1) + 0.15 * same))
                    key = f"whisper/7/{tick}/r000/r001/{obs}"
                    if int.from_bytes(sha256(key.encode()).digest()[:4], "big") / 2**32 < chance:
                        expected.add(obs)
                secret = f"private sentence {tick}"
                trace = []
                events = turn(w, {"r000": Intent("wait", whisper_to="r001", whisper=secret)}, trace)
                self.assertEqual({v for v, _, _, _, text in events if secret in text}, set(pair))
                observers = {v for v, _, k, _, _ in events if k == "spotted_whisper"}
                self.assertEqual(observers, expected)
                self.assertEqual(set(trace[0]["detected_by"]), expected)
                self.assertNotIn(secret, encode(trace))
                observations += len(expected)
            rates.append(observations / (trials * (n - 2)))
        self.assertGreater(rates[0], rates[1] * 2)
        # Small-circle multi-target whisper delivers to up to 4 co-located peers without leaking content.
        store = self.store()
        sim = Simulation(
            store,
            Decisions(
                lambda a, v: (
                    dict(action="wait", whisper_to="r001,r002,r003,r004,r005", whisper="cabal plan")
                    if a == "r000"
                    else dict(action="wait")
                )
            ),
        )
        sim.step()
        events = store.db.execute("SELECT viewer, kind, source, detail FROM events WHERE tick=0").fetchall()
        recipients = {r[0] for r in events if r[1] == "whisper" and r[3] == "cabal plan"}
        self.assertEqual(recipients, {"r001", "r002", "r003", "r004"})
        self.assertFalse(any(r[0] == "r005" and "cabal plan" in r[3] for r in events))
        self.assertEqual(report(store.db)["audit"]["whisper_privacy"], "passed")

    def test_replay_fork_budget_resume_and_trace_determinism(self):
        store = self.store()
        full = self.store(name="full")
        partial = Simulation(store, Scripted())
        self.assertFalse(partial.step(3))
        store.fork(self.folder / "fork")
        branch = Store(self.folder / "fork")
        self.addCleanup(branch.db.close)
        self.assertEqual(report(branch.db)["audit"]["verified_ticks"], 0)
        for db, workers, budget in ((full, 1, None), (store, 3, 3), (branch, 4, 2)):
            sim = Simulation(db, Scripted(), workers=workers)
            while db.world().tick < 5:
                sim.step(budget)
            self.assertEqual(db.replay()["verified_ticks"], 5)
        paths = []
        for index, db in enumerate((full, store, branch)):
            self.assertEqual(db.world(), full.world())
            path = self.folder / f"trace{index}"
            report(db.db, path)
            paths.append(path.read_bytes())
        self.assertEqual(paths, [paths[0]] * 3)
        self.assertEqual(len(paths[0].splitlines()), 5)
        w = world()
        intents = {
            "r000": Intent("force", "person:r001"),
            "r002": Intent("force", "person:r001"),
            "r003": Intent("guard", "person:r001"),
            "r004": Intent("move", "annex"),
        }
        clone = deepcopy(w)
        clone2 = deepcopy(w)
        t1, t2 = [], []
        e1 = turn(w, intents, t1)
        self.assertEqual(e1, turn(clone, dict(reversed(intents.items())), t2))
        self.assertEqual(e1, turn(clone2, intents))
        self.assertEqual(t1, t2)
        self.assertEqual(w, clone)
        self.assertEqual(w, clone2)

    def test_polity_compression_retains_high_salience_events_and_provenance(self):
        spec = fixture()
        spec["notices"] = [dict(tick=0, place="atrium", source="sky", text="storm warning")]

        def choose(a, v):
            if a == "r000":
                return dict(action="wait", whisper="a private message", whisper_to="r001", speech="public")
            return dict(action="wait", speech="talk")

        store = self.store(spec)
        sim = Simulation(store, Decisions(choose), window=2, keep=0)
        for _ in range(8):
            sim.step()
        audit = report(store.db)["audit"]
        self.assertGreater(audit["digest_entries"], 0)
        self.assertGreater(audit["recall_entries"], 0)
        digest = store.mind("r001")["digest"]
        self.assertEqual({e["kind"] for e in digest}, {"whisper", "notice", "speech"})
        self.assertTrue(any(e["source"] == "sky" for e in digest))
        events = [dict(tick=0, kind="death", source="x", detail="combat")]
        for tick in range(20):
            events.append(dict(tick=tick, kind="speech", source="y", detail="hello"))
        self.assertIn(events[0], compress([], events))
        store.db.execute(
            "UPDATE turns SET digest=? WHERE actor='r002' AND tick=4",
            (encode([dict(tick=0, kind="whisper", source="r000", detail="a private message")]),),
        )
        with self.assertRaisesRegex(ValueError, "digest source"):
            report(store.db)
        store.db.rollback()
        store.db.execute("UPDATE events SET detail='a private message' WHERE kind='spotted_whisper'")
        with self.assertRaisesRegex(ValueError, "third-party whisper"):
            report(store.db)

    def test_iemp_flows_endorsements_and_attributable_news_delay(self):
        spec = fixture()
        spec["default_residents"] = 4
        spec["groups"]["circle"].update(exact_count=1, initial_inv={"badge": 1, "token": 2})
        del spec["places"]["atrium"]["initial_items"]["badge"]
        spec["groups"]["visitors"]["home"] = "annex"
        spec["shocks"] = [
            dict(
                tick=0,
                kind="items_delta",
                item="token",
                delta=1,
                place="atrium",
                announce_places=["atrium"],
                source="desk",
                text="One additional token arrived.",
            )
        ]
        store = self.store(spec)
        people = store.world().people
        courier = next(a for a, p in people.items() if p.role == "circle")
        peer = next(a for a, p in people.items() if p.role == "visitors")
        mid = "d0-" + courier

        def choose(a, v):
            if a == courier:
                return [
                    dict(action="write", target="post", item="badge", detail="receipt"),
                    dict(action="produce", target=mid),
                    dict(action="move", target="annex"),
                    dict(
                        action="give", target=peer, item="token", qty="2", speech=spec["shocks"][0]["text"]
                    ),
                    dict(action="post", target=mid),
                    dict(action="wait"),
                ][v["tick"]]
            return (
                dict(action="endorse", target=mid) if v["tick"] == 5 and a == peer else dict(action="wait")
            )

        sim = Simulation(store, Decisions(choose))
        for _ in range(6):
            sim.step()
        by_tick = report(store.db)["iemp_timeline"]
        self.assertEqual(by_tick[0]["I"]["new_documents"], 1)
        self.assertEqual(by_tick[1]["I"]["copies"], 3)
        self.assertEqual(
            by_tick[2]["P"]["document_coverage"][mid], dict(places=["annex", "atrium"], share=1)
        )
        metrics = by_tick[3]
        self.assertEqual(metrics["E"]["net_flows"]["circle"]["visitors"]["token"], 2)
        self.assertEqual(metrics["E"]["net_flows"]["visitors"]["circle"]["token"], -2)
        self.assertEqual(metrics["E"]["room_kinds"]["fungible"], 1)
        self.assertEqual(metrics["E"]["group_kinds"]["visitors"]["fungible"], 2)
        self.assertEqual(
            metrics["P"]["seals"]["badge"], [dict(place="annex", actor=courier, group="circle", qty=1)]
        )
        arrival = next(iter(metrics["P"]["shock_notice_latency"].values()))["places"]["annex"]
        self.assertEqual(arrival, dict(tick=3, delay=3, hops=1))
        self.assertEqual(by_tick[5]["I"]["board_documents"], 1)
        self.assertEqual(by_tick[5]["I"]["board_postings"], 2)
        self.assertEqual(by_tick[5]["I"]["sealed_share"], 1)
        self.assertEqual(by_tick[5]["I"]["signatures"], 2)
        self.assertEqual(by_tick[5]["I"]["author_group_shares"]["circle"], 1)
        self.assertEqual(by_tick[5]["I"]["cross_group_share"], 0.5)

    def test_written_notice_requires_an_attributable_reader(self):
        spec = fixture()
        spec["groups"]["circle"].update(exact_count=1)
        spec["groups"]["visitors"]["home"] = "annex"
        spec["shocks"] = [
            dict(
                tick=0,
                kind="drop_doc",
                place="atrium",
                source="desk",
                text="Supply count changed.",
                announce_places=[],
            )
        ]
        store = self.store(spec)
        courier = next(a for a, p in store.world().people.items() if p.role == "circle")
        peer = next(a for a, p in store.world().people.items() if p.role == "visitors")

        def choose(a, v):
            if a == courier:
                if v["tick"] == 1:
                    return dict(action="write", detail=v["board"][0]["text"])
                if v["tick"] == 2:
                    return dict(action="move", target="annex")
                if v["tick"] == 3:
                    return dict(action="give", target=peer, item="d1-" + a)
            return dict(action="wait")

        sim = Simulation(store, Decisions(choose))
        for _ in range(5):
            sim.step()
        timeline = report(store.db)["iemp_timeline"]
        arrivals = [next(iter(t["P"]["shock_notice_latency"].values()))["places"] for t in timeline]
        self.assertEqual(arrivals[0], {})
        self.assertEqual(arrivals[1]["atrium"], dict(tick=1, delay=1, hops=0))
        self.assertNotIn("annex", arrivals[3])
        self.assertEqual(arrivals[4]["annex"], dict(tick=4, delay=4, hops=1))

    def test_attribution_credits_originating_decisions_across_profile_changes(self):
        def choose(actor, v):
            actions = {
                (0, "r000"): dict(action="write", target="post", detail="Receipt"),
                (0, "r001"): dict(action="offer", target="none", item="ration", detail="swap"),
                (1, "r002"): dict(action="endorse", target="d0-r000"),
                (1, "r003"): dict(action="accept", target="o0-r001"),
                (2, "r004"): dict(action="force", target="person:r005"),
            }
            return actions.get((v["tick"], actor), dict(action="wait"))

        store = self.store()
        sim = Simulation(store, Decisions(choose))
        for _ in range(3):
            sim.step()
        # Attribution must come from archived turn metrics, even if routing later changes.
        with store.db:
            rows = store.db.execute("SELECT actor,tick,metrics FROM turns").fetchall()
            for actor, tick, raw in rows:
                profile = "origin" if tick == 0 and actor in ("r000", "r001") else "next"
                metrics = json.loads(raw)
                metrics.update(profile=profile, model="shared", prompt_tokens=2, cached_tokens=1)
                store.db.execute(
                    "UPDATE turns SET metrics=? WHERE actor=? AND tick=?", (encode(metrics), actor, tick)
                )
        out = report(store.db)["backend_attribution"]
        origin, other = out["by_profile"]["origin"], out["by_profile"]["next"]
        self.assertEqual(
            (
                origin["actors"],
                origin["decisions"],
                origin["contracts"],
                origin["documents"],
                origin["endorsements_received"],
            ),
            (["r000", "r001"], 2, 1, 1, 1),
        )
        self.assertEqual(
            (other["contracts"], other["documents"], other["combat_participations"], other["combat_wins"]),
            (1, 0, 2, 1),
        )
        combined = out["by_model"]["shared"]
        self.assertEqual(
            (
                combined["contracts"],
                combined["prompt_tokens"],
                combined["cached_tokens"],
                combined["survival_rate"],
            ),
            (1, 48, 24, 1),
        )

    def test_house_arrest_emerges_from_path_star_guard(self):
        def decide(a, v):
            if a in ("r001", "r002", "r003"):
                if v["tick"] == 0:
                    return dict(action="guard", target="path:*")
                if v["tick"] == 2:
                    return dict(action="guard", target="none")
                return dict(action="wait")
            if a == "r000":
                return [
                    dict(action="move", target="annex"),
                    dict(action="force", target="path:annex"),
                    dict(action="move", target="annex"),
                ][v["tick"]]
            return dict(action="wait")

        spec = fixture()
        spec["analysis_markers"] = dict(
            focal_groups=["circle"], focal_places=["annex"], focal_seals=["badge"]
        )
        store = self.store(spec)
        sim = Simulation(store, Decisions(decide))
        for _ in range(3):
            sim.step()
        a = store.world().people["r000"]
        self.assertEqual(a.place, "annex")
        self.assertLess(a.health, 10)
        r = report(store.db)
        first, second, third = r["iemp_timeline"]
        self.assertIn("r000", first["P"]["trapped"])
        self.assertEqual(second["M"]["wins"]["path"]["defenders"], 1)
        self.assertEqual(third["P"]["trapped"], [])
        self.assertEqual(third["M"]["dropped"], 3)
        self.assertEqual(len(r["focal_summary"]), 3)
        self.assertEqual(sum(r["focal_summary"][0]["places"]["annex"]["paths"]["atrium"].values()), 3)
        # A gate at the destination blocks the return direction too and is visible from both sides.
        w = store.world()
        turn(w, {"r001": Intent("guard", "path:annex"), "r000": Intent("move", "atrium")})
        self.assertEqual(w.people["r000"].place, "annex")
        self.assertEqual(w.observe("r000")["guards"]["paths"]["atrium"]["count"], 1)
        turn(
            w,
            {"r001": Intent("guard", "path:annex", detail="allow:r000"), "r000": Intent("move", "atrium")},
        )
        self.assertEqual(w.people["r000"].place, "atrium")
