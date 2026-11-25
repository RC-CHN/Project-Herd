"""Project only the physical information a resident can see at the opening of a turn."""

from copy import deepcopy

from .time import diurnal


def observe(world, actor_id: str, index) -> dict:
    me = world.people[actor_id]
    room = world.places[me.place]
    known = set(me.contacts) | {actor_id}
    rules, items = world.spec["rules"], world.all_items()
    held_items = me.inv.keys() | room.items.keys()
    if room.facility and "item" in room.facility:
        held_items.add(room.facility["item"])

    def copies(bag):
        return [
            dict(
                id=doc_id,
                qty=qty,
                author=world.documents[doc_id].author,
                text=world.documents[doc_id].text,
                seal=world.documents[doc_id].seal,
            )
            for doc_id, qty in bag.items()
            if qty
        ]

    board = []
    for doc_id in room.board:
        doc = world.documents[doc_id]
        board.append(
            dict(
                id=doc_id,
                tick=doc.tick,
                author=doc.author,
                seal=doc.seal,
                text=doc.text,
                signed=len(doc.signers),
                signed_weight=sum(world.people[a].size for a in doc.signers if a in world.people),
                known_signers=[a for a in doc.signers if a in known],
            )
        )
    near = []
    near_ids = {actor_id}
    for peer_id in index.huddles[actor_id]:
        if peer_id != actor_id:
            near_ids.add(peer_id)
            peer = world.people[peer_id]
            near.append(
                dict(
                    id=peer.id,
                    tag=peer.tag,
                    size=peer.size,
                    disciplined=peer.discipline >= 0.7,
                    roe=peer.roe,
                    known=peer.id in known,
                    rels=me.rels_for(peer.id),
                    armed=peer.armed(items),
                    wounded=peer.health < rules["max_health"],
                    weak=peer.energy <= rules["low_energy_threshold"],
                )
            )
    others_in_room = []
    for peer_id in index.present[room.id]:
        if peer_id not in near_ids:
            peer = world.people[peer_id]
            others_in_room.append(
                dict(
                    id=peer.id,
                    tag=peer.tag,
                    size=peer.size,
                    disciplined=peer.discipline >= 0.7,
                    roe=peer.roe,
                    known=peer_id in known,
                    rels=me.rels_for(peer.id),
                )
            )
    offers = [
        o.public_view()
        for o in world.offers.values()
        if o.place == room.id
        and o.issuer != actor_id
        and o.is_open(world.tick)
        and world.people[o.issuer].is_alive
        and world.people[o.issuer].place == room.id
    ]
    own_offers = [
        o.issuer_view()
        for o in world.offers.values()
        if o.place == room.id and o.issuer == actor_id and o.is_open(world.tick)
    ]
    interval = rules["clock_interval"]
    day, time_of_day, _ = diurnal(world.tick, interval)
    return dict(
        tick=world.tick,
        day=day,
        time_of_day=time_of_day,
        self=actor_id,
        tag=me.tag,
        size=me.size,
        initial_size=me.initial_size,
        discipline=me.discipline,
        roe=me.roe,
        force_modes={
            k: dict(label=v["label"], severity=v["severity"], lethal=v["lethal"])
            for k, v in rules["force_modes"].items()
        },
        place=room.id,
        place_label=room.label,
        energy=me.energy,
        max_energy=rules["max_energy"],
        health=me.health,
        max_health=rules["max_health"],
        energy_decay=world.energy_decay,
        clock_interval=interval,
        ticks_to_decay=interval - world.tick % interval,
        inv=me.inv.copy(),
        docs=copies(me.docs),
        carry_weight=me.carry_weight(items),
        carry_cap=me.carry_cap(rules["carry_cap"]),
        room_comms=room.comms,
        can_remote_comm=me.can_remote_comm(room.comms, items),
        contacts=[
            dict(id=cid, tag=world.people[cid].tag, rels=me.rels_for(cid))
            for cid in me.contacts
            if cid in world.people
        ],
        room_docs=copies(room.docs),
        items=room.items.copy(),
        item_info={k: items[k].copy() for k in sorted(held_items)},
        paths={p: world.places[p].label for p in room.paths},
        facility=deepcopy(room.facility),
        near=near,
        others_in_room=others_in_room,
        room_count=len(index.present[room.id]),
        board=board,
        board_slots=rules["board_slots"],
        offers=offers,
        own_offers=own_offers,
        guards=index.gates.view(actor_id),
        own_posture=me.posture.to_dict() if me.posture else None,
        recent_room=deepcopy(room.last_summary),
    )


def local_context(world, actor_id: str, index) -> dict:
    """Capture the opening evidence available to this resident's read-only tools."""
    person = world.people[actor_id]
    room = world.places[person.place]
    readable = set(room.board)
    for bag in (room.docs, person.docs):
        readable.update(doc_id for doc_id, quantity in bag.items() if quantity > 0)
    documents = {}
    for doc_id in sorted(readable):
        doc = world.documents[doc_id]
        documents[doc_id] = dict(
            id=doc_id, author=doc.author, text=doc.text, seal=doc.seal, signers=doc.signers.copy()
        )
    return {
        "present": sorted(index.present[room.id]),
        "can_remote_comm": person.can_remote_comm(room.comms, world.all_items()),
        "contacts": list(person.contacts),
        "documents": documents,
    }
