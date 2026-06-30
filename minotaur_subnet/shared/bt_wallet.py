"""Robust bittensor wallet loading for validator weight-signing.

The validator + api resolve their signing hotkey from a bittensor wallet. The SDK
default looks the wallet up under ``$HOME/.bittensor/wallets/`` — which, in the
hardened image, means ``/home/minotaur/.bittensor/wallets`` resolved for uid 1000,
backstopped by a ``/home/minotaur/.bittensor -> /root/.bittensor`` compat symlink.
That chain is brittle: an operator who mounts the wallet anywhere else, or whose
mount isn't readable by uid 1000, gets a bare "wallet load failed at startup" and a
dead weight-emitter (no champion weights ever signed).

This module removes that rigidity:
  * :func:`wallet_path_override` honours ``BT_WALLET_PATH`` (canonical, matches the
    miner) / ``WALLET_PATH`` (alias), so the wallet root can live anywhere.
  * :func:`load_hotkey_wallet` forces the hotkey file read at load time and, on
    failure, logs an ACTIONABLE diagnostic (the exact path tried, the file's
    owner/mode, and whether the current uid can read it) instead of a generic error.

Signing weights only needs the (unencrypted) hotkey file, so this never prompts for
a password — every failure here is a path/permission problem, which the diagnostic
pins down precisely.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def wallet_path_override() -> str | None:
    """Operator override for the bittensor wallet root, or ``None`` for the SDK
    default (``$HOME/.bittensor/wallets``).

    ``BT_WALLET_PATH`` wins (same name the miner uses); ``WALLET_PATH`` is accepted
    as an alias. Empty/unset → ``None`` (unchanged default behaviour)."""
    for env in ("BT_WALLET_PATH", "WALLET_PATH"):
        val = os.environ.get(env, "").strip()
        if val:
            return val
    return None


def load_hotkey_wallet(wallet_name: str, hotkey_name: str):
    """Return a ``bt.Wallet`` with its hotkey loaded, honouring the path override.

    Forces ``wallet.hotkey.ss58_address`` so a bad path / unreadable file fails HERE
    (not lazily, later). Re-raises after logging a diagnostic — callers keep their
    existing try/except semantics but now get an actionable log line on failure."""
    import bittensor as bt

    path = wallet_path_override()
    try:
        wallet = (
            bt.Wallet(name=wallet_name, hotkey=hotkey_name, path=path)
            if path
            else bt.Wallet(name=wallet_name, hotkey=hotkey_name)
        )
        # Force the file read so a wrong path / unreadable mount surfaces now.
        _ = wallet.hotkey.ss58_address
        return wallet
    except Exception:
        _log_wallet_diagnostic(wallet_name, hotkey_name, path)
        raise


def _log_wallet_diagnostic(wallet_name: str, hotkey_name: str, path: str | None) -> None:
    """Log exactly why a hotkey load failed: the resolved path, the file's
    owner/mode, and whether the current uid can read it — turning the silent
    "wallet load failed" into a one-line fix."""
    base = path or os.path.join(os.path.expanduser("~"), ".bittensor", "wallets")
    hk_file = os.path.join(base, wallet_name, "hotkeys", hotkey_name)
    uid = getattr(os, "getuid", lambda: -1)()
    info = [
        f"path={hk_file!r}",
        f"process_uid={uid}",
        f"HOME={os.environ.get('HOME', '?')!r}",
        f"BT_WALLET_PATH={os.environ.get('BT_WALLET_PATH', '') or '(unset)'}",
    ]
    try:
        st = os.stat(hk_file)
        info.append(f"exists=yes owner_uid={st.st_uid} mode={oct(st.st_mode & 0o777)}")
        info.append(f"readable_by_this_uid={os.access(hk_file, os.R_OK)}")
    except FileNotFoundError:
        info.append("exists=NO")
    except Exception as exc:  # pragma: no cover - defensive
        info.append(f"stat_failed={exc!r}")
    logger.error(
        "Hotkey wallet load FAILED — cannot sign weights. %s. "
        "Fix: point BT_WALLET_PATH at the mounted wallet root and ensure the hotkey "
        "file is readable by uid %s (chown/chmod the mount); verify "
        "WALLET_NAME=%r / HOTKEY_NAME=%r match the on-disk layout.",
        "  ".join(info), uid, wallet_name, hotkey_name,
    )
