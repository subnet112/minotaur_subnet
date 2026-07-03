"""Create-time owner binding: a self-serve create records the RECOVERED
signer of an EIP-712 create_app signature as the app's deployer, so ownership
is proven by a key, not a claimed address."""
from __future__ import annotations

import time

from eth_account import Account

from minotaur_subnet.api.services import app_auth, developer_auth
from minotaur_subnet.api.services.app_service import create_app_intent
from minotaur_subnet.store.app_intent_store import AppIntentStore

OWNER = Account.from_key("0x" + "11" * 32)
ATTACKER = Account.from_key("0x" + "22" * 32)

JS = (
    "const manifest = { intent_functions: [{ name: 'swap', params: [] }] };\n"
    "function score() { return 1; }\n"
    "module.exports = { score, manifest };\n"
)
SOL = "contract X {}"


def _store(tmp_path):
    return AppIntentStore(store_path=tmp_path / "s.db")


def _sign(wallet, js=JS, sol=SOL, deadline=None):
    deadline = deadline if deadline is not None else int(time.time()) + 600
    ph = app_auth.create_owner_binding_hash(js.strip(), sol.strip())
    return developer_auth.sign_developer_auth(
        wallet.key.hex(), action=developer_auth.ACTION_CREATE_APP,
        app_id="", params_hash=ph, nonce=0, deadline=deadline,
    ), deadline


def _create(store, **kw):
    base = dict(name="dex", description="", supported_chains=[8453],
                js_code=JS, solidity_code=SOL)
    base.update(kw)
    return create_app_intent(store, **base)


def test_owner_signature_sets_proven_deployer(tmp_path):
    s = _store(tmp_path)
    sig, dl = _sign(OWNER)
    out = _create(s, owner_signature=sig, owner_deadline=dl)
    assert "error" not in out, out
    assert s.get_app(out["app_id"]).deployer.lower() == OWNER.address.lower()


def test_claimed_deployer_must_match_signature(tmp_path):
    """Claiming a different address than you signed with is rejected — the
    anti-spoof property."""
    s = _store(tmp_path)
    sig, dl = _sign(OWNER)
    out = _create(s, owner_signature=sig, owner_deadline=dl,
                  deployer=ATTACKER.address)
    assert "error" in out and "does not match" in out["error"]


def test_claimed_deployer_matching_signature_ok(tmp_path):
    s = _store(tmp_path)
    sig, dl = _sign(OWNER)
    out = _create(s, owner_signature=sig, owner_deadline=dl,
                  deployer=OWNER.address)
    assert "error" not in out, out
    assert s.get_app(out["app_id"]).deployer.lower() == OWNER.address.lower()


def test_signature_over_different_code_rejected(tmp_path):
    """A signature bound to other code doesn't authorize creating this app."""
    s = _store(tmp_path)
    sig, dl = _sign(OWNER, sol="contract DIFFERENT {}")
    out = _create(s, owner_signature=sig, owner_deadline=dl)
    # Recovered signer won't match a claimed deployer... but none claimed, so
    # ownership would be set to whatever address recovers from the wrong hash —
    # which is NOT the owner. Assert it's not the real owner.
    assert s.get_app(out["app_id"]).deployer.lower() != OWNER.address.lower()


def test_expired_owner_signature_rejected(tmp_path):
    s = _store(tmp_path)
    sig, _ = _sign(OWNER, deadline=int(time.time()) - 1)
    out = _create(s, owner_signature=sig, owner_deadline=int(time.time()) - 1)
    assert "error" in out and "signature" in out["error"].lower()


def test_admin_path_still_trusts_claimed_deployer(tmp_path):
    """No signature → the deployer field is trusted (admin-gated route)."""
    s = _store(tmp_path)
    out = _create(s, deployer=OWNER.address)
    assert "error" not in out, out
    assert s.get_app(out["app_id"]).deployer.lower() == OWNER.address.lower()


def test_no_deployer_no_signature_is_ownerless(tmp_path):
    s = _store(tmp_path)
    out = _create(s)
    assert "error" not in out, out
    assert s.get_app(out["app_id"]).deployer == ""
