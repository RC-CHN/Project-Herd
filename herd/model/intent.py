"""Flat resident decision protocol, shared by inference, settlement and replay."""

from dataclasses import dataclass
import json
import re


ACTIONS = (
    "wait",
    "eat",
    "produce",
    "move",
    "guard",
    "force",
    "take",
    "give",
    "write",
    "post",
    "endorse",
    "offer",
    "accept",
)
WORLD_RULES = """本轮同桌的人（near）可以听到你的公开发言；屋里其他在场者见 others_in_room。contacts、near 与 others_in_room 中的 rels 列出你与对方的已知关系标签（可有多重有向关系，如上级/部下/同袍/同僚/恩师/旧友/政敌/新相识；陌生人为 []）。你也可以向同地一至四人私语，旁观者可能察觉交谈。当 can_remote_comm 为真（独立个体或高组织度建制单元 discipline>=0.7，且身处标有[有通讯]的地点或随身携带通讯器）时，你还可以用私语直接跨房联系 contacts 通讯录中的熟人（对方也需具备跨房通讯条件，否则会提示无法接通；自发人群 size>1 且 discipline<0.7 不能直接跨房通联，需靠现场交谈、代表中转或公墙传单；跨房联络只能传口信，不能递交实物或验印，同屋旁观者仍可能察觉你联络了谁）。私人笔记可写入、改写或删除。
每个行动体带有规模 size 与训练/组织度 discipline（0~1）：size=1 是独立个体；size>1 是集体行动单元（人群、小队或建制单位），其 health 代表凝聚力与建制完整度，归零即溃散退出，carry_cap 与文书联署权重（signed_weight）随规模放大，但单份食物的补给效果按规模折算。高训练度单位能把人数高效转化为战力与封锁力，且战败时不易溃散；低组织度人群拥挤折损大，遇袭更易溃散。near 与 others_in_room 会标出他人的 size 及是否队列严整（disciplined）。
每隔 clock_interval 轮结算自然耗能、物资再生与休养自愈（体力充沛且未满血者恢复一点健康），day 与 time_of_day 表示当前天数与昼夜天色，ticks_to_decay 表示还剩几轮。吃一份随身食物可以恢复体力。体力或健康归零即死亡或溃散退出，全部随身物品和文书留在原地。
每个地点的 items 与 docs 就是该处的全部无主实物，不存在隐藏密室或地下暗库。物资与文书均按整数份存取和交付，不可拆分半份。可以在房间内拾取或放下物资和文书，也可以把它们交给同地的居民。采集设施提供当地物资，复制设施可以复制手中的文书。carry_weight 是当前负重，carry_cap 是上限，各物资的重量见 item_info。本轮取得的物资和文书下轮才能支出。同屋的拾取（taken）、放置（placed）、当面交接（given）与被守卫拦下的取物（blocked_takes）均公开记录在 recent_room。房间内的物品常有人看管或记名，未经许可私取可能招致拦截、追责或反击。
居民可以提出或接受本地邀约，自己尚未过期的邀约见 own_offers。邀约可约定用一定数量的物资换取另一种物资，也可不索取回报。现货交易需要双方备足货物。承诺交易可能履约或违约，是否打算履约只有发起者知道。
移动只能前往相邻地点。本轮值守支持四种普世目标：path:邻地（守在本屋门口，只拦从该邻地进入本屋的人，不拦屋内人离开）、path:*（双向封锁本屋全部出入口，并同时看护屋内地面物品）、items（仅看护本屋地面物品与文书，拦住未放行者拾取）、person:同地编号（护卫同地居民）。值守每轮都要重新选择。守卫总是放行自己，也可以放行指定居民、熟人（contacts）或持指定印信者。持有该印信或盖有该印的文书都可验印。符合任一放行条件即可获该守卫放行。阻挡者的战力合力超过放行者时才会拦截。
force 是致命的武装/肉搏强攻，绝非普通试探移动！目标为 path:邻地、items 或 person:同地编号。徒手冲击持械守卫极易重伤送命；强攻通道时已获放行者和平通过；强攻人物（person）胜出且目标存活时，会迫使败者掉落一件随身武器或重物到地面；交火中战败负伤会打断同轮拾取与交付；发生冲突时在场所有人（通道冲突含通道两端）都会亲眼目睹是谁先动武。伤势严重削弱战力，请优先交谈、谈判、交换或值守。
写好的文书可用随身印信盖印，可保留或张贴在本地公墙。张贴已有文书会消耗手中的一份副本。公墙槽位有限，日常沟通请用发言或私语，重要公告、协议或证据再写成文书张贴。居民可以在墙上文书上公开署名，再次署名则撤回。"""

