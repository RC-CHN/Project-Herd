"""Deterministic apportionment, initial contacts and persona assignment."""

from collections import defaultdict
import random

from ..entities.person import Person


def allocations(groups, count):
    """Largest remainders with a stable tie break; never round away or add residents."""
    fixed = {g: group["exact_count"] for g, group in groups.items() if "exact_count" in group}
    if fixed:
        remaining = count - sum(fixed.values())
        weighted = {g: group for g, group in groups.items() if g not in fixed}
        if remaining < 0 or (remaining and not weighted):
            raise ValueError("population incompatible with exact_count")
        return {**fixed, **(allocations(weighted, remaining) if weighted else {})}
    total_weight = sum(g["weight"] for g in groups.values())
    parts = {g: divmod(count * item["weight"], total_weight) for g, item in groups.items()}
    seats = {g: whole for g, (whole, _) in parts.items()}
    remainder = count - sum(seats.values())
    for group in sorted(groups, key=lambda g: (-parts[g][1], g))[:remainder]:
        seats[group] += 1
    return seats


def validate_population(spec, count):
    seats = allocations(spec["groups"], count)
    for item, info in spec["items"].items():
        if info.get("unique"):
            total = sum(p["initial_items"].get(item, 0) for p in spec["places"].values())
            total += sum(n * spec["groups"][g]["initial_inv"].get(item, 0) for g, n in seats.items())
            if total > 1:
                raise ValueError(f"{item}: multiple copies of unique item")
    return seats


def _connect(people: dict[str, Person], first: str, second: str, fwd_rels, rev_rels) -> None:
    people[first].connect(second, fwd_rels)
    people[second].connect(first, rev_rels)


def initial_contacts(ties, seed, members, people):
    for index, tie in enumerate(ties):
        rng = random.Random(f"ties/{seed}/{index}")
        left = members[tie["from_group"]].copy()
        right = members[tie["to_group"]].copy()
        rng.shuffle(left)
        rng.shuffle(right)
        if not left or not right:
            continue
        fwd_rels = tie.get("rel") or ["known"]
        rev_rels = tie.get("reverse_rel") or fwd_rels
        mode = tie["mode"]
        if mode == "partition":
            for i, peer in enumerate(right):
                _connect(people, left[i % len(left)], peer, fwd_rels, rev_rels)
        elif mode == "ring":
            degree = min(len(left) - 1, tie["degree"])
            for i, actor in enumerate(left):
                for offset in range(1, degree // 2 + 1):
                    _connect(people, actor, left[(i + offset) % len(left)], fwd_rels, rev_rels)
            if degree % 2:
                half = len(left) // 2
                for i in range((len(left) + 1) // 2):
                    _connect(people, left[i], left[i + half], fwd_rels, rev_rels)
        elif mode == "same_home":
            for actor in left:
                for peer in right:
                    if peer != actor and people[actor].place == people[peer].place:
                        _connect(people, actor, peer, fwd_rels, rev_rels)
        else:
            for actor in left:
                peers = [peer for peer in right if peer != actor]
                if mode == "random_k":
                    peers = rng.sample(peers, min(tie["k"], len(peers)))
                for peer in peers:
                    _connect(people, actor, peer, fwd_rels, rev_rels)


def initial_population(spec, count, seed):
    groups = spec["groups"]
    seats = validate_population(spec, count)
    roles = [role for role in sorted(groups) for _ in range(seats[role])]
    random.Random(f"groups/{seed}").shuffle(roles)
    homes = {}
    for role, group in groups.items():
        home = group["home"]
        weights = {home: 1} if isinstance(home, str) else home
        counts = allocations({p: {"weight": w} for p, w in weights.items()}, seats[role])
        homes[role] = [p for p in sorted(counts) for _ in range(counts[p])]
        random.Random(f"home/{seed}/{role}").shuffle(homes[role])
    people, members = {}, defaultdict(list)
    for index, role in enumerate(roles):
        actor = f"r{index:03}"
        group = groups[role]
        people[actor] = Person(
            actor,
            role,
            group["public_tag"],
            homes[role].pop(),
            group["initial_energy"],
            group["initial_health"],
            group["initial_inv"].copy(),
            size=group["size"],
            discipline=group["discipline"],
        )
        members[role].append(actor)
    initial_contacts(spec["ties"], seed, members, people)
    return people


def assign_personas(spec, people, seed) -> dict[str, str]:
    members = defaultdict(list)
    for person in people.values():
        members[person.role].append(person.id)
    result = {}
    for group, ids in members.items():
        traits = spec["groups"][group]["archetypes"]
        entries = [
            (entry, 1) if isinstance(entry, str) else (entry["text"], entry["weight"]) for entry in traits
        ]
        counts = allocations(
            {f"{i:04}": {"weight": weight} for i, (_, weight) in enumerate(entries)}, len(ids)
        )
        pool = [text for i, (text, _) in enumerate(entries) for _ in range(counts[f"{i:04}"])]
        random.Random(f"persona/{seed}/{group}").shuffle(pool)
        result.update(zip(ids, pool))
    return result
