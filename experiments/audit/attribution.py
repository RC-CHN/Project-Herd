"""Attribute observed outcomes to the decisions that produced them, across route changes."""

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Cohort:
    actors: set[str] = field(default_factory=set)
    decisions: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    loop_steps: int = 0
    contracts: int = 0
    documents: int = 0
    endorsements_received: int = 0
    combat_participations: int = 0
    combat_wins: int = 0
    combat_deaths: int = 0

    def decision(self, actor, metrics):
        self.actors.add(actor)
        self.decisions += 1
        self.prompt_tokens += metrics.get("prompt_tokens") or 0
        self.cached_tokens += metrics.get("cached_tokens") or 0
        self.loop_steps += metrics.get("loop_steps", 0)

    def summary(self, world):
        residents = [world.people[actor] for actor in self.actors]
        return {
            **vars(self),
            "actors": sorted(self.actors),
            "mean_loop_steps": self.loop_steps / self.decisions,
            "survival_rate": sum(p.is_alive for p in residents) / len(residents),
            "mean_energy": sum(p.energy for p in residents) / len(residents),
        }


class BackendAttribution:
    def __init__(self):
        self.by_profile = defaultdict(Cohort)
        self.by_model = defaultdict(Cohort)
        self.authorship = {}
        self.offer_origins = {}

    def record_tick(self, world, decisions, trace):
        labels = {}
        for actor, metrics in decisions.items():
            profile, model = metrics.get("profile") or "unknown", metrics.get("model") or "unknown"
            labels[actor] = profile, model
            self.by_profile[profile].decision(actor, metrics)
            self.by_model[model].decision(actor, metrics)

        contracts = set()
        for event in trace:
            if event["type"] == "doc":
                doc_id = event["doc_id"]
                if event["kind"] == "write":
                    author = event["actor"]
                    self.authorship[doc_id] = labels[author]
                    self.increment(labels[author], "documents")
                elif (
                    event["kind"] == "endorse"
                    and doc_id in self.authorship
                    and event["actor"] != world.documents[doc_id].author
                ):
                    self.increment(self.authorship[doc_id], "endorsements_received")
            elif event["type"] == "combat":
                for actor in event["attackers"] + event["defenders"]:
                    self.increment(labels[actor], "combat_participations")
                for actor in event[event["winner"]]:
                    self.increment(labels[actor], "combat_wins")
                for actor in event["deaths"]:
                    self.increment(labels[actor], "combat_deaths")
            elif event["type"] == "transfer" and event["kind"] in ("swap", "pledge"):
                oid = event["offer_id"]
                if oid not in contracts:
                    issuer, origin = self.offer_origins[oid]
                    buyer = event["to"] if event["from"] == issuer else event["from"]
                    self.credit_contract(origin, labels[buyer])
                    contracts.add(oid)

        # An accepted offer belongs to the issuer's profile at publication.
        # Expired and settled offers need no attribution state in the next round.
        self.offer_origins = {
            oid: (o.issuer, labels[o.issuer]) if o.tick == world.tick - 1 else self.offer_origins[oid]
            for oid, o in world.offers.items()
        }

    def increment(self, labels, field_name):
        profile, model = labels
        for cohort in (self.by_profile[profile], self.by_model[model]):
            setattr(cohort, field_name, getattr(cohort, field_name) + 1)

    def credit_contract(self, issuer, buyer):
        for profile in {issuer[0], buyer[0]}:
            self.by_profile[profile].contracts += 1
        for model in {issuer[1], buyer[1]}:
            self.by_model[model].contracts += 1

    def summary(self, world):
        return {
            "by_profile": {k: v.summary(world) for k, v in sorted(self.by_profile.items())},
            "by_model": {k: v.summary(world) for k, v in sorted(self.by_model.items())},
        }
