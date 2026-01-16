from typing import Any

from neurons.metagraph_manager import MetagraphManager


class FakeSubtensor:
    def __init__(self, uid_for_hotkey: dict):
        self._uid_for_hotkey = uid_for_hotkey
        self.network = "local"

    def get_uid_for_hotkey_on_subnet(self, hotkey: str, netuid: int):
        return self._uid_for_hotkey.get(hotkey)

    def get_current_block(self):
        return 100


class FakeWallet:
    def __init__(self, hotkey: str):
        self.hotkey = type("Hotkey", (), {"ss58_address": hotkey})()


class FakeMetagraph:
    def __init__(self, hotkeys, uids, validator_permit):
        self.hotkeys = hotkeys
        self.uids = uids
        self.validator_permit = validator_permit


def test_metagraph_snapshot_builds_uid_map_and_permit():
    subtensor = FakeSubtensor(uid_for_hotkey={"hk-validator": 3})
    wallet = FakeWallet("hk-validator")
    logger = type("Logger", (), {"error": lambda *a, **k: None})()

    manager = MetagraphManager(subtensor=subtensor, wallet=wallet, netuid=1, logger=logger)

    metagraph = FakeMetagraph(
        hotkeys=["hk-1", "hk-2", "hk-validator"],
        uids=[0, 1, 3],
        validator_permit=[True, True, False, True],
    )

    snapshot = manager._build_snapshot(metagraph)

    assert snapshot is not None
    assert snapshot.uid_for_hotkey["hk-1"] == 0
    assert snapshot.uid_for_hotkey["hk-2"] == 1
    assert snapshot.uid_for_hotkey["hk-validator"] == 3
    assert snapshot.validator_uid == 3
    assert snapshot.validator_permit is True


