from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from herd.model import Intent, World, encode
from herd.runtime.backends import Scripted
from herd.runtime.storage import Store


def fixture():
    group = dict(
        label="Circle",
        public_tag="Visitor",
        weight=1,
        home="atrium",
        brief="Meet your neighbors.",
        initial_energy=12,
        initial_health=10,
        initial_inv={"ration": 2},
        archetypes=["curious", "quiet"],
    )
    return dict(
        engine="polity-v1",
        id="exchange",
        title="Exchange",
        start_date="2000-01-01",
        default_residents=8,
        briefing="Meetings are held locally.",
        rules=dict(
            contact_size=4,
            board_slots=2,
            max_energy=16,
            max_health=10,
            base_energy_decay=0,
            low_energy_threshold=4,
            whisper_detect_base=0.35,
            whisper_detect_ref_crowd=6,
            whisper_detect_min=0.05,
            whisper_detect_max=0.65,
            whisper_same_huddle_bonus=0.15,
            offer_ttl=1,
        ),
        items=dict(
            ration=dict(label="Ration", kind="edible", energy_restore=6),
            token=dict(label="Token", kind="fungible"),
            tile=dict(label="Tile", kind="arms", combat_bonus=1.5),
            badge=dict(label="Badge", kind="seal", unique=True),
        ),
        places=dict(
            atrium=dict(
                label="Atrium",
                paths=["annex"],
                pos=[0.1, 0.2],
                defense=0.35,
                initial_items={"badge": 1, "ration": 8, "tile": 2},
                regen={"ration": dict(rate=5, cap=12)},
                facility=dict(kind="press", copies_per_action=3),
            ),
            annex=dict(
                label="Annex",
                paths=["atrium"],
                facility=dict(kind="harvest", item="ration", yield_per_action=3),
            ),
        ),
        groups={"circle": group, "visitors": dict(group, public_tag="Neighbor")},
        ties=[dict(from_group="circle", to_group="visitors", mode="partition")],
        shocks=[],
        notices=[],
    )


def world(n=8):
    return World.create(fixture(), n, 7)


def turn(w, actions, trace=None):
    return w.settle(
        {a: (v if isinstance(v, Intent) else Intent(v)) for a, v in actions.items()}, trace=trace
    )


class Decisions(Scripted):
    def __init__(self, decide):
        super().__init__()
        self.choose = decide

    def decide(self, request):
        return encode(self.choose(request.actor, request.frame["view"])), {}


class PolityCase(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def store(self, spec=None, name="run", count=None):
        store = Store.create(self.folder / name, count, 7, scenario=spec or fixture())
        self.addCleanup(store.db.close)
        return store
