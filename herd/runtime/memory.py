"""Resident prompt construction, on-demand/periodic compression, and context budgeting."""

from dataclasses import dataclass, field
import json

from ..model import RULES, Intent, encode
from ..model.intent import WORLD_RULES
from ..model.observation import local_context


@dataclass
class Request:
    actor: str
    messages: list
    frame: dict
    cut: int
    digest: list
    compressed: bool
    local: dict = field(default_factory=dict)


SPOKEN_STYLE_GUIDE = (
    "【自然口语铁律】公开发言（stage_speech）和私语（stage_whisper）必须是“现场录音转文字”般的现代日常大白话，不是写报告、背台词或演话剧：\n"
    '- 坏示范（禁止）："诸位，三件事请记下：一、核验、登记、留存，缺一不可；二、本队即行封门——账目一日不开，信用一日不解。"\n'
    '- 好示范（像活人）："大伙先别急，先把眼前这事弄清楚再说。东西我先看着，谁也别乱抢，有动静马上跟我讲一声。"\n'
    "- 短促直白：每次控制在1~3句、15~65字，只讲眼下最急的一两层意思，不求面面俱到、首尾呼应。\n"
    "- 彻底去书面腔与戏腔：禁止“一、二、三/①②③”列条，禁止破折号“——”、分号“；”、冒号总起分点，禁止工整对仗和装酷金句，少用连串顿号，禁止“本席、本队、本报、诸位、切勿、即行、何在”等文言或话剧腔。\n"
    "- 身份语气拉开：普通民众粗直接地气，高位决策者冷硬简短但不拽文，一线人员干脆利落；称呼用“大伙、老哥、长官、兄弟、那位”，尽量别满嘴喊角色编号。\n"
    "- 哪怕前文有人说得很文绉绉，你也坚持说松弛自然的现代大白话；只有写正式文书（write_document）时才用书面体。"
)


def format_map(spec):
    places = spec["places"]
    entries = []
    for place_id, layout in places.items():
        neighbors = "、".join(f"{places[n]['label']}({n})" for n in layout["paths"])
        tags = ("[有通讯]" if layout.get("comms") else "") + ("[有补给]" if layout.get("sustenance") else "")
        entries.append(f"{layout['label']}({place_id}){tags}通往{neighbors}")
    return "空间布局：" + "；".join(entries) + "。"


def format_static_catalog(spec, energy_decay=None):
    rules = spec["rules"]
    modes = {
        k: {
            "label": v["label"],
            "severity": v["severity"],
            "lethal": v["lethal"],
            "requires_lethal_arms": bool(v.get("requires_lethal_arms", False)),
        }
        for k, v in rules["force_modes"].items()
    }
    constants = {
        "max_energy": rules["max_energy"],
        "max_health": rules["max_health"],
        "write_energy_cost": rules.get("write_energy_cost", 0),
        "exertion_energy_cost": rules.get("exertion_energy_cost", 0),
        "clock_interval": rules["clock_interval"],
        "zero_energy_mode": rules.get("zero_energy_mode", "fatal"),
        "board_slots": rules["board_slots"],
    }
    if energy_decay is not None:
        constants["energy_decay"] = energy_decay
    items = {
        k: {ik: iv for ik, iv in v.items() if iv not in (False, 0, None, "")}
        for k, v in sorted(spec["items"].items())
    }
    return (
        "静态规则常量（不再在每轮帧中重复）："
        f"constants={encode(constants)}；"
        f"force_modes={encode(modes)}；"
        f"item_catalog={encode(items)}。\n"
        "帧压缩约定：每轮仅列出变动或非默认字段——"
        "near/others_in_room/guards 中未列出的布尔字段均为 false、未列出的列表或计数为空/0；"
        "contacts/notes/guards/near/others_in_room 未变化时沿用前文最近一次给出值；"
        "已出现过的角色后续帧省略重复 tag，已见过的文书后续帧仅列编号与最新联署/印章（全文随时用 read_document 查）；"
        "events 默认属于上一幕（tick-1）且省略你上一轮自己已发出的私语/笔记/发言回显。"
    )


