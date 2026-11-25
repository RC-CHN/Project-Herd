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
    box.social.update(whisper_to=whisper_to, whisper=text)
    return {"staged": "whisper", "targets": targets}


def set_roe(box, mode):
    modes = box.view.get("force_modes") or {}
    if mode not in modes:
        valid = ", ".join(sorted(modes))
        raise ValueError(f"unknown roe mode; choose one of: {valid}")
    box.parse_intent({"action": "wait", "roe": mode})
    box.social["roe"] = mode
    return {"staged": "roe", "mode": mode}
