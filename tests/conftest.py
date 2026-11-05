import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from salesdesk.brain import RulesBrain, Turn, normalise  # noqa: E402
from salesdesk.config import Settings  # noqa: E402
from salesdesk.desk import Desk  # noqa: E402
from salesdesk.shop import load_shop  # noqa: E402
from salesdesk.store import Store  # noqa: E402

LAGOS = ZoneInfo("Africa/Lagos")


class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


class ScriptedBrain:
    """Returns pre-written model turns and records the context it was given. No turns left = it was called
    when it shouldn't have been."""

    def __init__(self, *turns: dict):
        self.turns = list(turns)
        self.contexts: list[str] = []

    async def think(self, system: str, context: str) -> Turn:
        self.contexts.append(context)
        return Turn(normalise(self.turns.pop(0)), "scripted")


@pytest.fixture
def clock():
    # Wednesday 4 November 2026, 9:10pm Lagos: after closing.
    return Clock(datetime(2026, 11, 4, 21, 10, tzinfo=LAGOS))


@pytest.fixture
def make_desk(clock):
    def make(brain=None, **settings):
        s = Settings(provider="rules", shop_dir=ROOT / "demo", **settings)
        return Desk(s, Store(":memory:"), load_shop(ROOT / "demo"), brain or RulesBrain(), clock=clock)
    return make
