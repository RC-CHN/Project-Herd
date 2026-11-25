"""A single resident's private, disposable workspace for one decision."""

from functools import cached_property
import json

from ...model import Intent, encode
from .schemas import validate_arguments
from .tools import ACTIONS, INTERNAL


class AgentToolbox:
    def __init__(self, request, store):
        self.actor = request.actor
        self.view = request.frame["view"]
        self.tick = self.view["tick"]
        self.local = getattr(request, "local", None) or {}
        self.present = set(
            self.local.get("present", [self.actor, *(p["id"] for p in self.view.get("near", []))])
        )
        self.notes = dict(self.local.get("notes", request.frame.get("notes", {})))
        self.note_ops = []
        self.tool_calls = []
        self.social = {}
        self.intent = None
        self.store = store

    @cached_property
    def delivered_events(self):
        return self.store.delivered_events(self.actor, self.tick)

    def parse_intent(self, action):
        return Intent.parse(encode({**self.social, **action}), self.actor, self.view)

    def call(self, name, arguments):
        if self.intent is not None:
            return {"ok": False, "error": "already submitted one action for this turn"}
        if not isinstance(name, str):
            return {"ok": False, "error": "tool name must be a string"}
        if name in INTERNAL:
            self.tool_calls.append(name)
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            validate_arguments(name, arguments)
            if name in INTERNAL:
                result = INTERNAL[name](self, **arguments)
            else:
                self.intent = self.parse_intent(ACTIONS[name](self, **arguments))
                result = {"submitted": self.intent.action}
            return {"ok": True, "result": result}
        except (ValueError, RecursionError) as error:
            return {"ok": False, "error": str(error)}
