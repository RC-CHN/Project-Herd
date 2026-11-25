"""Physical guard targets and passage credentials."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class PassRule:
    raw: str
    allow_ids: frozenset[str]
    seal_id: str
    allow_known: bool

    @classmethod
    def parse(
        cls, raw: str, *, valid_actors: Collection[str] | None = None, items: dict | None = None
    ) -> PassRule:
        """When a namespace is unavailable, only validate its identifier syntax."""
        if raw in ("", "none"):
            return cls(raw, frozenset(), "", False)
        if not isinstance(raw, str):
            raise ValueError("pass rule must be a string")
        seen = set()
        ids = frozenset()
        seal = ""
        for clause in raw.split(";"):
            kind, separator, value = clause.partition(":")
            if kind in seen:
                raise ValueError(f"repeated pass clause: {kind}")
            seen.add(kind)
            if clause == "known":
                continue
            if kind == "allow" and separator:
                ids = frozenset(value.split(","))
                if all(
                    actor in valid_actors
                    if valid_actors is not None
                    else re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", actor)
                    for actor in ids
                ):
                    continue
            if (
                kind == "seal"
                and separator
                and (
                    items.get(value, {}).get("kind") == "seal"
                    if items is not None
                    else re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", value)
                )
            ):
                seal = value
                continue
            raise ValueError(f"invalid pass clause: {clause}")
        return cls(raw, ids, seal, "known" in seen)

    def permits(self, guard_id, guard_contacts, actor_id, credentials, documents) -> bool:
        if (
            guard_id == actor_id
            or actor_id in self.allow_ids
            or self.allow_known
            and actor_id in guard_contacts
        ):
            return True
        if not self.seal_id:
            return False
        if isinstance(credentials, Mapping):
            inv, docs = credentials["inv"], credentials["docs"]
        else:
            inv, docs = credentials.inv, credentials.docs
        return inv.get(self.seal_id, 0) > 0 or any(
            count > 0 and documents[doc_id].seal == self.seal_id for doc_id, count in docs.items()
        )


@dataclass(frozen=True, slots=True)
class GuardPosture:
    scope_kind: str
    scope_target: str
    pass_rule: PassRule

    def __post_init__(self) -> None:
        if self.scope_kind == "items":
            if self.scope_target != "":
                raise ValueError("items guard posture takes no target suffix")
        elif self.scope_kind not in ("path", "person") or not self.scope_target:
            raise ValueError("a guard posture requires a path, person or items target")

    @property
    def scope(self) -> str:
        return "items" if self.scope_kind == "items" else f"{self.scope_kind}:{self.scope_target}"

    @classmethod
    def try_parse(cls, scope, raw, *, valid_actors, items, paths, present) -> GuardPosture | None:
        if not isinstance(scope, str):
            return None
        if scope == "items":
            try:
                rule = PassRule.parse(raw, valid_actors=valid_actors, items=items)
            except ValueError:
                return None
            return cls("items", "", rule)
        kind, separator, target = scope.partition(":")
        is_path = kind == "path" and (target == "*" or target in paths)
        is_person = kind == "person" and target in present
        if separator and (is_path or is_person):
            try:
                rule = PassRule.parse(raw, valid_actors=valid_actors, items=items)
            except ValueError:
                return None
            return cls(kind, target, rule)
        return None

    def to_dict(self) -> dict:
        return {"scope": self.scope, "allow": self.pass_rule.raw}

    @classmethod
    def from_dict(cls, data, *, valid_actors, paths, present, items) -> GuardPosture:
        posture = cls.try_parse(
            data["scope"],
            data["allow"],
            valid_actors=valid_actors,
            paths=paths,
            present=present,
            items=items,
        )
        if posture is None:
            raise ValueError(f"invalid archived guard posture: {data}")
        return posture
