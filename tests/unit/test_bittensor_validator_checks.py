import asyncio
import types

from neurons.bittensor_validator import BittensorValidator
from neurons.metagraph_manager import MetagraphSnapshot


class DummyManager:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    async def get_current_metagraph(self):
        return self._snapshot


class DummyWallet:
    def __init__(self, hotkey):
        self.hotkey = types.SimpleNamespace(ss58_address=hotkey)


class DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


def test_check_wallet_registration_requires_permit():
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={"hk": 3},
        size=1,
        validator_permit=False,
        validator_uid=3,
    )

    validator = BittensorValidator.__new__(BittensorValidator)
    validator._metagraph_manager = DummyManager(snapshot)
    validator.wallet = DummyWallet("hk")
    validator.config = types.SimpleNamespace(netuid=1)
    validator.logger = DummyLogger()

    result = asyncio.run(BittensorValidator._check_wallet_registration(validator))

    assert result is False


def test_check_wallet_registration_with_permit():
    snapshot = MetagraphSnapshot(
        uid_for_hotkey={"hk": 3},
        size=1,
        validator_permit=True,
        validator_uid=3,
    )

    validator = BittensorValidator.__new__(BittensorValidator)
    validator._metagraph_manager = DummyManager(snapshot)
    validator.wallet = DummyWallet("hk")
    validator.config = types.SimpleNamespace(netuid=1)
    validator.logger = DummyLogger()

    result = asyncio.run(BittensorValidator._check_wallet_registration(validator))

    assert result is True