_JSON_PROTOCOL = """每轮提交一个扁平 JSON。action 必填，所有字段值均为字符串。根据自己的局部观察和收到的消息行动。
speech 填公开发言。whisper 与 whisper_to 成对填写，whisper_to 填同地接收者编号，用逗号分隔。note_key 指定私人笔记，note 填内容，留空则删除。
wait 等待。eat 吃一份随身食物，可用 item 指定。take 拾取物资或文书，item 填编号，qty 缺省为 "1"。give 用 item 和 qty 指定交出的物资或文书，target 填同地接收者编号，或填 here 放在本房间。produce 使用采集设施，在复制设施则用 target 指定手头文书编号。
offer 用 item 和 qty 指定承诺付出物，target 填索取物:数量或 none，detail 填 swap（现货）、fair（履约）或 cheat（违约）。accept 用 target 指定本地邀约编号。
move 用 target 指定邻地。guard 的 target 填 path:邻地（拦外人进入）、path:*（双向封锁全屋并看护地面物品）、items（看护地面物品）或 person:同地编号，detail 填放行条件。detail 留空或填 none 时仅放行自己；allow:编号列表（逗号分隔）、seal:印信编号、known（熟人）可用分号组合。force 的 target 填 path:邻地、items 或 person:同地编号。
write 用 detail 填文书正文，item 可填随身印信编号，target 填 keep 或 post。post 和 endorse 均用 target 指定文书编号。"""

RULES = _JSON_PROTOCOL + "\n" + WORLD_RULES


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


FIELD_LIMITS = {
    "action": 12,
    "target": 32,
    "speech": 240,
    "note_key": 32,
    "note": 160,
    "item": 32,
    "qty": 8,
    "detail": 240,
    "whisper_to": 48,
    "whisper": 240,
}
POLITY_FIELDS = ("item", "qty", "detail", "whisper_to", "whisper")
QTY_PATTERN = r"^(?:0*[1-9][0-9]{0,2})?$"


@dataclass(frozen=True)
class Intent:
    action: str
    target: str = ""
    speech: str = ""
    note_key: str = ""
    note: str = ""
    item: str = ""
    qty: str = ""
    detail: str = ""
    whisper_to: str = ""
    whisper: str = ""

    @classmethod
    def parse(cls, raw, actor=None, view=None):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected an intent object")
        # Some chat servers echo the resident ID. The engine always chooses
        # the acting resident; this annotation grants no authority.
        if "id" in data:
            echoed = data.pop("id")
            if not isinstance(echoed, str) or (actor is not None and echoed != actor):
                raise ValueError("resident ID echo does not match the active resident")
        if unknown := set(data) - set(cls.__dataclass_fields__):
            raise ValueError(f"unexpected intent fields: {sorted(unknown)}")
        if "action" not in data:
            raise ValueError("missing action")
        intent = cls(**data)
        for field, limit in FIELD_LIMITS.items():
            value = getattr(intent, field)
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"invalid intent field: {field}")
        if intent.action not in ACTIONS:
            raise ValueError("unknown action")
        if intent.note and not intent.note_key:
            raise ValueError("note requires note_key")
        if not re.fullmatch(QTY_PATTERN, intent.qty):
            raise ValueError("qty must be a positive integer up to 999")
        if bool(intent.whisper) != bool(intent.whisper_to):
            raise ValueError("whisper and whisper_to must be supplied together")
        return intent
