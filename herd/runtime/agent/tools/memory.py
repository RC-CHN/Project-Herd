"""Queries over the resident's delivered transcript and private turn-local notebook."""

import re


SOCIAL_EVENTS = frozenset(
    {"speech", "received", "whisper", "whisper_sent", "spotted_whisper", "contract", "combat_witness"}
)


def recall_person(box, actor_id):
    identifier = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(actor_id) + r"(?![A-Za-z0-9_-])")
    recalled = []
    for event in reversed(box.delivered_events):
        kind = event["kind"]
        if kind not in SOCIAL_EVENTS:
            continue
        if actor_id in event["source"].split(",") or identifier.search(event["detail"]):
            recalled.append(event)
            if len(recalled) == 10:
                break
    return recalled[::-1]


def search_memory(box, query):
    needle = query.casefold()
    matches = []
    for event in reversed(box.delivered_events):
        if any(needle in event[k].casefold() for k in ("source", "kind", "detail")):
            matches.append(event)
            if len(matches) == 10:
                break
    matches.reverse()
    for key, note in box.notes.items():
        if needle in key.casefold() or needle in note["text"].casefold():
            matches.append(
                {
                    "tick": note["tick"],
                    "kind": "private_note",
                    "source": box.actor,
                    "key": key,
                    "detail": note["text"],
                }
            )
    return sorted(matches, key=lambda e: e["tick"])[-10:]


def read_notes(box):
    return box.notes.copy()


def write_note(box, key, text):
    box.notes[key] = {"tick": box.tick, "text": text}
    box.note_ops.append({"op": "write", "key": key, "text": text})
    return {"key": key, "written": True}


def delete_note(box, key):
    box.notes.pop(key, None)
    box.note_ops.append({"op": "delete", "key": key})
    return {"key": key, "deleted": True}
