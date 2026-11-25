"""Validate inference profiles and resolve deterministic assignments."""

import json
from pathlib import Path
import random

from .base import Backend, _positive_int, _text
from .chat_api import ChatAPI
from .scripted import Scripted


class RoutedBackend:
    """Assign profiles using immutable population membership, seed and ordered overrides."""

    def __init__(self, default, profiles, routes=()):
        if not isinstance(profiles, dict) or not profiles:
            raise ValueError("profiles must be a nonempty object")
        _text(default, "default")
        if default not in profiles:
            raise ValueError("unknown default profile")
        self.default = default
        self.profiles = dict(profiles)
        for name, backend in self.profiles.items():
            _text(name, "profile name")
            if not isinstance(backend, Backend) or backend.name != name:
                raise ValueError(f"{name}: backend must have the configured profile name")
            if backend.mode not in ("agent_loop", "json", "scripted"):
                raise ValueError(f"{name}: unknown decision mode")
            _positive_int(backend.max_steps, "max_steps")
        if len({b.scope for b in self.profiles.values()}) != len(self.profiles):
            raise ValueError("each profile must have an independent backend scope")
        if not isinstance(routes, (list, tuple)):
            raise ValueError("routes must be a list")
        self.routes = []
        for route in routes:
            self._validate_route(route)
            self.routes.append(dict(route))

    def _validate_route(self, route):
        if not isinstance(route, dict) or set(route) - {
            "profile",
            "actors",
            "group",
            "random",
            "ratio",
            "seed",
        }:
            raise ValueError("invalid route fields")
        _text(route.get("profile"), "route.profile")
        if route["profile"] not in self.profiles:
            raise ValueError("unknown route profile")
        selectors = route.keys() - {"profile", "seed"}
        if "actors" in route:
            actors = route["actors"]
            if (
                selectors != {"actors"}
                or "seed" in route
                or not isinstance(actors, list)
                or not actors
                or not all(isinstance(a, str) and a.strip() for a in actors)
                or len(set(actors)) != len(actors)
            ):
                raise ValueError("actors must be a nonempty list of distinct resident IDs used alone")
            return
        if "group" in route:
            _text(route["group"], "route.group")
        if not selectors or {"random", "ratio"} <= selectors:
            raise ValueError("provide a group or one sampling method per route")
        if "random" in route:
            _positive_int(route["random"], "random")
        if "ratio" in route and (type(route["ratio"]) not in (float, int) or not 0 < route["ratio"] <= 1):
            raise ValueError("ratio must be in (0, 1]")
        if "seed" in route and (type(route["seed"]) is not int or not selectors & {"random", "ratio"}):
            raise ValueError("seed requires a sampling route and an integer")

    def resolve(self, world):
        residents = sorted(world.people)
        assignment = dict.fromkeys(residents, self.profiles[self.default])
        for index, route in enumerate(self.routes):
            if "actors" in route:
                selected = route["actors"]
                unknown = set(selected) - assignment.keys()
                if unknown:
                    raise ValueError(f"unknown route actors: {sorted(unknown)}")
            else:
                selected = residents
                if "group" in route:
                    group = route["group"]
                    if group not in world.spec["groups"]:
                        raise ValueError(f"unknown route group: {group}")
                    selected = [a for a in residents if world.people[a].role == group]
                if "random" in route or "ratio" in route:
                    count = route.get("random")
                    if count is None:
                        count = min(len(selected), max(1, round(len(selected) * route["ratio"])))
                    if count > len(selected):
                        raise ValueError(f"route {index} requests more residents than its candidate pool")
                    seed = route.get("seed", world.seed)
                    selected = random.Random(f"route/{seed}/{index}").sample(selected, count)
            for actor in selected:
                assignment[actor] = self.profiles[route["profile"]]
        return assignment


def load_backends(config):
    """Read a routing plan. Credentials remain in the selected environment variables."""
    if isinstance(config, (str, Path)):
        config = json.loads(Path(config).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not {"default", "profiles"} <= config.keys() <= {
        "default",
        "profiles",
        "routes",
    }:
        raise ValueError("expected default, profiles and optional routes")
    if not isinstance(config["profiles"], dict):
        raise ValueError("profiles must be an object")
    profiles = {}
    for name, spec in config["profiles"].items():
        _text(name, "profile name")
        if not isinstance(spec, dict):
            raise ValueError(f"{name}: expected a profile configuration object")
        options = dict(spec)
        kind = options.pop("type", None)
        if "workers" in options:
            _positive_int(options["workers"], "workers")
        if kind == "scripted" and options.get("mode") == "scripted":
            options.pop("mode")
        if kind == "scripted" and options.keys() <= {"workers", "max_steps"}:
            profiles[name] = Scripted(name=name, **options)
        elif kind == "api" and {"base_url", "model"} <= options.keys() <= {
            "base_url",
            "model",
            "max_tokens",
            "tokenizer_path",
            "workers",
            "cache_key",
            "api_key_env",
            "mode",
            "max_steps",
        }:
            profiles[name] = ChatAPI(name=name, **options)
        else:
            raise ValueError(f"{name}: invalid profile type or options")
    return RoutedBackend(config["default"], profiles, config.get("routes", []))
