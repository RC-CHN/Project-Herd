"""Offline decisions based on this resident's delivered observation."""

from ...model import encode
from ..prefix import message_key
from .base import Backend, _positive_int, _text


class Scripted(Backend):
    unit = "bytes"
    model = "scripted"
    mode = "scripted"

    def __init__(self, name="scripted", workers=None, *, max_steps=6, context_bytes=None):
        _text(name, "profile name")
        _positive_int(max_steps, "max_steps")
        self.max_steps = max_steps
        if workers is not None:
            _positive_int(workers, "workers")
        if context_bytes is not None:
            _positive_int(context_bytes, "context_bytes")
        self.name = name
        self.workers = workers
        self.context_bytes = context_bytes
        self.scope = (
            "scripted:message-framing-v1" if name == "scripted" else f"scripted:{name}:message-framing-v1"
        )

    def key(self, request):
        return message_key(request.messages)

    def decide(self, request):
        return encode(self.polity_decision(request.frame["view"])), {
            "profile": self.name,
            "model": self.model,
            "mode": self.mode,
        }

    @staticmethod
    def polity_decision(view):
        choice = (int(view["self"][1:]) + view["tick"]) % 11
        near = view["near"]
        peer = next((p["id"] for p in near if p["known"]), near[0]["id"] if near else "")
        food = [k for k, v in view["item_info"].items() if v["kind"] == "edible"]
        held = [k for k, n in view["inv"].items() if n]
        seal = next((k for k in held if view["item_info"][k]["kind"] == "seal"), "")
        if view["energy"] <= view["max_energy"] // 2 and any(k in held for k in food):
            intent = {"action": "eat"}
        elif choice == 0 and view["paths"]:
            intent = {"action": "move", "target": sorted(view["paths"])[view["tick"] % len(view["paths"])]}
        elif choice == 1 and view["paths"]:
            intent = {"action": "guard", "target": "path:*", "detail": "known"}
        elif choice == 2:
            intent = {
                "action": "write",
                "target": "post",
                "item": seal,
                "detail": "请核对本地的存量与需求。",
            }
        elif choice == 3 and view["board"]:
            intent = {"action": "endorse", "target": view["board"][-1]["id"]}
        elif choice == 4 and held and peer:
            intent = {"action": "give", "item": held[0], "target": peer}
        elif choice == 5 and held:
            intent = {"action": "offer", "item": held[0], "detail": "swap", "target": "none"}
        elif choice == 6 and view["offers"]:
            intent = {"action": "accept", "target": view["offers"][0]["id"]}
        elif view["facility"] and view["facility"]["kind"] == "press" and view["docs"]:
            intent = {"action": "produce", "target": view["docs"][0]["id"]}
        elif view["facility"] and view["facility"]["kind"] == "harvest":
            intent = {"action": "produce"}
        elif any(view["items"].get(k) for k in food):
            intent = {"action": "take", "item": next(k for k in food if view["items"].get(k))}
        else:
            intent = {"action": "wait"}
        if choice % 3 == 0 and view["board"]:
            intent["speech"] = view["board"][-1]["text"][:240]
        elif choice % 3 == 1 and peer:
            intent.update(whisper_to=peer, whisper="本地的情况如何？")
        return intent
