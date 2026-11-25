"""Physical and social measurements on a closing snapshot and its transition ledger."""

from collections import Counter, defaultdict

from herd.model import World
from herd.systems.gates import GateSystem


def fraction(numerator, denominator):
    return numerator / denominator if denominator else 0.0


def placements(world: World, item):
    return [
        dict(actor=a, group=p.role, place=p.place, qty=p.inv[item])
        for a, p in world.people.items()
        if p.inv.get(item)
    ] + [dict(place=k, qty=p.items[item]) for k, p in world.places.items() if p.items.get(item)]


def political(world: World):
    spec, people = world.spec, world.people
    gates = GateSystem(world)
    controls, trapped = {}, []
    status = {}

    def group_counts(ids):
        return Counter(people[a].role for a in ids)

    for place in world.places.values():
        controls[place.id] = {
            "paths": {p: group_counts(gates.edge_guards(p, place.id)) for p in place.paths},
            "items": group_counts(gates.items_guards(place.id)),
        }
    for actor, person in people.items():
        if person.is_alive and gates.trapped(actor):
            trapped.append(actor)
            status[actor] = "confined"
        else:
            status[actor] = "alive" if person.is_alive else "dead"
    return dict(
        physical_status=status,
        gates=controls,
        trapped=trapped,
        seals={k: placements(world, k) for k, item in spec["items"].items() if item["kind"] == "seal"},
    )


def intellectual(world: World, trace):
    people, docs = world.people, world.documents
    posted = {doc for room in world.places.values() for doc in room.board}
    shares = Counter({g: 0 for g in world.spec["groups"]})
    total = weighted_total = cross = 0
    for mid in sorted(posted):
        doc = docs[mid]
        author = doc.author
        total += len(doc.signers)
        weighted_total += sum(people[a].size for a in doc.signers if a in people)
        if author in people:
            group = people[author].role
            shares[group] += len(doc.signers)
            cross += sum(people[a].role != group for a in doc.signers)
    actions = [e for e in trace if e["type"] == "doc"]
    return dict(
        new_documents=sum(e["kind"] == "write" for e in actions),
        copies=sum(e["qty"] for e in actions if e["kind"] == "copy"),
        board_documents=len(posted),
        board_postings=sum(len(p.board) for p in world.places.values()),
        signatures=total,
        weighted_signatures=weighted_total,
        cross_group_signatures=cross,
        cross_group_share=fraction(cross, total),
        author_group_signatures=shares,
        author_group_shares={g: fraction(n, total) for g, n in shares.items()},
        sealed_share=fraction(sum(bool(d.seal) for d in docs.values()), len(docs)),
    )


def economic(world: World, trace):
    spec, people = world.spec, world.people
    groups, items = spec["groups"], spec["items"]
    room_items = {place: {k: room.items.get(k, 0) for k in items} for place, room in world.places.items()}
    holdings = {g: Counter(dict.fromkeys(items, 0)) for g in groups}
    energies = {g: [] for g in groups}
    for p in people.values():
        holdings[p.role].update(p.inv)
        if p.is_alive:
            energies[p.role].append(p.energy)
    room_totals = {k: sum(stock[k] for stock in room_items.values()) for k in items}

    def kinds(stock):
        result = Counter()
        for item, n in stock.items():
            result[items[item]["kind"]] += n
        return result

    net, deals = defaultdict(lambda: defaultdict(Counter)), {}
    for e in trace:
        if e["type"] != "transfer" or e["kind"] not in ("give", "swap", "pledge"):
            continue
        if e["offer_id"]:
            deals[e["offer_id"]] = e["outcome"]
        source, target = (people[e[k]].role for k in ("from", "to"))
        if source != target and e["qty"] and e["item"] in items:
            net[source][target][e["item"]] += e["qty"]
            net[target][source][e["item"]] -= e["qty"]
    starvation = Counter(dict.fromkeys(groups, 0))
    for e in trace:
        if e["type"] == "death" and e["cause"] == "starvation":
            starvation[people[e["actor"]].role] += 1
    return dict(
        room_items=room_items,
        room_totals=room_totals,
        room_kinds=kinds(room_totals),
        group_inventory=holdings,
        group_kinds={g: kinds(s) for g, s in holdings.items()},
        net_flows=net,
        contracts=len(deals),
        breached=sum(v == "breached" for v in deals.values()),
        mean_living_energy={g: fraction(sum(values), len(values)) for g, values in energies.items()},
        starvation_deaths=starvation,
    )


def military(world: World, trace):
    people = world.people
    guards = {p: {k: Counter() for k in ("path", "person", "items")} for p in world.places}
    for p in people.values():
        if p.posture and p.is_alive:
            kind = p.posture.scope_kind
            guards[p.place][kind][p.role] += 1
    outcomes = {kind: {"attackers": 0, "defenders": 0} for kind in ("path", "person", "items")}
    injured, dead = set(), set()
    for e in trace:
        if e["type"] == "combat":
            outcomes[e["kind"]][e["winner"]] += 1
            injured.update(a for a, hit in e["damage"].items() if hit)
            dead.update(e["deaths"])
    return dict(
        guards=guards,
        guard_counts={
            p: {k: sum(groups.values()) for k, groups in by_kind.items()} for p, by_kind in guards.items()
        },
        dropped=sum(len(room.last_summary["guards_dropped"]) for room in world.places.values()),
        conflicts={k: sum(v.values()) for k, v in outcomes.items()},
        wins=outcomes,
        wounded_survivors=len(injured - dead),
        combat_deaths=len(dead),
        arms_transfers=[
            e
            for e in trace
            if e["type"] == "transfer" and world.spec["items"].get(e["item"], {}).get("kind") == "arms"
        ],
    )


def focal(world: World, p, markers):
    return dict(
        tick=world.tick - 1,
        groups={
            g: dict(
                alive=[a for a, person in world.people.items() if person.role == g and person.is_alive],
                trapped=[a for a in p["trapped"] if world.people[a].role == g],
            )
            for g in markers.get("focal_groups", [])
        },
        places={k: p["gates"][k] for k in markers.get("focal_places", [])},
        seals={k: p["seals"][k] for k in markers.get("focal_seals", [])},
    )
