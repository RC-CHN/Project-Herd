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
    tool(
        "stage_speech",
        "你此刻当面张嘴说出的公开发言。必须是现场录音式的1~3句现代日常大白话（15~65字），自然有情绪、不工整不对仗；"
        "严禁列123条陈、破折号、分号、金句排比与文言/戏台/公文腔。",
        {"text": dict(TEXT, description="15~65字现代日常大白话，像真人当面脱口而出，禁列点/破折号/分号/对仗金句/戏腔。")},
        ["text"],
    ),
    tool(
        "stage_whisper",
        "本轮唯一一条私下口信（单次最多4个目标，同屋或 can_remote_comm 为真时可发给 contacts 熟人）。"
        "必须是1~3句简短自然的私下现代大白话（15~65字），像凑耳边叮嘱或电台短呼；严禁分点汇报、破折号、分号与戏腔。",
        {
            "targets": TARGETS,
            "text": dict(TEXT, description="15~65字私下现代大白话口信，简短直白，禁列点/破折号/分号/对仗/戏腔。"),
        },
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
    tool(
        "wait",
        "原地停留、静坐抗议、当场喊话交涉、观望待命或和平施压（不打算真正动武打架时默认调用此工具，可同一步并行调用 stage_speech / stage_whisper）。",
    ),
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
        "【发动暴力袭击/武力冲卡】真正动武打架、暴力冲撞封锁线、强抢物品或攻击他人（会立刻触发真实斗殴或致命反击与伤亡）。"
        "严禁用于静坐、喊话、请愿、试探或“不开就不走/不散”——只要不想真动手打架，一律用 wait 配合 stage_speech，绝不要调用 force！",
        {
            "target": dict(
                ID,
                description="仅在决心真正发动暴力袭击或武力冲撞时填写目标（path:<邻地>、items 或 person:<同地编号>）；和平喊话或静坐请改用 wait。",
            )
        },
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
