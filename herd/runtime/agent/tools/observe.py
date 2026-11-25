"""Inspect only documents and room information projected into this resident's opening view."""


def inspect_room(box):
    return box.view


def read_document(box, doc_id):
    for collection in ("docs", "room_docs", "board"):
        for doc in box.view.get(collection, []):
            if doc["id"] == doc_id:
                details = box.local.get("documents", {}).get(doc_id, doc)
                return {
                    k: details[k]
                    for k in ("id", "author", "text", "seal", "signers", "known_signers")
                    if k in details
                }
    raise ValueError("document unavailable in your room or possessions")
