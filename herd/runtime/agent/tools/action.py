"""Translate native action parameters into the flat world protocol using only local evidence."""

import re

from ....model import PassRule


def copies(documents, doc_id):
    return next((doc["qty"] for doc in documents if doc["id"] == doc_id), 0)


def available_to_collect(view, item):
    if copies(view.get("room_docs", []), item):
        return
    if not view.get("items", {}).get(item):
        raise ValueError("no stock of this item in your room")
    weight = view["item_info"][item]["weight"]
    if weight and view["carry_weight"] + weight > view["carry_cap"]:
        raise ValueError("no room in your carrying capacity for this item")


EXHAUSTED_TOOL_ERROR = (
    "你当前处于力竭失能状态（energy=0），无法执行此行动。"
    "自救方式：① 若背包有食物请直接调用 eat；② 若房间地面有食物请先 take 再 eat；"
    "③ 若负重已满请先 give(target='here') 放下重物腾出空间；"
    "④ 若身边无食物，请用 stage_speech/stage_whisper 呼救求粮，并调用 wait 原地等待救援。"
)


def _require_not_exhausted(box):
    if box.view.get("exhausted"):
        raise ValueError(EXHAUSTED_TOOL_ERROR)


def wait(box):
    return {"action": "wait"}


def eat(box, item=None):
    view = box.view
    foods = {i for i, n in view["inv"].items() if n and view["item_info"][i]["kind"] == "edible"}
    if not foods or (item is not None and item not in foods):
        raise ValueError("no matching edible item in your inventory")
    return {"action": "eat", "item": item or ""}


def produce(box, target=None):
    _require_not_exhausted(box)
    facility = box.view.get("facility")
    if not facility:
        raise ValueError("your room has no production facility")
    if facility["kind"] == "press":
        if not copies(box.view["docs"], target):
            raise ValueError("a press needs the id of a document you hold")
        return {"action": "produce", "target": target}
    if target and target != facility["item"]:
        raise ValueError("this facility produces a different item")
    available_to_collect(box.view, facility["item"])
    return {"action": "produce", "item": target or ""}


def take(box, item, qty=1):
    available_to_collect(box.view, item)
    return {"action": "take", "item": item, "qty": str(qty)}


def give(box, target, item, qty=1):
    if target != "here":
        _require_not_exhausted(box)
    if target != "here" and (target == box.actor or target not in box.present):
        raise ValueError("give requires another resident here or the target here")
    if max(box.view["inv"].get(item, 0), copies(box.view["docs"], item)) < qty:
        raise ValueError("you do not hold this quantity of the item or document")
    return {"action": "give", "target": target, "item": item, "qty": str(qty)}


def move(box, place):
    _require_not_exhausted(box)
    if place not in box.view["paths"]:
        raise ValueError("move requires an adjacent place listed in paths")
    return {"action": "move", "target": place}


def check_target(box, target, guarding):
    if target == "items":
        return
    kind, _, value = target.partition(":")
    if kind == "path" and (value in box.view["paths"] or (guarding and value == "*")):
        return
    if kind == "person" and value in box.present and (guarding or value != box.actor):
        return
    raise ValueError("target must name items, a valid local person, or an adjacent path")


def guard(box, target, allow_pass=""):
    _require_not_exhausted(box)
    check_target(box, target, guarding=True)
    rule = PassRule.parse(allow_pass)
    if "self" in rule.allow_ids:
        raise ValueError(
            "guards always admit themselves automatically; omit 'self' and list only resident IDs"
        )
    visible_item = box.view.get("item_info", {}).get(rule.seal_id)
    if visible_item and visible_item["kind"] != "seal":
        raise ValueError("seal passage requires a seal item")
    return {"action": "guard", "target": target, "detail": allow_pass}


def force(box, target):
    _require_not_exhausted(box)
    check_target(box, target, guarding=False)
    return {"action": "force", "target": target}


def write_document(box, text, seal=None, post=False):
    _require_not_exhausted(box)
    if not text.strip():
        raise ValueError("document text must contain at least one non-whitespace character")
    if seal and (not box.view["inv"].get(seal) or box.view["item_info"][seal]["kind"] != "seal"):
        raise ValueError("writing with a seal requires a seal in your possession")
    return {"action": "write", "detail": text, "item": seal or "", "target": "post" if post else "keep"}


def post_document(box, doc_id):
    _require_not_exhausted(box)
    if not copies(box.view["docs"], doc_id):
        raise ValueError("posting requires a document in your possession")
    return {"action": "post", "target": doc_id}


def endorse(box, doc_id):
    _require_not_exhausted(box)
    if not any(doc["id"] == doc_id for doc in box.view["board"]):
        raise ValueError("endorsement requires a document on the local board")
    return {"action": "endorse", "target": doc_id}


def offer(box, item, qty, want, detail):
    _require_not_exhausted(box)
    # A pledge may be unfunded, and a resident may know a distant item from conversation.
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", item):
        raise ValueError("invalid offered item identifier")
    if want != "none":
        wanted_item, _, price = want.rpartition(":")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", wanted_item) or not (
            price.isascii() and price.isdecimal() and 1 <= int(price) <= 999
        ):
            raise ValueError("want must be none or item:quantity, with quantity from 1 to 999")
    return {"action": "offer", "item": item, "qty": str(qty), "target": want, "detail": detail}


def accept(box, offer_id):
    _require_not_exhausted(box)
    local = next((o for o in box.view["offers"] if o["id"] == offer_id), None)
    if not local:
        raise ValueError("this offer is unavailable in your room")
    if local["want"] != "none":
        item, _, price = local["want"].rpartition(":")
        if box.view["inv"].get(item, 0) < int(price):
            raise ValueError("you cannot pay the requested amount")
    return {"action": "accept", "target": offer_id}
