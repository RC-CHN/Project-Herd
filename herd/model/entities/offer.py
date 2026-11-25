"""A local exchange or pledge with private terms."""

from dataclasses import dataclass


@dataclass(slots=True)
class Offer:
    id: str
    issuer: str
    place: str
    tick: int
    expires: int
    mode: str
    give_item: str
    give_qty: int
    want_item: str
    want_qty: int
    terms: str

    def is_open(self, tick: int) -> bool:
        return self.tick < tick <= self.expires

    def public_view(self) -> dict:
        return dict(
            id=self.id,
            issuer=self.issuer,
            mode=self.mode,
            give=f"{self.give_item}:{self.give_qty}",
            want=f"{self.want_item}:{self.want_qty}" if self.want_qty else "none",
        )

    def issuer_view(self) -> dict:
        return dict(**self.public_view(), expires=self.expires, terms=self.terms)

    def to_dict(self) -> dict:
        return dict(
            issuer=self.issuer,
            place=self.place,
            tick=self.tick,
            expires=self.expires,
            mode=self.mode,
            give_item=self.give_item,
            give_qty=self.give_qty,
            want_item=self.want_item,
            want_qty=self.want_qty,
            terms=self.terms,
        )
