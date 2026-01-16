from neurons.miner import Miner


class FakeKeypair:
    def __init__(self):
        self.ss58_address = "hk-bittensor"

    def sign(self, message: bytes):
        return b"\x01" * 64


class FakeWallet:
    def __init__(self):
        self.hotkey = FakeKeypair()


def test_miner_signing_bittensor_mode():
    miner = Miner(
        miner_id="ignored",
        aggregator_url="http://example",
        miner_api_key="key",
        mode="bittensor",
        wallet=FakeWallet(),
    )

    sig = miner._sign_message(b"hello")

    assert miner.signature_type == "sr25519"
    assert sig.startswith("0x")
    assert len(sig) == 2 + 64 * 2


def test_miner_signing_simulation_mode():
    miner = Miner(
        miner_id="simulation-id",
        aggregator_url="http://example",
        miner_api_key="key",
        mode="simulation",
    )

    sig = miner._sign_message(b"hello")

    assert miner.signature_type == "ed25519"
    assert sig.startswith("0x")
    # ed25519 signature is 64 bytes
    assert len(sig) == 2 + 64 * 2

