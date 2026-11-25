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


def format_map(spec):
    places = spec["places"]
    entries = []
    for place_id, layout in places.items():
        neighbors = "、".join(f"{places[n]['label']}({n})" for n in layout["paths"])
        tag = "[有通讯]" if layout.get("comms") else ""
        entries.append(f"{layout['label']}({place_id}){tag}通往{neighbors}")
    return "空间布局：" + "；".join(entries) + "。"


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


def compact_frame_for_loop(frame, preview_chars=DOC_PREVIEW_CHARS):
    """Trim verbose document bodies in agent_loop views; full text stays available via read_document."""
    view = frame.get("view")
    if not isinstance(view, dict):
        return frame
    compact_view = None
    for key in ("docs", "room_docs", "board"):
        entries = view.get(key)
        if not isinstance(entries, list) or not entries:
            continue
        trimmed = None
        for idx, entry in enumerate(entries):
            text = entry.get("text") if isinstance(entry, dict) else None
            if isinstance(text, str) and len(text) > preview_chars:
                if trimmed is None:
                    trimmed = list(entries)
                item = dict(entry)
                item["text"] = text[:preview_chars] + "…(用read_document查全文)"
                trimmed[idx] = item
        if trimmed is not None:
            if compact_view is None:
                compact_view = dict(view)
            compact_view[key] = trimmed
    if compact_view is None:
        return frame
    out = dict(frame)
    out["view"] = compact_view
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

    def prepare(self, store, world, window, keep, byte_budget, meeting=None, *, mode="json"):
        tick = world.tick
        state = store.mind(self.actor)
        initial_cut = cut = state["cut"]
        digest = state["digest"]
        history = store.history(self.actor, cut, tick)
        meeting = meeting or world.build_index()
        view = world.observe(self.actor, meeting)
        frame = {
            "view": view,
            "events": store.events(self.actor, tick - 1, tick - 1),
            "notes": state["notes"],
        }

        def fold(count):
            nonlocal cut, digest, history
            # The archived frame records exactly what made it into the resident's input,
            # including any omissions made to fit its context budget.
            delivered = [event for turn in history[:count] for event in json.loads(turn["frame"])["events"]]
            digest = compress(digest, delivered)
            cut = history[count - 1]["tick"]
            history = history[count:]

        # window == 0 disables periodic folding so long local contexts stay append-only for KV cache.
        if window > 0 and len(history) >= window:
            fold(len(history) - keep)
        frame["recall"] = store.recall(self.actor, view["near"], cut)
        spec = world.spec
        person = world.people[self.actor]
        group = spec["groups"][person.role]
        rules = RULES
        if mode == "agent_loop":
            rules = (
                "依据自己的局部观察和已送达的信息行动。开局帧已包含当前房间概况、公墙目录与你的私人笔记，"
                "仅在需要核实文书全文、回忆某人过往或搜索线索时再调用查询工具。"
                "你可以在同一步并行调用 write_note、stage_speech、stage_whisper、set_roe 和唯一的世界行动提交工具；"
                "每轮步数有限（工具返回会提示 remaining_steps），查清关键信息后请果断提交唯一的世界行动。"
                "工具报错时可更正参数重试。历史对话中的工具调用记录是往轮已提交的决定。\n" + WORLD_RULES
            )
        system_prompt = "\n".join(
            (rules, spec["briefing"], format_map(spec), group["brief"], identity_block(person, self.persona))
        )
        while True:
            # A full system message stays hot across residents with the same archetype
            # and across compression. Place the changing digest in the first user turn.
            identity = {"id": self.actor, "older_observations": digest}
            messages = [message("system", system_prompt)]
            for turn in history:
                if mode == "agent_loop":
                    user_text = encode(compact_frame_for_loop(json.loads(turn["frame"])))
                    messages.append(message("user", user_text))
                    messages.extend(render_tool_history(turn["tick"], turn["reply"]))
                else:
                    messages.append(message("user", turn["frame"]))
                    messages.append(message("assistant", turn["reply"]))
            current_frame = compact_frame_for_loop(frame) if mode == "agent_loop" else frame
            messages.append(message("user", encode(current_frame)))
            messages[1]["content"] = encode(identity) + "\n" + messages[1]["content"]
            if message_bytes(messages) <= byte_budget:
                local = {}
                if mode == "agent_loop":
                    local = local_context(world, self.actor, meeting)
                    local["notes"] = state["notes"]
                return Request(self.actor, messages, frame, cut, digest, cut != initial_cut, local)
            if history:
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
