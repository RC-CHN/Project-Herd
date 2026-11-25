"""Verify what each resident saw, recalled and recorded across a replay."""

from collections import Counter, defaultdict
from dataclasses import replace
import json

from herd.model import Intent
from herd.runtime.storage.notes import apply_note_ops, note_ops_for
from herd.runtime.storage.replay import same_json


def check(condition, message):
    if not condition:
        raise ValueError(message)


def evidence(event):
    check(set(event) == {"tick", "source", "kind", "detail"}, "unexpected evidence fields")
    return event["tick"], event["kind"], event["source"], event["detail"]


class Audit:
    def __init__(self):
        self.delivered = defaultdict(Counter)
        self.seen = defaultdict(dict)
        self.notes = defaultdict(dict)
        self.cuts = defaultdict(lambda: -1)
        self.counts = Counter()

    def frame(self, row, world, index, metrics):
        a, tick = row["actor"], row["tick"]
        frame = json.loads(row["frame"])
        view = frame["view"]
        check(
            set(frame) <= {"view", "notes", "events", "recall", "omitted_events", "omitted_notes"},
            "unexpected frame fields",
        )
        check(same_json(view, world.observe(a, index)), f"{a}/{tick}: opening view mismatch")
        check(
            set(view)
            <= {
                "tick",
                "day",
                "time_of_day",
                "self",
                "tag",
                "size",
                "initial_size",
                "discipline",
                "roe",
                "force_modes",
                "place",
                "place_label",
                "energy",
                "max_energy",
                "health",
                "max_health",
                "energy_decay",
                "clock_interval",
                "ticks_to_decay",
                "inv",
                "docs",
                "paths",
                "facility",
                "items",
                "room_docs",
                "carry_weight",
                "carry_cap",
                "room_comms",
                "can_remote_comm",
                "contacts",
                "near",
                "others_in_room",
                "room_count",
                "guards",
                "board",
                "board_slots",
                "offers",
                "own_offers",
                "own_posture",
                "recent_room",
                "item_info",
            },
            "private world fields",
        )
        check(
            all(
                set(p) == {"id", "tag", "size", "disciplined", "roe", "known", "rels", "armed", "wounded", "weak"}
                for p in view["near"]
            ),
            "private peer fields",
        )
        check(
            all(
                set(p) == {"id", "tag", "size", "disciplined", "roe", "known", "rels"}
                for p in view["others_in_room"]
            ),
            "private room peer fields",
        )
        check(
            all(set(c) == {"id", "tag", "rels"} for c in view["contacts"]),
            "private contact fields",
        )
        check(
            all(set(o) == {"id", "issuer", "give", "want", "mode"} for o in view["offers"]),
            "private offer terms",
        )
        check(
            all(o.get("issuer") == a for o in view["own_offers"]),
            "foreign own_offers entry",
        )
        actual, prior = Counter(map(evidence, frame["events"])), self.delivered[a]
        check(
            not (actual - prior) and (prior - actual).total() == frame.get("omitted_events", 0),
            f"{a}/{tick}: delivery",
        )
        notes = self.notes[a]
        check(
            all(k in notes and same_json(notes[k], v) for k, v in frame["notes"].items())
            and len(notes) - len(frame["notes"]) == frame.get("omitted_notes", 0),
            f"{a}/{tick}: notes",
        )
        check(self.cuts[a] <= row["cut"] < tick, f"{a}/{tick}: invalid cut")
        self.cuts[a] = row["cut"]
        for kind, entries in (("digest", json.loads(row["digest"])), ("recall", frame["recall"])):
            for e in entries:
                check(self.seen[a].get(evidence(e), tick + 1) <= row["cut"], f"{a}/{tick}: {kind} source")
                self.counts[kind + "_entries"] += 1
        for entry in actual:
            self.seen[a].setdefault(entry, tick)
        self.counts["resident_frames"] += 1
        self.counts["compressions"] += row["compressed"]
        intent = Intent.parse(row["reply"], a, view)
        operations = note_ops_for(intent, metrics)
        return frame, intent, operations

    @staticmethod
    def whisper_targets(actor, intent, opening, opening_comms=None):
        if not intent.whisper or not intent.whisper_to:
            return []
        sender_comms, sender_contacts = (
            opening_comms.get(actor, (False, ())) if opening_comms is not None else (False, ())
        )
        targets = []
        for raw in intent.whisper_to.split(","):
            peer = raw.strip()
            if not peer or peer == actor or peer in targets or peer not in opening:
                continue
            local = opening[actor] == opening[peer]
            remote = (
                not local
                and sender_comms
                and peer in sender_contacts
                and opening_comms is not None
                and opening_comms.get(peer, (False, ()))[0]
            )
            if local or remote:
                targets.append(peer)
                if len(targets) == 4:
                    break
        return targets

    def whispers(self, intents, opening, events, opening_comms=None):
        # Use opening co-location, comms capability and contacts independently of the trace.
        expected = Counter()
        observers = {}
        for a, i in intents.items():
            targets = self.whisper_targets(a, i, opening, opening_comms)
            if targets:
                for b in targets:
                    expected[b, "whisper", a, i.whisper] += 1
                label = ",".join(targets)
                expected[a, "whisper_sent", label, i.whisper] += 1
                local_targets = {b for b in targets if opening.get(b) == opening[a]}
                observers[a] = (local_targets, label)
        actual = Counter()
        for viewer, _, kind, source, detail in events:
            if kind in ("whisper", "whisper_sent"):
                actual[viewer, kind, source, detail] += 1
            if kind == "spotted_whisper":
                entry = observers.get(source)
                check(
                    entry
                    and viewer not in entry[0]
                    and viewer != source
                    and opening.get(viewer) == opening[source]
                    and detail == f"privately whispered with {entry[1]}",
                    "third-party whisper exposure",
                )
        check(actual == expected, "private whisper deliveries")
        self.counts["whispers"] += len(observers)

    def verify_whisper_isolation(self, shadow, world, intents, opening, events, note_events, tick, opening_comms=None):
        private_probe = {
            a: replace(i, whisper=("x" if i.whisper != "x" else "y")) if i.whisper else i
            for a, i in intents.items()
        }
        probe_events = shadow.settle(private_probe)
        probe_events.extend(note_events)
        permitted = {}
        for a, i in intents.items():
            targets = self.whisper_targets(a, i, opening, opening_comms)
            if targets:
                for b in targets:
                    permitted[b, "whisper", a] = a
                permitted[a, "whisper_sent", ",".join(targets)] = a
        expected_probe = []
        for viewer, et, kind, source, detail in events:
            sender = permitted.get((viewer, kind, source))
            expected_probe.append(
                (viewer, et, kind, source, private_probe[sender].whisper if sender else detail)
            )
        check(
            probe_events == expected_probe and shadow == world,
            f"whisper content affected third-party state at {tick}",
        )

    def close_tick(self, operations, events, tick):
        self.delivered.clear()
        for viewer, et, kind, source, detail in events:
            self.delivered[viewer][et, kind, source, detail] += 1
        for actor, ops in operations.items():
            apply_note_ops(self.notes[actor], ops, tick)
            self.counts["note_ops"] += len(ops)

    def verify_notes(self, rows, people):
        archived = {actor: json.loads(notes) for actor, notes in rows}
        check(archived.keys() == people.keys(), "resident archive mismatch")
        for actor, notes in archived.items():
            check(same_json(notes, self.notes[actor]), f"{actor}: final private notes mismatch")