def identity_block(person, persona):
    if not person.is_collective:
        return (
            "身份类型：独立个体（size=1）。这是社会动力学模拟沙箱，请忠实于角色的立场、私利、恐惧与性格以第一人称自主决策——"
            "协商、秘密结盟、撒谎、违约（cheat）、封锁软禁、夺印或动武均为沙箱内的合法策略，不必为了迎合他人或维持表面体面而违背自身核心利益。"
            "当场目击致命冲突、伤亡溃退、遗骸或要员/熟人倒下，以及听见邻室激战声响，均属重大变故，请按角色性格真实表现出震动、惊惧、愤怒、追责或立场急转，切勿视若无睹。\n"
            + "长期性格："
            + persona
        )
    if person.discipline >= 0.7:
        return (
            f"身份类型：建制集体单元（初始编制={person.initial_size}，训练度={person.discipline:.2f}，当前实有人数见每轮 view.size）。"
            "你代表这支队列严整、组织协同的建制单位，重视指挥链、阵地与纪律，受压时不易溃散，"
            "按角色立场、直属指令与自身安危自主权衡执行或观望，不必为了表面客套而软化立场。"
            "目击致命冲突、伤亡遗骸、长官/同袍倒下或听见邻室激战声响时，请按军纪与立场作出真实反应。"
            "公开发言代表集体喊话或表态；私语代表派联络员或电台接洽；笔记用于记录指令、阵地与士气。\n"
            + "群体特质："
            + persona
        )
    return (
        f"身份类型：自发集体单元（初始编制={person.initial_size}，组织度={person.discipline:.2f}，当前实有人数见每轮 view.size）。"
        "你代表这支由多名普通成员组成的群体，而非单个个人。"
        "普通自发人群很少逐字研读冗长公文，更受眼前饥饱（energy）、士气（health）、怒气、恐慌、口号与失真传闻驱动；"
        "内部意见撕裂时容易情绪化起哄、推搡冲卡、跟风移动，亲眼目睹流血伤亡、遗骸、溃兵涌入或听见邻室激战声响会立刻引爆群体恐慌、四散躲避或极端狂怒，切勿像无事发生一样继续客套。"
        "公开发言代表集体呼声或代表表态；私语代表同屋派代表私下接洽；笔记用于记录共同诉求、内部情绪与后勤状态。\n"
        + "群体特质："
        + persona
    )


def message(role, content):
    return {"role": role, "content": content}


def _action_tool_call(intent):
    action = intent.action
    if action == "wait":
        return "wait", {}
    if action == "eat":
        return "eat", ({"item": intent.item} if intent.item else {})
    if action == "produce":
        target = intent.target or intent.item
        return "produce", ({"target": target} if target else {})
    if action == "take":
        args = {"item": intent.item}
        if intent.qty:
            args["qty"] = int(intent.qty)
        return "take", args
    if action == "give":
        args = {"target": intent.target, "item": intent.item}
        if intent.qty:
            args["qty"] = int(intent.qty)
        return "give", args
    if action == "move":
        return "move", {"place": intent.target}
    if action == "guard":
        return "guard", {"target": intent.target, "allow_pass": intent.detail or "none"}
    if action == "force":
        return "force", {"target": intent.target}
    if action == "write":
        args = {"text": intent.detail}
        if intent.item:
            args["seal"] = intent.item
        if intent.target == "post":
            args["post"] = True
        return "write_document", args
    if action == "post":
        return "post_document", {"doc_id": intent.target}
    if action == "endorse":
        return "endorse", {"doc_id": intent.target}
    if action == "offer":
        return "offer", {
            "item": intent.item,
            "qty": int(intent.qty or 1),
            "want": intent.target,
            "detail": intent.detail,
        }
    if action == "accept":
        return "accept", {"offer_id": intent.target}
    return "wait", {}


