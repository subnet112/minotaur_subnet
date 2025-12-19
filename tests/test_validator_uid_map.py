from typing import Any, Dict

import types
import pytest

import neurons.validator as vmod


class FakeMetagraph:
    def __init__(self, hotkeys, uids):
        self.hotkeys = hotkeys
        self.uids = uids

    def sync(self, subtensor: Any, lite: bool = True) -> None:  # pragma: no cover - no-op
        return


class FakeBt:
    def __init__(self, hotkeys, uids):
        self._mg = FakeMetagraph(hotkeys, uids)
        self.logging = types.SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )

    def metagraph(self, netuid: int, subtensor: Any) -> FakeMetagraph:  # type: ignore[override]
        return self._mg


def test_validator_uses_metagraph_first(monkeypatch: Any) -> None:
    # Arrange: prepare a Validator instance without running __init__
    Validator = vmod.Validator
    v = Validator.__new__(Validator)

    # Minimal config/subtensor stubs
    v.config = types.SimpleNamespace(netuid=2)
    v.subtensor = types.SimpleNamespace(network="local")

    # Fake bt returns a metagraph with 2 entries
    fake_bt = FakeBt(
        hotkeys=["HK1", "HK2"],
        uids=[0, 5],
    )
    monkeypatch.setattr(vmod, "bt", fake_bt)

    # Act
    mapping: Dict[str, int] = v._build_miner_id_to_uid_map()

    # Assert
    assert mapping == {"HK1": 0, "HK2": 5}


