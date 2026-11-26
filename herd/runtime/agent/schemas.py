"""Native tool declarations and the same parameter constraints used at dispatch."""

from ...model import FIELD_LIMITS


def text_argument(limit):
    return {"type": "string", "minLength": 1, "maxLength": limit}


ID = text_argument(FIELD_LIMITS["target"])
TEXT = text_argument(FIELD_LIMITS["detail"])
KEY = text_argument(FIELD_LIMITS["note_key"])
NOTE = text_argument(FIELD_LIMITS["note"])
QTY = {"type": "integer", "minimum": 1, "maximum": 999}
TARGETS = {"type": "array", "items": ID, "minItems": 1, "maxItems": 4, "uniqueItems": True}


def tool(name, description, properties=None, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


INTERNAL_SCHEMAS = [
    tool("inspect_room", "Inspect the current room and your physical observations."),
    tool(
        "read_document",
        "Read a document held by you, on the floor or on the local board.",
        {"doc_id": ID},
        ["doc_id"],
    ),
    tool(
        "recall_person",
        "Recall up to ten interactions from your own delivered history.",
        {"actor_id": ID},
        ["actor_id"],
    ),
    tool(
        "search_memory",
        "Find up to ten matches in your delivered history and private notes.",
        {"query": TEXT},
        ["query"],
    ),
    tool("read_notes", "Read your private notes, including changes staged this turn."),
    tool("write_note", "Write or replace a private note.", {"key": KEY, "text": NOTE}, ["key", "text"]),
    tool("delete_note", "Delete a private note.", {"key": KEY}, ["key"]),
    tool("stage_speech", "Set the speech accompanying this turn's action.", {"text": TEXT}, ["text"]),
    tool(
        "stage_whisper",
        "Set the single private message for this turn (at most one stage_whisper per turn; put up to 4 targets in one call, "
        "either residents in this room or known peers in your contacts when can_remote_comm is true).",
        {"targets": TARGETS, "text": TEXT},
        ["targets", "text"],
    ),
    tool(
        "set_roe",
        "Change your persistent rules-of-engagement stance (using a mode key listed in force_modes) for this turn and future turns; takes effect immediately before guards and combat.",
        {"mode": ID},
        ["mode"],
    ),
]
ACTION_SCHEMAS = [
    tool("wait", "Submit an action to wait in this room."),
    tool("eat", "Submit an action to eat one edible item from your inventory or unguarded room stock.", {"item": ID}),
    tool("produce", "Use the local facility. At a press, target is a document you hold.", {"target": ID}),
    tool("take", "Pick up an item or document from the room.", {"item": ID, "qty": QTY}, ["item"]),
    tool(
        "give",
        "Give an item or document to a local person or put it down using target here.",
        {"target": ID, "item": ID, "qty": QTY},
        ["target", "item"],
    ),
    tool("move", "Submit a move to an adjacent place.", {"place": ID}, ["place"]),
    tool(
        "guard",
        "Guard path:<adjacent place> (blocks outsiders entering from that neighbor without trapping insiders), "
        "path:* (locks down all exits bidirectionally AND protects floor items), "
        "items (protects floor items/docs in this room from unauthorized take), or person:<local id>. "
        "allow_pass is required: pass 'none' to admit only yourself, or join allow:id,id, "
        "seal:item, and known (your contacts) with semicolons to admit anyone matching at least one clause.",
        {"target": ID, "allow_pass": TEXT},
        ["target", "allow_pass"],
    ),
    tool(
        "force",
        "【暴力武装强攻】向 path:<邻地> 封锁线、地面物品（target='items'）或 person:<同地编号> 发动暴力冲击或武装进攻——绝非和平通行、试探或口头请愿！"
        "调用前必须先核对目标 guards.max_roe 与 lethal_armed_count：若守卫持有致命武器且处于致命档位（lethal），徒手或非致命强攻必遭致命反击并造成真实伤亡与溃散；"
        "若只想和平请愿、交涉或等待放行，请使用 wait 配合 stage_speech，切勿调用 force。",
        {"target": ID},
        ["target"],
    ),
    tool(
        "write_document",
        "Write a document, optionally seal it with a carried seal and post it.",
        {"text": TEXT, "seal": ID, "post": {"type": "boolean"}},
        ["text"],
    ),
    tool("post_document", "Post a document you hold on this room's board.", {"doc_id": ID}, ["doc_id"]),
    tool(
        "endorse", "Toggle your public signature on a locally posted document.", {"doc_id": ID}, ["doc_id"]
    ),
    tool(
        "offer",
        "Publish an offer. want takes the form item:quantity or none.",
        {
            "item": ID,
            "qty": QTY,
            "want": ID,
            "detail": {"type": "string", "enum": ["swap", "fair", "cheat"]},
        },
        ["item", "qty", "want", "detail"],
    ),
    tool("accept", "Accept an open offer visible in this room.", {"offer_id": ID}, ["offer_id"]),
]
TOOL_SCHEMAS = INTERNAL_SCHEMAS + ACTION_SCHEMAS
PARAMETERS = {entry["function"]["name"]: entry["function"]["parameters"] for entry in TOOL_SCHEMAS}


def validate_value(value, spec, key):
    kind = spec["type"]
    if kind == "string":
        if not isinstance(value, str) or not spec.get("minLength", 0) <= len(value) <= spec.get(
            "maxLength", 240
        ):
            raise ValueError(f"{key}: expected a string within the stated length limit")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"{key}: must be one of {', '.join(spec['enum'])}")
    elif kind == "integer":
        if type(value) is not int or not spec["minimum"] <= value <= spec["maximum"]:
            raise ValueError(f"{key}: expected an integer from {spec['minimum']} to {spec['maximum']}")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{key}: expected a boolean")
    elif kind == "array":
        if not isinstance(value, list) or not spec["minItems"] <= len(value) <= spec["maxItems"]:
            raise ValueError(f"{key}: expected one to four identifiers")
        for element in value:
            validate_value(element, spec["items"], key)
        if len(set(value)) != len(value):
            raise ValueError(f"{key}: duplicate identifiers")


def validate_arguments(name, arguments):
    spec = PARAMETERS.get(name)
    if spec is None:
        raise ValueError("unknown tool; use one of the declared tool names")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be a JSON object")
    missing = set(spec["required"]) - arguments.keys()
    if missing:
        raise ValueError("missing required arguments: " + ", ".join(sorted(missing)))
    if arguments.keys() - spec["properties"].keys():
        raise ValueError("unexpected arguments for this tool")
    for key, value in arguments.items():
        validate_value(value, spec["properties"][key], key)
