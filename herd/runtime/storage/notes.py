"""Ordered, private note operations persisted alongside the flat physical intent."""

from ...model import FIELD_LIMITS, encode


def note_ops_for(intent, metrics):
    """Validate the two storage formats and normalize older single-note replies."""
    legacy = []
    if intent.note_key:
        if intent.note:
            legacy.append({"op": "write", "key": intent.note_key, "text": intent.note})
        else:
            legacy.append({"op": "delete", "key": intent.note_key})

    if not isinstance(metrics, dict):
        raise ValueError("expected turn metrics")
    if "mode" in metrics and metrics["mode"] not in ("agent_loop", "json", "scripted"):
        raise ValueError("unknown decision mode in turn metrics")
    if "note_ops" not in metrics:
        if metrics.get("mode") == "agent_loop":
            raise ValueError("missing agent-loop note_ops")
        return legacy

    ops = metrics["note_ops"]
    if not isinstance(ops, list):
        raise ValueError("note_ops must be an ordered list")
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in ("write", "delete"):
            raise ValueError("invalid note operation")
        fields = {"op", "key", "text"} if op["op"] == "write" else {"op", "key"}
        if set(op) != fields:
            raise ValueError("invalid note operation fields")
        key = op["key"]
        if not isinstance(key, str) or not 0 < len(key) <= FIELD_LIMITS["note_key"]:
            raise ValueError("invalid note key")
        if op["op"] == "write":
            text = op["text"]
            if not isinstance(text, str) or not 0 < len(text) <= FIELD_LIMITS["note"]:
                raise ValueError("invalid note text")
    if metrics.get("mode") == "agent_loop":
        if legacy:
            raise ValueError("agent-loop note operations must be recorded in metrics")
    elif ops != legacy:
        raise ValueError("note_ops do not match the submitted intent")
    return ops


def apply_note_ops(notes, ops, tick):
    for op in ops:
        if op["op"] == "write":
            notes[op["key"]] = {"tick": tick, "text": op["text"]}
        else:
            notes.pop(op["key"], None)


def extra_note_events(tick, intents, metrics_by_actor):
    events = []
    for actor in sorted(intents):
        intent = intents[actor]
        ops = note_ops_for(intent, metrics_by_actor[actor])
        # Physical settlement already emits the single note embedded in a JSON intent.
        if not intent.note_key:
            for op in ops:
                detail = encode([op["key"], op.get("text", "")])
                events.append((actor, tick, "private_note", actor, detail))
    return events
