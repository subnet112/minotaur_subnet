"""Business logic / service layer for the App Intents platform.

Service groups:
  1. Wallet management   -- create_wallet, get_wallet, fund_wallet, list_wallets
  2. App Intent lifecycle -- create_app_intent, deploy_app_intent,
                            list_minotaur_subnet, get_app_status
  3. Chain discovery      -- list_chains
  4. Monitoring           -- monitor_app, update_scoring
  5. Manifest & dry-run   -- get_app_manifest, dry_run_order
  6. Testnet              -- faucet_eth, faucet_erc20
  7. Native Bittensor     -- delegated permission management

All functions operate against an AppIntentStore for persistence and return
JSON-serialisable dicts that map to the shared types.

BACKWARD COMPATIBILITY: Every public function that existed in the old flat
services.py is re-exported here so that ``from minotaur_subnet.api.services
import X`` and ``from minotaur_subnet.api import services; services.X``
both continue to work unchanged.
"""

# ── shared state setters/getters (used by server.py at startup) ──────────
from ._state import (  # noqa: F401
    set_wallet_manager,
    get_wallet_manager,
    set_deploy_service,
    set_chain_info,
    set_native_bittensor_executor,
    set_native_bittensor_delegate_allocator,
    set_faucet_rpc_urls,
    # Expose mutable state for tests that poke at internals
    # (e.g. tests/unit/test_interop_api.py accesses _faucet_rpc_urls directly)
)

from . import _state as _state_mod  # noqa: F401


def __getattr__(name: str):
    """Dynamic attribute lookup for mutable state aliases.

    Tests access ``services._faucet_rpc_urls`` directly; this ensures
    they always get the current dict from ``_state``, even after
    ``set_faucet_rpc_urls()`` replaces it.
    """
    if name == "_faucet_rpc_urls":
        return _state_mod._faucet_rpc_urls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ── wallet service ───────────────────────────────────────────────────────
from .wallet_service import (  # noqa: F401
    create_wallet,
    get_wallet,
    list_wallets,
    get_wallet_balances,
    fund_wallet,
    _get_bittensor_balances,
)

# ── app intent lifecycle ─────────────────────────────────────────────────
from .app_service import (  # noqa: F401
    create_app_intent,
    deploy_app_intent,
    validate_app_intent_code,
    list_minotaur_subnet,
    get_app_status,
    update_scoring,
    get_app_manifest,
    build_intent_params_hex_from_manifest,
)
from .app_admin import get_app_admin_state  # noqa: F401
from .app_lifecycle import (  # noqa: F401
    update_app_solidity,
    retire_deployment,
    float_deposit,
    float_withdraw,
    set_app_config,
    registry_calldata,
    set_developer_allowed,
    auto_register_deployment,
    bootstrap_app_owner,
)
from .app_registration import (  # noqa: F401
    request_registration,
    approve_registration,
    reject_registration,
    registration_allows_autoregister,
)

# ── order / quote / approval ─────────────────────────────────────────────
from .order_service import (  # noqa: F401
    dry_run_order,
    ensure_token_approval,
    sign_user_order_for_managed_wallet,
    compute_intent_selector,
    _extract_manifest_safely,
)

# ── chain discovery ──────────────────────────────────────────────────────
from .chain_service import (  # noqa: F401
    list_chains,
)

# ── monitoring ───────────────────────────────────────────────────────────
from .monitoring_service import (  # noqa: F401
    monitor_app,
)

# ── testnet faucet ───────────────────────────────────────────────────────
from .testnet_service import (  # noqa: F401
    faucet_eth,
    faucet_erc20,
)

# ── native bittensor ─────────────────────────────────────────────────────
from .native_bittensor_service import (  # noqa: F401
    create_native_bittensor_permission,
    get_native_bittensor_permission,
    activate_native_bittensor_permission,
    list_native_bittensor_permissions,
    refresh_native_bittensor_permission,
    revoke_native_bittensor_permission,
    list_native_bittensor_executions,
    execute_native_bittensor_add_stake,
    execute_native_bittensor_move_stake,
)
