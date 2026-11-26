"""Validate declarative geography, items, groups and scheduled changes."""

from copy import deepcopy
from datetime import date
import math
import re

from ..entities.person import inventory_weight
from .population import validate_population

DEFAULT_FORCE_MODES = {
    "lethal": {
        "label": "致命强攻",
        "severity": 1,
        "lethal_arms_scale": 1.0,
        "damage_scale": 1.0,
        "lethal": True,
        "lanchester_exp": 1.0,
        "rout_shock": 1.0,
        "casualty_rate": 0.4,
    }
}


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
    extra = {
        "edible": "energy_restore",
        "fungible": "",
        "arms": "combat_bonus nonlethal",
        "seal": "unique",
        "comm": "",
    }
    for name, item in items.items():
        kind = mapping(item, name).get("kind")
        reference(kind, extra, name)
        if kind == "seal":
            item.setdefault("unique", False)
            if not isinstance(item["unique"], bool):
                raise ValueError(f"{name}: unique must be boolean")
        elif kind == "arms":
            item.setdefault("nonlethal", False)
            if not isinstance(item["nonlethal"], bool):
                raise ValueError(f"{name}: nonlethal must be boolean")
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
            ("sustenance", False),
        ):
            place.setdefault(field, default)
        keys(
            place,
            "label paths facility initial_items regen defense comms sustenance" + (" pos" if "pos" in place else ""),
            name,
        )
        if not isinstance(place["comms"], bool):
            raise ValueError(f"{name}.comms: expected boolean")
        if not isinstance(place["sustenance"], bool):
            raise ValueError(f"{name}.sustenance: expected boolean")
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


def validate_force_modes(modes):
    mapping(modes, "rules.force_modes")
    if not modes:
        raise ValueError("rules.force_modes: expected at least one mode")
    for name, mode in modes.items():
        identifier(name, "rules.force_modes")
        mapping(mode, f"force_modes.{name}")
        mode.setdefault("casualty_rate", 0.4 if mode.get("lethal", True) else 0.0)
        mode.setdefault("sound", "")
        mode.setdefault("requires_lethal_arms", False)
        keys(
            mode,
            "label severity lethal_arms_scale damage_scale lethal lanchester_exp rout_shock casualty_rate sound requires_lethal_arms",
            f"force_modes.{name}",
        )
        string(mode["label"], f"force_modes.{name}.label")
        if not isinstance(mode["sound"], str):
            raise ValueError(f"force_modes.{name}.sound: expected string")
        if not isinstance(mode["requires_lethal_arms"], bool):
            raise ValueError(f"force_modes.{name}.requires_lethal_arms: expected boolean")
        integer(mode["severity"], 1, f"force_modes.{name}.severity")
        number(mode["lethal_arms_scale"], 0, 1, f"force_modes.{name}.lethal_arms_scale")
        number(mode["damage_scale"], 0, math.inf, f"force_modes.{name}.damage_scale")
        if not isinstance(mode["lethal"], bool):
            raise ValueError(f"force_modes.{name}.lethal: expected boolean")
        number(mode["lanchester_exp"], 0.5, 4.0, f"force_modes.{name}.lanchester_exp")
        number(mode["rout_shock"], 0, math.inf, f"force_modes.{name}.rout_shock")
        number(mode["casualty_rate"], 0, 1, f"force_modes.{name}.casualty_rate")
        for field in ("lethal_arms_scale", "damage_scale", "lanchester_exp", "rout_shock", "casualty_rate"):
            mode[field] = float(mode[field])


