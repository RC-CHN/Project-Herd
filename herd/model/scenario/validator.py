"""Validate declarative geography, items, groups and scheduled changes."""

from datetime import date
import math
import re

from ..entities.person import inventory_weight
from .population import validate_population


def keys(value, names, path):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError(f"{path}: expected fields {names}")


def string(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: expected nonempty text")


def integer(value, minimum, path):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{path}: expected integer >= {minimum}")


def identifier(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", value):
        raise ValueError(f"{path}: expected a short lowercase identifier")


def mapping(value, path):
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected an object")
    return value


def sequence(value, path):
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected a list")
    return value


def number(value, low, high, path):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{path}: number out of range")


def reference(value, options, path):
    if not isinstance(value, str) or value not in options:
        raise ValueError(f"{path}: unknown reference {value!r}")


def references(values, options, path):
    for value in sequence(values, path):
        reference(value, options, path)
    if len(set(values)) != len(values):
        raise ValueError(f"{path}: duplicate reference")


def inventory(stock, items):
    for item, qty in mapping(stock, "inventory").items():
        reference(item, items, "inventory")
        integer(qty, 0, item)


def validate_items(items):
    extra = {"edible": "energy_restore", "fungible": "", "arms": "combat_bonus", "seal": "unique", "comm": ""}
    for name, item in items.items():
        kind = mapping(item, name).get("kind")
        reference(kind, extra, name)
        if kind == "seal":
            item.setdefault("unique", False)
            if not isinstance(item["unique"], bool):
                raise ValueError(f"{name}: unique must be boolean")
        item.setdefault("weight", 1)
        integer(item["weight"], 0, f"{name}.weight")
        keys(item, "label kind weight " + extra[kind], name)
        string(item["label"], name)
        if kind == "edible":
            integer(item["energy_restore"], 1, name)
        elif kind == "arms":
            number(item["combat_bonus"], 0, math.inf, name)
            if not item["combat_bonus"]:
                raise ValueError(f"{name}: combat_bonus must be positive")


def validate_places(places, items):
    for name, place in places.items():
        mapping(place, name)
        for field, default in (
            ("facility", None),
            ("initial_items", {}),
            ("regen", {}),
            ("defense", 0),
            ("comms", False),
        ):
            place.setdefault(field, default)
        keys(
            place,
            "label paths facility initial_items regen defense comms" + (" pos" if "pos" in place else ""),
            name,
        )
        if not isinstance(place["comms"], bool):
            raise ValueError(f"{name}.comms: expected boolean")
        string(place["label"], name)
        references(place["paths"], places, "paths")
        if name in place["paths"]:
            raise ValueError(f"{name}: self path")
        if "pos" in place:
            pos = sequence(place["pos"], "pos")
            if len(pos) != 2:
                raise ValueError("pos: expected two coordinates")
            for coord in pos:
                number(coord, 0, 1, "pos")
        number(place["defense"], 0, math.inf, "defense")
        inventory(place["initial_items"], items)
        for item, regen in mapping(place["regen"], "regen").items():
            reference(item, items, "regen")
            keys(regen, "rate cap", "regen")
            for field in regen:
                integer(regen[field], 0, field)
            if items[item].get("unique") and regen["rate"]:
                raise ValueError("cannot regenerate a unique item")
        facility = place["facility"]
        if facility is not None:
            kind = mapping(facility, "facility").get("kind")
            if kind == "harvest":
                keys(facility, "kind item yield_per_action", "facility")
                reference(facility["item"], items, "facility")
                integer(facility["yield_per_action"], 1, "yield_per_action")
            elif kind == "press":
                keys(facility, "kind copies_per_action", "facility")
                integer(facility["copies_per_action"], 1, "copies_per_action")
            else:
                raise ValueError("unknown facility kind")
    for name, place in places.items():
        if any(name not in places[other]["paths"] for other in place["paths"]):
            raise ValueError(f"{name}: paths must be bidirectional")


def validate_groups(spec):
    groups, places, rules = spec["groups"], spec["places"], spec["rules"]
    for name, group in groups.items():
        mapping(group, name)
        if "exact_count" in group:
            integer(group["exact_count"], 0, name)
            group.setdefault("weight", 1)
        group.setdefault("size", 1)
        group.setdefault("discipline", 1.0)
        keys(
            group,
            "label public_tag weight home brief initial_energy initial_health initial_inv archetypes size discipline"
            + (" exact_count" if "exact_count" in group else ""),
            name,
        )
        for field in ("label", "public_tag", "brief"):
            string(group[field], field)
        integer(group["weight"], 1, name)
        integer(group["size"], 1, f"{name}.size")
        number(group["discipline"], 0, 1, f"{name}.discipline")
        group["discipline"] = float(group["discipline"])
        home = group["home"]
        if isinstance(home, str):
            reference(home, places, "home")
        elif mapping(home, "home"):
            for place, weight in home.items():
                reference(place, places, "home")
                integer(weight, 1, "home")
        else:
            raise ValueError("home: empty distribution")
        for field in ("energy", "health"):
            value = group["initial_" + field]
            integer(value, 1, field)
            if value > rules["max_" + field]:
                raise ValueError(f"{field} exceeds maximum")
        inventory(group["initial_inv"], spec["items"])
        group_cap = round(rules["carry_cap"] * math.sqrt(group["size"]))
        if inventory_weight(group["initial_inv"], spec["items"]) > group_cap:
            raise ValueError(f"{name}: initial inventory exceeds carry capacity")
        if not sequence(group["archetypes"], "archetypes"):
            raise ValueError("empty archetypes")
        for persona in group["archetypes"]:
            if isinstance(persona, str):
                string(persona, "archetype")
            else:
                keys(mapping(persona, "archetype"), "text weight", "archetype")
                string(persona["text"], "archetype.text")
                integer(persona["weight"], 1, "archetype.weight")
    validate_population(spec, spec["default_residents"])
    for tie in sequence(spec["ties"], "ties"):
        mode = mapping(tie, "tie").get("mode")
        extra = {"all": "", "partition": "", "ring": " degree", "random_k": " k", "same_home": ""}
        reference(mode, extra, "tie.mode")
        keys(tie, "from_group to_group mode" + extra[mode], "tie")
        for field in ("from_group", "to_group"):
            reference(tie[field], groups, field)
        if mode == "ring" and tie["from_group"] != tie["to_group"]:
            raise ValueError("ring must join the same group")
        if extra[mode]:
            integer(tie[extra[mode].strip()], 1, mode)


def validate_schedule(spec):
    places, items = spec["places"], spec["items"]
    extra = {
        "regen_multiplier": "place item multiplier",
        "energy_decay_set": "value",
        "items_delta": "place item delta",
        "drop_doc": "place",
        "comms_set": "place enabled",
    }
    for event in sequence(spec["shocks"], "shocks"):
        kind = mapping(event, "shock").get("kind")
        reference(kind, extra, "shock.kind")
        keys(event, "tick kind announce_places source text " + extra[kind], "shock")
        if "place" in event:
            reference(event["place"], places, "shock.place")
        if "item" in event:
            reference(event["item"], items, "shock.item")
        if kind == "regen_multiplier":
            reference(event["item"], places[event["place"]]["regen"], "regen_multiplier")
            number(event["multiplier"], 0, math.inf, "multiplier")
        if kind == "energy_decay_set":
            integer(event["value"], 0, "energy_decay_set")
        if kind == "items_delta":
            if type(event["delta"]) is not int or (
                items[event["item"]].get("unique") and event["delta"] > 0
            ):
                raise ValueError("invalid items_delta")
        if kind == "comms_set" and not isinstance(event["enabled"], bool):
            raise ValueError("comms_set: enabled must be boolean")
    for notice in sequence(spec["notices"], "notices"):
        mapping(notice, "notice")
        if "place" in notice:
            keys(notice, "tick place source text", "notice")
            notice["announce_places"] = [notice.pop("place")]
        keys(notice, "tick announce_places source text", "notice")
    for event in spec["notices"] + spec["shocks"]:
        integer(event["tick"], 0, "tick")
        for field in ("source", "text"):
            string(event[field], field)
        if event["announce_places"] != "*":
            references(event["announce_places"], places, "announce_places")


def validate_polity(spec):
    for field, default in (("ties", []), ("shocks", []), ("analysis_markers", {})):
        spec.setdefault(field, default)
    keys(
        spec,
        "engine id title start_date default_residents briefing rules places groups items ties shocks "
        "notices sources assumptions analysis_markers",
        "scenario",
    )
    identifier(spec["id"], "id")
    for field in ("title", "start_date", "briefing"):
        string(spec[field], field)
    date.fromisoformat(spec["start_date"])
    integer(spec["default_residents"], 2, "default_residents")
    if not isinstance(spec["assumptions"], str):
        raise ValueError("assumptions: expected text")
    for source in sequence(spec["sources"], "sources"):
        string(source, "source")
    rules = mapping(spec["rules"], "rules")
    rules.setdefault("carry_cap", 24)
    rules.setdefault("clock_interval", 1)
    rules.setdefault("combat_damage_scale", 1.0)
    rules.setdefault("exertion_energy_cost", 0)
    rules.setdefault("night_whisper_multiplier", 1.0)
    positive = "contact_size board_slots max_energy max_health whisper_detect_ref_crowd offer_ttl clock_interval carry_cap"
    probabilities = "whisper_detect_base whisper_detect_min whisper_detect_max whisper_same_huddle_bonus night_whisper_multiplier"
    keys(
        rules,
        positive
        + " base_energy_decay low_energy_threshold exertion_energy_cost combat_damage_scale "
        + probabilities,
        "rules",
    )
    for field in positive.split():
        integer(rules[field], 1, field)
    for field in ("base_energy_decay", "low_energy_threshold", "exertion_energy_cost"):
        integer(rules[field], 0, field)
    number(rules["combat_damage_scale"], 0, math.inf, "combat_damage_scale")
    for field in probabilities.split():
        number(rules[field], 0, 1, field)
    if rules["whisper_detect_min"] > rules["whisper_detect_max"]:
        raise ValueError("reversed detection bounds")
    for field in ("items", "places", "groups"):
        entries = mapping(spec[field], field)
        if not entries and field != "items":
            raise ValueError(f"{field} is empty")
        for name in entries:
            identifier(name, field)
    validate_items(spec["items"])
    validate_places(spec["places"], spec["items"])
    validate_groups(spec)
    validate_schedule(spec)
    seals = {k for k, v in spec["items"].items() if v["kind"] == "seal"}
    options = {"focal_groups": spec["groups"], "focal_places": spec["places"], "focal_seals": seals}
    for field, values in mapping(spec["analysis_markers"], "analysis_markers").items():
        reference(field, options, "analysis_markers")
        references(values, options[field], field)
    return spec