def render_tool_history(tick, reply_raw):
    """Project an archived canonical Intent into standard OpenAI tool_calls + tool messages."""
    intent = Intent.parse(reply_raw)
    specs = []
    if intent.note_key:
        if intent.note:
            specs.append(("write_note", {"key": intent.note_key, "text": intent.note}, {"ok": True}))
        else:
            specs.append(("delete_note", {"key": intent.note_key}, {"ok": True}))
    if intent.speech:
        specs.append(("stage_speech", {"text": intent.speech}, {"ok": True, "staged": "speech"}))
    if intent.whisper and intent.whisper_to:
        targets = [peer.strip() for peer in intent.whisper_to.split(",") if peer.strip()]
        specs.append(("stage_whisper", {"targets": targets, "text": intent.whisper}, {"ok": True, "staged": "whisper"}))
    if intent.roe:
        specs.append(("set_roe", {"mode": intent.roe}, {"ok": True, "staged": "roe"}))
    act_name, act_args = _action_tool_call(intent)
    specs.append((act_name, act_args, {"ok": True, "committed": intent.action}))
    calls, tool_msgs = [], []
    for idx, (name, args, result) in enumerate(specs):
        call_id = f"h-{tick}-{idx}"
        calls.append({"type": "function", "id": call_id, "function": {"name": name, "arguments": encode(args)}})
        tool_msgs.append({"role": "tool", "tool_call_id": call_id, "content": encode(result)})
    return [{"role": "assistant", "content": "", "tool_calls": calls}, *tool_msgs]


DOC_PREVIEW_CHARS = 50
FOLD_TARGET_RATIO = 0.55

STATIC_VIEW_KEYS = frozenset(
    {
        "force_modes",
        "max_energy",
        "max_health",
        "write_energy_cost",
        "exertion_energy_cost",
        "clock_interval",
        "zero_energy_mode",
        "board_slots",
        "initial_size",
        "discipline",
        "self",
    }
)
OMIT_IF_EMPTY_VIEW_KEYS = (
    "docs",
    "room_docs",
    "board",
    "offers",
    "own_offers",
    "guards",
    "own_posture",
    "facility",
    "items",
    "inv",
    "others_in_room",
    "exhausted",
    "room_comms",
    "room_sustenance",
    "can_remote_comm",
)


def _compact_peer(peer, seen_tags=None):
    if not isinstance(peer, dict):
        return peer
    out = {k: v for k, v in peer.items() if v is not False and v != []}
    pid = out.get("id")
    if seen_tags is not None and pid is not None:
        if pid in seen_tags:
            out.pop("tag", None)
        elif "tag" in out:
            seen_tags.add(pid)
    return out


def _compact_guard(entry):
    if not isinstance(entry, dict):
        return entry
    return {
        k: v
        for k, v in entry.items()
        if not (
            (k == "admitted" and v is False)
            or (k == "lethal_armed_count" and v == 0)
            or (k == "blocked_by" and v == [])
        )
    }


