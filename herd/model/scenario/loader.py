"""Resolve a scenario name or file to a frozen, validated specification."""

from copy import deepcopy
import json
from pathlib import Path

from .validator import validate_polity


ENGINE_POLITY = "polity-v1"
PRESETS = Path(__file__).resolve().parents[2] / "scenarios"


def validate(spec):
    if not isinstance(spec, dict):
        raise ValueError("scenario: expected an object")
    spec = {"sources": [], "notices": [], "assumptions": "", **deepcopy(spec)}
    if spec.get("engine") != ENGINE_POLITY:
        raise ValueError(f"unsupported scenario engine: {spec.get('engine')}")
    return validate_polity(spec)


def load(scenario=None):
    if scenario is None:
        presets = sorted(PRESETS.glob("*.json"))
        if len(presets) != 1:
            raise ValueError("select a scenario by name or path")
        scenario = presets[0]
    if isinstance(scenario, dict):
        return validate(scenario)
    path = Path(scenario)
    if not path.is_file() and path.name == str(scenario):
        path = PRESETS / (path.name + ".json")
    if not path.is_file():
        raise ValueError(f"scenario not found: {scenario}")
    return validate(json.loads(path.read_text(encoding="utf-8")))
