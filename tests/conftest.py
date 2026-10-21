import os, sys, tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="angelbot_test_")
os.environ["ANGELBOT_RUNTIME"] = os.path.join(_TMP, "runtime")
os.environ["ANGELBOT_DATA"] = os.path.join(_TMP, "data")
os.environ.pop("ANGELBOT_MODE", None)
os.environ.pop("ANGELBOT_LIVE_CONFIRM", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import copy
import pytest
from datetime import datetime, timedelta
from bot.config import load_config
from bot.models import Bar, Instrument
from bot.timeutil import IST

FIXTURE_MASTER = Path(__file__).parent / "fixtures" / "scrip_sample.json"


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c["instruments"] = [{"symbol": "SBIN-EQ", "exchange": "NSE", "qty": 1, "product": "INTRADAY"}]
    return c


@pytest.fixture
def inst():
    return Instrument("SBIN-EQ", "3045", "NSE", 1, 0.05, 1, "INTRADAY")


def make_bars(closes, start="2026-10-14 09:15", spread=0.5):
    t0 = datetime.fromisoformat(start).replace(tzinfo=IST)
    bars, prev = [], closes[0]
    for k, c in enumerate(closes):
        o = prev
        bars.append(Bar(t0 + timedelta(minutes=k), o, max(o, c) + spread, min(o, c) - spread, c, 1000))
        prev = c
    return bars