def compact_frame_for_loop(
    frame,
    preview_chars=DOC_PREVIEW_CHARS,
    *,
    static_items=None,
    static_energy_decay=None,
    prev_frame=None,
    seen_docs=None,
    seen_tags=None,
    actor_id=None,
):
    """Slim agent_loop frames deterministically while keeping prefix append-only and lossless."""
    view = frame.get("view")
    if not isinstance(view, dict):
        return frame
    compact_view = dict(view)
    if static_items is not None:
        for key in STATIC_VIEW_KEYS:
            compact_view.pop(key, None)
        if static_energy_decay is not None and compact_view.get("energy_decay") == static_energy_decay:
            compact_view.pop("energy_decay", None)
        item_info = compact_view.get("item_info")
        if isinstance(item_info, dict):
            dynamic_info = {k: v for k, v in item_info.items() if k not in static_items}
            if dynamic_info:
                compact_view["item_info"] = dynamic_info
            else:
                compact_view.pop("item_info", None)
        paths = compact_view.get("paths")
        if isinstance(paths, dict):
            compact_view["paths"] = list(paths.keys())

    prev_view = prev_frame.get("view") if isinstance(prev_frame, dict) else None
    for key in ("near", "others_in_room"):
        if isinstance(prev_view, dict) and compact_view.get(key) == prev_view.get(key):
            compact_view.pop(key, None)
            continue
        entries = compact_view.get(key)
        if isinstance(entries, list) and entries:
            compact_view[key] = [_compact_peer(entry, seen_tags) for entry in entries]

    if isinstance(prev_view, dict) and compact_view.get("contacts") == prev_view.get("contacts"):
        compact_view.pop("contacts", None)
    elif isinstance(compact_view.get("contacts"), list):
        compact_view["contacts"] = [_compact_peer(c, seen_tags) for c in compact_view["contacts"]]

    if isinstance(prev_view, dict) and compact_view.get("guards") == prev_view.get("guards"):
        compact_view.pop("guards", None)
    elif isinstance(compact_view.get("guards"), list) and compact_view["guards"]:
        compact_view["guards"] = [_compact_guard(g) for g in compact_view["guards"]]

    for key in ("docs", "room_docs", "board"):
        entries = compact_view.get(key)
        if not isinstance(entries, list) or not entries:
            continue
        trimmed = []
        for entry in entries:
            if not isinstance(entry, dict):
                trimmed.append(entry)
                continue
            doc_id = entry.get("id")
            already_seen = seen_docs is not None and doc_id in seen_docs
            if already_seen:
                if key == "board":
                    item = {"id": doc_id, "signed": entry.get("signed", 0)}
                    if entry.get("signed_weight") is not None:
                        item["signed_weight"] = entry["signed_weight"]
                    if entry.get("seal"):
                        item["seal"] = entry["seal"]
                    if entry.get("known_signers"):
                        item["known_signers"] = entry["known_signers"]
                else:
                    item = {"id": doc_id, "qty": entry.get("qty", 1)}
                    if entry.get("seal"):
                        item["seal"] = entry["seal"]
                trimmed.append(item)
                continue
            if seen_docs is not None and doc_id is not None:
                seen_docs.add(doc_id)
            item = {k: v for k, v in entry.items() if v not in ("", None, [])}
            text = item.get("text")
            if isinstance(text, str) and len(text) > preview_chars:
                item["text"] = text[:preview_chars] + "…(用read_document查全文)"
            trimmed.append(item)
        compact_view[key] = trimmed

    recent = compact_view.get("recent_room")
    if isinstance(recent, dict):
        pruned = {k: v for k, v in recent.items() if v}
        if pruned:
            compact_view["recent_room"] = pruned
        else:
            compact_view.pop("recent_room", None)

    for key in OMIT_IF_EMPTY_VIEW_KEYS:
        if key in compact_view and not compact_view[key]:
            compact_view.pop(key, None)

    out = {"view": compact_view}
    events = frame.get("events")
    if events:
        prev_tick = view.get("tick", 0) - 1 if isinstance(view.get("tick"), int) else None
        slim_events = []
        for ev in events:
            if not isinstance(ev, dict):
                slim_events.append(ev)
                continue
            # When the previous turn's tool_calls sit immediately above in this context window,
            # omit redundant self-echo events already present in the preceding assistant turn.
            if prev_frame is not None and actor_id is not None:
                kind = ev.get("kind")
                if kind in ("private_note", "whisper_sent") or (
                    kind == "speech" and ev.get("source") == actor_id
                ):
                    continue
            if prev_tick is not None and ev.get("tick") == prev_tick:
                slim_events.append({k: v for k, v in ev.items() if k != "tick"})
            else:
                slim_events.append(ev)
        if slim_events:
            out["events"] = slim_events
    notes = frame.get("notes")
    prev_notes = prev_frame.get("notes") if isinstance(prev_frame, dict) else None
    if prev_frame is not None:
        if notes != prev_notes:
            out["notes"] = notes
    elif notes:
        out["notes"] = notes
    if frame.get("recall"):
        out["recall"] = frame["recall"]
    for extra in ("omitted_events", "omitted_notes"):
        if frame.get(extra):
            out[extra] = frame[extra]
    return out


