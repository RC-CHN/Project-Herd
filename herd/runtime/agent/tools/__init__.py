"""Functions callable by one resident during an opening turn."""

from . import action, memory, observe, social


INTERNAL = {
    f.__name__: f
    for f in (
        observe.inspect_room,
        observe.read_document,
        memory.recall_person,
        memory.search_memory,
        memory.read_notes,
        memory.write_note,
        memory.delete_note,
        social.stage_speech,
        social.stage_whisper,
    )
}
ACTIONS = {
    f.__name__: f
    for f in (
        action.wait,
        action.eat,
        action.produce,
        action.take,
        action.give,
        action.move,
        action.guard,
        action.force,
        action.write_document,
        action.post_document,
        action.endorse,
        action.offer,
        action.accept,
    )
}
