"""Resident prompt construction, on-demand/periodic compression, and context budgeting."""

from dataclasses import dataclass, field
import json

from ..model import RULES, encode
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
    if person.size <= 1:
        return (
            "身份类型：独立个体（size=1）。以第一人称自主判断个人安危、立场、人脉与秘密。\n"
            + "长期性格："
            + persona
        )
    if person.discipline >= 0.7:
        return (
            f"身份类型：建制集体单元（size={person.size}，训练度={person.discipline:.2f}）。"
            "你代表这支队列严整、组织协同的建制单位，重视指挥链与纪律，受压时不易溃散，"
            "但仍需面对命令合法性、后勤消耗与道义压力的权衡。"
            "公开发言代表集体喊话或表态；私语代表派联络员接洽；笔记用于记录指令、阵地与士气。\n"
            + "群体特质："
            + persona
        )
    return (
        f"身份类型：自发集体单元（size={person.size}，组织度={person.discipline:.2f}）。"
        "你代表这支由多名普通成员组成的群体，而非单个个人。"
        "群体内部缺乏铁板一块的统一指挥，热忱、观望、疲惫、恐惧与温和/激进声音并存；"
        "请结合当前士气（health）、体能补给（energy）与耳闻风声，按群体主流共识或动摇心态行动。"
        "公开发言代表集体呼声或代表表态；私语代表派代表私下接洽；笔记用于记录共同诉求、内部风向与后勤状态。\n"
        + "群体特质："
        + persona
    )


def message(role, content):
    return {"role": role, "content": content}


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
                "你可以在同一步并行调用 write_note、stage_speech、stage_whisper 和唯一的世界行动提交工具；"
                "每轮步数有限（工具返回会提示 remaining_steps），查清关键信息后请果断提交唯一的世界行动。"
                "工具报错时可更正参数重试。历史对话中的 JSON 是往轮已提交的决定。\n" + WORLD_RULES
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
                messages.extend([message("user", turn["frame"]), message("assistant", turn["reply"])])
            messages.append(message("user", encode(frame)))
            messages[1]["content"] = encode(identity) + "\n" + messages[1]["content"]
            if sum(len(m["content"].encode()) + 12 for m in messages) <= byte_budget:
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