def validate_groups(spec):
    groups, places, rules = spec["groups"], spec["places"], spec["rules"]
    modes = rules["force_modes"]
    default_mode = min(modes, key=lambda k: (modes[k]["severity"], k))
    for name, group in groups.items():
        mapping(group, name)
        if "exact_count" in group:
            integer(group["exact_count"], 0, name)
            group.setdefault("weight", 1)
        group.setdefault("size", 1)
        group.setdefault("discipline", 1.0)
        group.setdefault("default_roe", default_mode)
        keys(
            group,
            "label public_tag weight home brief initial_energy initial_health initial_inv archetypes size discipline default_roe"
            + (" exact_count" if "exact_count" in group else ""),
            name,
        )
        for field in ("label", "public_tag", "brief"):
            string(group[field], field)
        integer(group["weight"], 1, name)
        integer(group["size"], 1, f"{name}.size")
        number(group["discipline"], 0, 1, f"{name}.discipline")
        group["discipline"] = float(group["discipline"])
        reference(group["default_roe"], modes, f"{name}.default_roe")
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
    def normalize_rels(value, path):
        if isinstance(value, str):
            string(value, path)
            return [value.strip()]
        tags = sequence(value, path)
        if not tags:
            raise ValueError(f"{path}: expected nonempty relation tags")
        result = []
        for tag in tags:
            string(tag, path)
            cleaned = tag.strip()
            if cleaned not in result:
                result.append(cleaned)
        return result

    for tie in sequence(spec["ties"], "ties"):
        mode = mapping(tie, "tie").get("mode")
        extra = {"all": "", "partition": "", "ring": " degree", "random_k": " k", "same_home": ""}
        reference(mode, extra, "tie.mode")
        optional = (" rel" if "rel" in tie else "") + (" reverse_rel" if "reverse_rel" in tie else "")
        keys(tie, "from_group to_group mode" + extra[mode] + optional, "tie")
        for field in ("from_group", "to_group"):
            reference(tie[field], groups, field)
        if mode == "ring" and tie["from_group"] != tie["to_group"]:
            raise ValueError("ring must join the same group")
        if extra[mode]:
            integer(tie[extra[mode].strip()], 1, mode)
        if "rel" in tie:
            tie["rel"] = normalize_rels(tie["rel"], "tie.rel")
        if "reverse_rel" in tie:
            tie["reverse_rel"] = normalize_rels(tie["reverse_rel"], "tie.reverse_rel")


def validate_schedule(spec):
    places, items = spec["places"], spec["items"]
    extra = {
        "regen_multiplier": "place item multiplier",
        "energy_decay_set": "value",
        "items_delta": "place item delta",
        "drop_doc": "place",
        "comms_set": "place enabled",
        "sustenance_set": "place enabled",
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
        if kind in ("comms_set", "sustenance_set") and not isinstance(event["enabled"], bool):
            raise ValueError(f"{kind}: enabled must be boolean")
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
    for field, default in (("ties", []), ("shocks", []), ("analysis_markers", {}), ("theme", {})):
        spec.setdefault(field, default)
    keys(
        spec,
        "engine id title start_date default_residents briefing rules places groups items ties shocks "
        "notices sources assumptions analysis_markers theme",
        "scenario",
    )
    theme = spec["theme"]
    if isinstance(theme, str):
        string(theme, "theme")
    elif isinstance(theme, dict):
        if "preset" in theme:
            string(theme["preset"], "theme.preset")
        for sub in ("palette", "group_colors"):
            if sub in theme:
                for k, v in mapping(theme[sub], f"theme.{sub}").items():
                    string(k, f"theme.{sub}")
                    string(v, f"theme.{sub}.{k}")
    else:
        raise ValueError("theme: expected string preset or object")
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
    rules.setdefault("write_energy_cost", 0)
    rules.setdefault("body_weight", 6)
    rules.setdefault("zero_energy_mode", "fatal")
    rules.setdefault("starvation_health_loss", 0)
    rules.setdefault("collective_starvation_health_loss", 2)
    rules.setdefault("night_whisper_multiplier", 1.0)
    rules.setdefault("force_modes", deepcopy(DEFAULT_FORCE_MODES))
    positive = "contact_size board_slots max_energy max_health whisper_detect_ref_crowd offer_ttl clock_interval carry_cap"
    probabilities = "whisper_detect_base whisper_detect_min whisper_detect_max whisper_same_huddle_bonus night_whisper_multiplier"
    keys(
        rules,
        positive
        + " base_energy_decay low_energy_threshold exertion_energy_cost write_energy_cost body_weight"
        + " zero_energy_mode starvation_health_loss collective_starvation_health_loss"
        + " combat_damage_scale force_modes "
        + probabilities,
        "rules",
    )
    for field in positive.split():
        integer(rules[field], 1, field)
    for field in (
        "base_energy_decay",
        "low_energy_threshold",
        "exertion_energy_cost",
        "write_energy_cost",
        "body_weight",
        "starvation_health_loss",
        "collective_starvation_health_loss",
    ):
        integer(rules[field], 0, field)
    reference(rules["zero_energy_mode"], ("fatal", "exhaust"), "rules.zero_energy_mode")
    number(rules["combat_damage_scale"], 0, math.inf, "combat_damage_scale")
    validate_force_modes(rules["force_modes"])
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
