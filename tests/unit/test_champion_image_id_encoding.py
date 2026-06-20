"""Regression: the on-chain candidateImageId must equal the bare digest D.

``_str_to_bytes32`` decodes a 64-hex string to the raw 32 bytes but keccak-hashes
anything else. A docker/local image id is ``sha256:<64hex>`` (70 chars) → keccak,
which is NOT the digest. Content-addressed P1 must feed the BARE 64-hex (via
``image_transport.bare_hex``) so ``candidateImageId`` == the real GHCR manifest
digest. This locks that subtlety so a future refactor can't silently regress it.
"""

from eth_hash.auto import keccak

from minotaur_subnet.consensus.champion_manager import _str_to_bytes32
from minotaur_subnet.harness import image_transport as it
from minotaur_subnet.harness.round_store import ChampionSnapshot
from minotaur_subnet.harness.submission_store import SubmissionStore

HEX = "b3" * 32  # 64 hex chars
REPO = "ghcr.io/subnet112/minotaur-solver-candidates"


def test_bare_hex_encodes_to_the_raw_digest():
    # The bare 64-hex takes the bytes.fromhex branch -> on-chain value == D.
    assert _str_to_bytes32(HEX) == bytes.fromhex(HEX)
    assert len(_str_to_bytes32(HEX)) == 32


def test_sha256_prefixed_id_is_keccak_not_the_digest():
    # A docker {{.Id}} "sha256:<hex>" is 70 chars -> keccak(string), NOT the digest.
    prefixed = f"sha256:{HEX}"
    assert _str_to_bytes32(prefixed) == keccak(prefixed.encode("utf-8"))
    assert _str_to_bytes32(prefixed) != bytes.fromhex(HEX)  # the whole point of P1


def test_bare_hex_helper_feeds_the_encoder_correctly():
    # The transport helper strips repo + sha256: down to bare hex, so
    # _str_to_bytes32(bare_hex(ref)) == the real digest for every ref shape.
    ref = f"{REPO}@sha256:{HEX}"
    assert _str_to_bytes32(it.bare_hex(ref)) == bytes.fromhex(HEX)
    assert _str_to_bytes32(it.bare_hex(f"sha256:{HEX}")) == bytes.fromhex(HEX)
    assert _str_to_bytes32(it.bare_hex(HEX)) == bytes.fromhex(HEX)


def test_0x_prefixed_hex_still_decodes():
    assert _str_to_bytes32("0x" + HEX) == bytes.fromhex(HEX)


def test_empty_is_zero_bytes32():
    assert _str_to_bytes32("") == b"\x00" * 32
    assert _str_to_bytes32(None) == b"\x00" * 32


def test_submission_image_digest_round_trips(tmp_path):
    # The new image_digest field must survive store persistence (to_dict -> _load).
    ref = f"{REPO}@sha256:{HEX}"
    path = tmp_path / "subs.json"
    store = SubmissionStore(persist_path=path)
    sub = store.create(repo_url="r", commit_hash="c1234567", epoch=1, hotkey="5G", round_id="rd")
    store.set_image_digest(sub.submission_id, ref)
    reloaded = SubmissionStore(persist_path=path)  # fresh store -> loads from disk
    assert reloaded.get(sub.submission_id).image_digest == ref


def test_champion_snapshot_image_digest_round_trips():
    snap = ChampionSnapshot(submission_id="sub_1", image_digest=f"{REPO}@sha256:{HEX}")
    assert ChampionSnapshot.from_dict(snap.to_dict()).image_digest == f"{REPO}@sha256:{HEX}"
