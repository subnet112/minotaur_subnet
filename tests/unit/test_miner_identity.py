"""GitHub-account ↔ hotkey identity binding: gist proof + persistent store.

Covers the registration verification (api/services/miner_identity), the gist
resolver (github_pr.resolve_gist), and that the binding survives a restart
(the SQLite store in AppIntentStore).
"""

import json
from unittest.mock import MagicMock

import pytest

from bittensor_wallet.keypair import Keypair

from minotaur_subnet.api.routes.submissions import github_pr as gp
from minotaur_subnet.api.services import miner_identity as mi
from minotaur_subnet.store.app_intent_store import AppIntentStore


# ── resolve_gist ──────────────────────────────────────────────────────────────


def _gist(owner="miner", content="x"):
    return {"owner": {"login": owner}, "files": {"proof.json": {"content": content}}}


def test_resolve_gist_returns_owner_and_content():
    owner, content = gp.resolve_gist("abc123", fetch=lambda gid: _gist("MinerX", "hello"))
    assert owner == "MinerX"
    assert content == "hello"


def test_resolve_gist_rejects_malformed_id():
    # Path-traversal / non-alphanumeric id must never reach the URL.
    with pytest.raises(gp.PRResolutionError):
        gp.resolve_gist("../../etc/passwd", fetch=lambda gid: _gist())


def test_resolve_gist_rejects_missing_owner():
    with pytest.raises(gp.PRResolutionError):
        gp.resolve_gist("abc", fetch=lambda gid: {"owner": None, "files": {"f": {"content": "x"}}})


def test_resolve_gist_rejects_empty_files():
    with pytest.raises(gp.PRResolutionError):
        gp.resolve_gist("abc", fetch=lambda gid: {"owner": {"login": "m"}, "files": {}})


# ── link_miner_identity (the proof verification) ──────────────────────────────


def _signed_gist(github_login: str, keypair: Keypair):
    """(owner_login, content) as resolve_gist returns, signed by ``keypair``."""
    hotkey = keypair.ss58_address
    sig = keypair.sign(mi.identity_message(github_login, hotkey).encode("utf-8")).hex()
    return github_login, json.dumps({"hotkey": hotkey, "signature": sig})


def test_link_valid_proof_persists_binding():
    alice = Keypair.create_from_uri("//Alice")
    store = MagicMock()
    ok, err, binding = mi.link_miner_identity(
        store, "abc123",
        resolve=lambda gid: _signed_gist("MinerAlice", alice),
        now=123.0,
    )
    assert ok, err
    assert binding == {"github_login": "mineralice", "hotkey": alice.ss58_address}
    # The gist OWNER (authoritative) is what gets bound — not a self-declared field.
    args, kwargs = store.set_miner_identity.call_args
    assert args[0] == "MinerAlice" and args[1] == alice.ss58_address
    assert kwargs["proof_ref"] == "abc123" and kwargs["linked_at"] == 123.0


def test_link_rejects_signature_over_different_account():
    # Signing the binding for a github login OTHER than the gist owner must fail —
    # this is the copier who signs their own binding but hosts under someone else.
    alice = Keypair.create_from_uri("//Alice")

    def resolve(gid):
        hotkey = alice.ss58_address
        sig = alice.sign(mi.identity_message("someone-else", hotkey).encode()).hex()
        return "MinerAlice", json.dumps({"hotkey": hotkey, "signature": sig})

    store = MagicMock()
    ok, _err, binding = mi.link_miner_identity(store, "abc", resolve=resolve)
    assert not ok and binding == {}
    store.set_miner_identity.assert_not_called()


def test_link_rejects_hotkey_mismatch():
    # Claim Bob's hotkey but sign with Alice → verification against Bob fails.
    alice = Keypair.create_from_uri("//Alice")
    bob = Keypair.create_from_uri("//Bob")

    def resolve(gid):
        sig = alice.sign(mi.identity_message("Miner", bob.ss58_address).encode()).hex()
        return "Miner", json.dumps({"hotkey": bob.ss58_address, "signature": sig})

    store = MagicMock()
    ok, _err, _ = mi.link_miner_identity(store, "abc", resolve=resolve)
    assert not ok
    store.set_miner_identity.assert_not_called()


def test_link_rejects_non_json_gist():
    store = MagicMock()
    ok, _err, _ = mi.link_miner_identity(store, "abc", resolve=lambda gid: ("Miner", "not json"))
    assert not ok
    store.set_miner_identity.assert_not_called()


# ── persistence: the binding survives a restart ───────────────────────────────


def test_binding_survives_restart(tmp_path):
    db = tmp_path / "identity.db"
    s1 = AppIntentStore(store_path=db)
    s1.set_miner_identity("MinerX", "5GHotkeyAlice", proof_ref="gist1", linked_at=1.0)

    # Reopen a fresh store on the same file — simulates an api restart.
    s2 = AppIntentStore(store_path=db)
    assert s2.get_miner_hotkey("minerx") == "5GHotkeyAlice"
    assert s2.get_miner_hotkey("MINERX") == "5GHotkeyAlice"  # case-insensitive
    assert s2.get_miner_hotkey("unregistered") == ""


def test_set_miner_identity_upserts(tmp_path):
    s = AppIntentStore(store_path=tmp_path / "id.db")
    s.set_miner_identity("Miner", "5GOld", proof_ref="g1", linked_at=1.0)
    s.set_miner_identity("miner", "5GNew", proof_ref="g2", linked_at=2.0)  # same login, re-link
    assert s.get_miner_hotkey("miner") == "5GNew"
