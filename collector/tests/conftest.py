from pathlib import Path

import pytest

from battery_collector import config

CFG = Path(__file__).resolve().parents[1] / "config" / "2a.toml"


@pytest.fixture
def cfg():
    return config.load(CFG)


@pytest.fixture
def mb(cfg):
    return cfg.mailbox


class Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()
