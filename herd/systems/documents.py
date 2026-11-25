"""A document's identity survives printing, physical handoff, posting and endorsement."""

from ..model.entities import Document
from .context import adjust


class DocumentSystem:
    def __init__(self, ctx):
        self.ctx = ctx

    def run(self):
        for actor, intent in self.ctx.actions("write", "post", "endorse"):
            self.ctx.result(actor, self.act(actor, intent))

    def act(self, actor, intent):
        ctx = self.ctx
        room = ctx.world.places[ctx.start[actor].place]
        doc_id = intent.target
        if intent.action == "write":
            if not intent.detail.strip() or intent.target not in ("", "keep", "post"):
                return "failed: invalid document"
            if intent.item and (
                ctx.items.get(intent.item, {}).get("kind") != "seal"
                or ctx.ledger.balance(actor, intent.item) < 1
            ):
                return "failed: missing seal"
            doc_id = f"d{ctx.tick}-{actor}"
            ctx.world.documents[doc_id] = Document(
                id=doc_id,
                author=actor,
                tick=ctx.tick,
                seal=intent.item,
                text=intent.detail,
                signers=[actor],
            )
            person = ctx.world.people[actor]
            person.docs[doc_id] = 1
            if cost := ctx.rules.get("write_energy_cost", 0):
                person.energy = max(1, person.energy - cost)
            self.trace("write", actor, doc_id)
            if intent.target == "post":
                self.post(room, doc_id)
                self.trace("post", actor, doc_id)
            return f"wrote {doc_id}"
        if intent.action == "post":
            if doc_id not in ctx.world.documents or ctx.ledger.balance(actor, doc_id) < 1:
                return "failed: missing document"
            ctx.ledger.spend(actor, doc_id, 1)
            self.post(room, doc_id)
            self.trace("post", actor, doc_id)
            return f"posted {doc_id}"
        if doc_id not in ctx.open_boards[room.id] or doc_id not in room.board:
            return "failed: document unavailable"
        signed = ctx.world.documents[doc_id].toggle_endorse(actor)
        action = "endorse" if signed else "unendorse"
        self.trace(action, actor, doc_id)
        return f"{action} {doc_id}"

    def copy(self, actor, doc_id, copies):
        ctx = self.ctx
        if doc_id not in ctx.world.documents or ctx.ledger.balance(actor, doc_id) < 1:
            return "failed: missing document"
        adjust(ctx.world.people[actor].docs, doc_id, copies)
        self.trace("copy", actor, doc_id, copies)
        return f"copied {copies} {doc_id}"

    def post(self, room, doc_id):
        if doc_id in room.board:
            room.board.remove(doc_id)
        room.board.append(doc_id)
        del room.board[: -self.ctx.rules["board_slots"]]

    def trace(self, kind, actor, doc_id, qty=1):
        ctx = self.ctx
        if ctx.trace is not None:
            ctx.record(
                "doc",
                kind=kind,
                actor=actor,
                doc_id=doc_id,
                place=ctx.start[actor].place,
                seal=ctx.world.documents[doc_id].seal,
                qty=qty,
            )
