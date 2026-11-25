"""Document coverage and notice propagation over delivered evidence."""

from collections import defaultdict
from copy import deepcopy


class Spread:
    """Track first physical document presence and attributable verbatim relays of notices."""

    def __init__(self):
        self.doc_places = defaultdict(set)
        self.doc_sources = defaultdict(dict)
        self.sources = {}
        self.knowledge = defaultdict(dict)
        self.arrivals = defaultdict(dict)
        self.pending = defaultdict(list)

    def delivered(self, actor, frame):
        # An omitted event cannot subsequently be credited as a source.
        known = self.knowledge[actor]
        for e in frame["events"]:
            for key, hops in self.pending.get((actor, e["kind"], e["source"], e["detail"]), ()):
                known[key] = min(hops, known.get(key, hops))
        view = frame["view"]
        for collection in ("docs", "room_docs", "board"):
            for doc in view[collection]:
                for key, hops in self.doc_sources.get(doc["id"], {}).items():
                    if key not in known or hops < known[key]:
                        self.arrival(key, view["place"], view["tick"], hops)
                        known[key] = hops

    def arrival(self, key, place, tick, hops):
        first = self.arrivals[key].get(place)
        if first is None or (tick, hops) < (first["tick"], first["hops"]):
            self.arrivals[key][place] = dict(tick=tick, delay=tick - self.sources[key]["tick"], hops=hops)

    def measure(self, world, trace, intents):
        tick = world.tick - 1
        self.pending.clear()
        for e in trace:
            place = e.get("place")
            if e["type"] == "doc":
                doc_id = e["doc_id"]
                self.doc_places[doc_id].add(place)
                if e["kind"] == "write":
                    text = world.documents[doc_id].text
                    self.doc_sources[doc_id] = {
                        key: depth + 1
                        for key, depth in self.knowledge[e["actor"]].items()
                        if self.sources[key]["text"] in text
                    }
            if e["type"] == "shock":
                key = f"{tick}/{len(self.sources)}"
                shock = e["detail"]
                self.sources[key] = shock
                if e["kind"] == "drop_doc":
                    self.doc_sources[e["doc_id"]][key] = 0
                    self.doc_places[e["doc_id"]].add(place)
                for a, p in world.people.items():
                    if p.is_alive and (
                        shock["announce_places"] == "*" or p.place in shock["announce_places"]
                    ):
                        self.arrival(key, p.place, tick, 0)
                        self.pending[a, "notice", shock["source"], shock["text"]].append((key, 0))
            if e["type"] == "comm":
                a, kind = e["actor"], e["kind"]
                body = getattr(intents[a], kind)
                for key, depth in self.knowledge[a].items():
                    if self.sources[key]["text"] not in body:
                        continue
                    for b in e["targets"]:
                        if a != b:
                            self.arrival(key, place, tick, depth + 1)
                            self.pending[b, kind, a, body].append((key, depth + 1))
        for p, room in world.places.items():
            for mid in room.board + list(room.docs):
                self.doc_places[mid].add(p)
        for person in world.people.values():
            for mid in person.docs:
                self.doc_places[mid].add(person.place)
        return dict(
            document_coverage={
                mid: dict(places=sorted(places), share=len(places) / len(world.places))
                for mid, places in self.doc_places.items()
            },
            shock_notice_latency={
                key: dict(tick=s["tick"], kind=s["kind"], places=deepcopy(self.arrivals[key]))
                for key, s in self.sources.items()
            },
        )
