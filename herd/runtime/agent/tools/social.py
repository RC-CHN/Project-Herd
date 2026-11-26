"""Companion speech channels staged privately until the barrier commits."""


def stage_speech(box, text):
    box.social["speech"] = text
    return {"staged": "speech"}


def stage_whisper(box, targets, text):
    present = set(box.present)
    can_remote = bool(box.local.get("can_remote_comm"))
    contacts = set(box.local.get("contacts", ()))
    for actor in targets:
        if actor == box.actor:
            raise ValueError("whisper targets must be other residents")
        if actor not in present and not (can_remote and actor in contacts):
            if can_remote:
                raise ValueError("whisper targets must be residents in your room or in your contacts")
            raise ValueError(
                "whisper targets must be other residents present in your room (no remote comms available here)"
            )
    whisper_to = ",".join(targets)
    box.parse_intent({"action": "wait", "whisper_to": whisper_to, "whisper": text})
    replaced = bool(box.social.get("whisper"))
    box.social.update(whisper_to=whisper_to, whisper=text)
    result = {"staged": "whisper", "targets": targets}
    if replaced:
        result["replaced_previous"] = True
        result["warning"] = "每轮仅保留一条私语，已覆盖此前暂存内容；如需同轮通知多人请将最多4个编号合并写入同一条 targets。"
    return result


def set_roe(box, mode):
    modes = box.view.get("force_modes") or {}
    if mode not in modes:
        valid = ", ".join(sorted(modes))
        raise ValueError(f"unknown roe mode; choose one of: {valid}")
    box.parse_intent({"action": "wait", "roe": mode})
    box.social["roe"] = mode
    return {"staged": "roe", "mode": mode}
