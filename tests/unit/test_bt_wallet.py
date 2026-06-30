"""Robust bittensor wallet loading (shared.bt_wallet).

Pins the path-override precedence and the actionable failure diagnostic that
replaces the bare "wallet load failed" — the silent dead-emitter mode that left a
follower validator unable to sign weights.
"""

import io
import logging

import pytest

from minotaur_subnet.shared import bt_wallet


def test_wallet_path_override_precedence(monkeypatch):
    monkeypatch.delenv("BT_WALLET_PATH", raising=False)
    monkeypatch.delenv("WALLET_PATH", raising=False)
    assert bt_wallet.wallet_path_override() is None       # unset → SDK default

    monkeypatch.setenv("WALLET_PATH", "/mnt/w")
    assert bt_wallet.wallet_path_override() == "/mnt/w"    # alias honoured

    monkeypatch.setenv("BT_WALLET_PATH", "/data/w")
    assert bt_wallet.wallet_path_override() == "/data/w"   # BT_WALLET_PATH wins

    monkeypatch.setenv("BT_WALLET_PATH", "   ")            # blank ignored → fall back
    assert bt_wallet.wallet_path_override() == "/mnt/w"


def _capture_module_log(func, *args):
    """Run *func* with a handler attached directly to the bt_wallet logger and
    return what it logged. Direct attach (not caplog) so the assertion survives
    bittensor reconfiguring the logging tree elsewhere in the suite."""
    log = logging.getLogger("minotaur_subnet.shared.bt_wallet")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.ERROR)
    prev_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.ERROR)
    try:
        func(*args)
    finally:
        log.removeHandler(handler)
        log.setLevel(prev_level)
    return buf.getvalue()


def test_diagnostic_names_path_and_fix(tmp_path):
    # The diagnostic must name the exact resolved path, exists=NO, the process uid,
    # the override env, and the wallet/hotkey names — so the operator isn't guessing.
    msg = _capture_module_log(
        bt_wallet._log_wallet_diagnostic, "no_such_wallet", "default", str(tmp_path),
    )
    assert "Hotkey wallet load FAILED" in msg
    assert "exists=NO" in msg
    assert str(tmp_path) in msg                  # resolved path root
    assert "no_such_wallet" in msg               # WALLET_NAME echoed
    assert "BT_WALLET_PATH" in msg               # the override surfaced
    assert "uid" in msg                          # readability hint


def test_load_hotkey_wallet_missing_raises(monkeypatch, tmp_path):
    # An empty wallet root → the hotkey file can't exist → the load must FAIL
    # (re-raised), not silently return a broken wallet.
    monkeypatch.setenv("BT_WALLET_PATH", str(tmp_path))
    monkeypatch.delenv("WALLET_PATH", raising=False)
    with pytest.raises(Exception):
        bt_wallet.load_hotkey_wallet("no_such_wallet", "default")