def message_bytes(messages):
    total = 0
    for m in messages:
        total += len(m["content"].encode()) + 12
        if "tool_calls" in m:
            total += len(encode(m["tool_calls"]).encode())
    return total


def compress(previous_digest, delivered_events, *, limits=(16, 12, 12)):
    """Summarize one resident's evidence while retaining source, kind, and high-salience items."""
    significant, private, public = [], [], []
    for event in sorted(previous_digest + delivered_events, key=lambda e: e["tick"]):
        kind = event["kind"]
        if kind in ("death", "notice", "contract", "spotted_whisper", "combat_witness") or (
            kind == "result" and any(word in event["detail"] for word in ("combat", "blocked"))
        ):
            significant.append(event)
        elif kind in ("whisper", "whisper_sent", "received"):
            private.append(event)
        elif kind == "speech":
            public.append(event)
    n_sig, n_priv, n_pub = limits
    return sorted(significant[-n_sig:] + private[-n_priv:] + public[-n_pub:], key=lambda e: e["tick"])


@dataclass(frozen=True)
class ResidentAgent:
    actor: str
    persona: str

    def prepare(
        self,
        store,
        world,
        window,
        keep,
        byte_budget,
        meeting=None,
        *,
        mode="json",
        fold_target=FOLD_TARGET_RATIO,
    ):
        tick = world.tick
        state = store.mind(self.actor)
        initial_cut = cut = state["cut"]
        digest = state["digest"]
        history = store.history(self.actor, cut, tick)
        parsed_history = [json.loads(turn["frame"]) for turn in history]
        meeting = meeting or world.build_index()
        view = world.observe(self.actor, meeting)
        frame = {
            "view": view,
            "events": store.events(self.actor, tick - 1, tick - 1),
            "notes": state["notes"],
        }

        def fold(count):
            nonlocal cut, digest, history, parsed_history
            # The archived frame records exactly what made it into the resident's input,
            # including any omissions made to fit its context budget.
            delivered = [event for raw in parsed_history[:count] for event in raw["events"]]
            digest = compress(digest, delivered)
            cut = history[count - 1]["tick"]
            history = history[count:]
            parsed_history = parsed_history[count:]

        # window == 0 disables periodic folding so long local contexts stay append-only for KV cache.
        if window > 0 and len(history) >= window:
            fold(len(history) - keep)
        frame["recall"] = store.recall(self.actor, view["near"], cut)
        spec = world.spec
        person = world.people[self.actor]
        group = spec["groups"][person.role]
        rules = RULES
        static_items = None
        static_energy_decay = None
        if mode == "agent_loop":
            rules = (
                "依据自己的局部观察和已送达的信息行动。开局帧已包含当前房间概况、公墙目录与你的私人笔记，"
                "无需重复调用 inspect_room 或 read_notes；仅在需要核实文书全文、回忆某人过往或搜索旧线索时再调用查询工具。"
                "无需查询时请在第 1 步直接并行调用 write_note、stage_speech、stage_whisper、set_roe 与唯一的世界行动提交工具一次完成；"
                "每轮步数有限（工具返回会提示 remaining_steps），查清关键信息后请果断提交唯一的世界行动。"
                "工具报错时可更正参数重试。历史对话中的工具调用记录是往轮已提交的决定。\n" + WORLD_RULES
            )
            static_items = frozenset(spec["items"].keys())
            static_energy_decay = world.energy_decay
            # Keep the entire system message 100% identical across all residents so every
            # actor shares one hot system KV prefix; per-resident role/persona live in messages[1].
            system_parts = (
                rules,
                spec["briefing"],
                format_map(spec),
                format_static_catalog(spec, world.energy_decay),
                SPOKEN_STYLE_GUIDE,
            )
        else:
            system_parts = (
                rules,
                spec["briefing"],
                format_map(spec),
                group["brief"],
                identity_block(person, self.persona),
            )
        system_prompt = "\n".join(system_parts)
        # Hysteresis batch folding: once byte_budget forces a history fold (paying the KV
        # cache reset cost once), fold down to fold_target * byte_budget (keeping at least
        # `keep` recent turns when they fit) so the next several ticks stay strictly append-only.
        folding_history = False
        target_bytes = int(byte_budget * fold_target)
        while True:
            # A full system message stays hot across all residents and across compression.
            # Place per-resident identity and the changing digest in the first user turn.
            if mode == "agent_loop":
                identity = {
                    "id": self.actor,
                    "role_brief": group["brief"],
                    "identity": identity_block(person, self.persona),
                    "older_observations": digest,
                }
            else:
                identity = {"id": self.actor, "older_observations": digest}
            messages = [message("system", system_prompt)]
            if mode == "agent_loop":
                seen_docs = set()
                seen_tags = set()
                prev_frame = None
                for turn, raw_frame in zip(history, parsed_history):
                    user_text = encode(
                        compact_frame_for_loop(
                            raw_frame,
                            static_items=static_items,
                            static_energy_decay=static_energy_decay,
                            prev_frame=prev_frame,
                            seen_docs=seen_docs,
                            seen_tags=seen_tags,
                            actor_id=self.actor,
                        )
                    )
                    messages.append(message("user", user_text))
                    messages.extend(render_tool_history(turn["tick"], turn["reply"]))
                    prev_frame = raw_frame
                current_frame = compact_frame_for_loop(
                    frame,
                    static_items=static_items,
                    static_energy_decay=static_energy_decay,
                    prev_frame=prev_frame,
                    seen_docs=seen_docs,
                    seen_tags=seen_tags,
                    actor_id=self.actor,
                )
                messages.append(message("user", encode(current_frame)))
            else:
                for turn in history:
                    messages.append(message("user", turn["frame"]))
                    messages.append(message("assistant", turn["reply"]))
                messages.append(message("user", encode(frame)))
            messages[1]["content"] = encode(identity) + "\n" + messages[1]["content"]
            size = message_bytes(messages)
            if size <= byte_budget and (
                not folding_history or size <= target_bytes or len(history) <= keep
            ):
                local = {}
                if mode == "agent_loop":
                    local = local_context(world, self.actor, meeting)
                    local["notes"] = state["notes"]
                return Request(self.actor, messages, frame, cut, digest, cut != initial_cut, local)
            if history:
                folding_history = True
                fold(1)
                frame["recall"] = store.recall(self.actor, view["near"], cut)
            elif frame["recall"]:
                frame["recall"] = []
            elif digest:
                # Render a smaller digest without destroying the persistent archive.
                digest = digest[1:]
            elif len(frame["events"]) > 1:
                # Bursts of local speech can exceed a whole context in a crowded place.
                # Prefer personal outcomes, then the latest heard speech.
                events = frame["events"]
                selected = sorted(events, key=lambda e: e["kind"] != "speech" or e["source"] == self.actor)
                removed = len(events) // 2
                frame["events"] = selected[removed:]
                frame["omitted_events"] = frame.get("omitted_events", 0) + removed
            elif len(frame["notes"]) > 1:
                notes = sorted(frame["notes"].items(), key=lambda item: item[1]["tick"])
                removed = len(notes) // 2
                frame["notes"] = dict(notes[removed:])
                frame["omitted_notes"] = frame.get("omitted_notes", 0) + removed
            else:
                raise ValueError(f"{self.actor}: current observation exceeds --context-bytes")
