"""Authored documents and public endorsements."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class Document:
    id: str
    author: str
    tick: int
    text: str
    seal: str = ""
    signers: list[str] = field(default_factory=list)

    def toggle_endorse(self, actor_id: str) -> bool:
        if actor_id in self.signers:
            self.signers.remove(actor_id)
            return False
        self.signers.append(actor_id)
        return True

    def to_dict(self) -> dict:
        return dict(
            author=self.author, tick=self.tick, text=self.text, seal=self.seal, signers=self.signers.copy()
        )
