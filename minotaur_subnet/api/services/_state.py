"""Shared mutable state for the services package.

Module-level variables that are set by server.py at startup and read by
individual service modules.  Centralised here to avoid circular imports.
"""

from __future__ import annotations

from typing import Any, Callable


# Optional LitMpcWallet instance, set by the server at startup.
_wallet_manager: Any = None

# Optional DeployService instance, set by the server at startup.
_deploy_service: Any = None

# Chain info populated by the server at startup.
_chain_info: list[dict[str, Any]] = []

# Optional native Bittensor proxy executor, set by the server at startup.
_native_bittensor_executor: Any = None

# Optional callback that maps owner ss58 -> dedicated delegate ss58.
_native_bittensor_delegate_allocator: Callable[[str], str | None] | None = None

# Per-chain Anvil RPC URLs for the faucet.
_faucet_rpc_urls: dict[int, str] = {}


def set_wallet_manager(wallet_mgr: Any) -> None:
    """Configure the Lit MPC wallet manager for production wallet creation."""
    global _wallet_manager
    _wallet_manager = wallet_mgr


def get_wallet_manager() -> Any:
    """Return the wallet manager instance, or None."""
    return _wallet_manager


def set_deploy_service(deploy_svc: Any) -> None:
    """Configure the DeployService for real on-chain deployment."""
    global _deploy_service
    _deploy_service = deploy_svc


def set_chain_info(chains: list[dict[str, Any]]) -> None:
    """Populate the available chain list from server startup."""
    global _chain_info
    _chain_info = chains


def set_native_bittensor_executor(executor: Any) -> None:
    """Configure the native Bittensor delegated execution adapter."""
    global _native_bittensor_executor
    _native_bittensor_executor = executor


def set_native_bittensor_delegate_allocator(
    allocator: Callable[[str], str | None] | None,
) -> None:
    """Configure owner -> delegate resolution for native Bittensor permissions."""
    global _native_bittensor_delegate_allocator
    _native_bittensor_delegate_allocator = allocator


def set_faucet_rpc_urls(urls: dict[int, str]) -> None:
    """Configure per-chain Anvil RPC URLs for the faucet.

    Args:
        urls: Mapping of chain_id (int) to Anvil RPC URL (str).
              Example: ``{1: "http://localhost:8545", 8453: "http://localhost:8546"}``
    """
    global _faucet_rpc_urls
    _faucet_rpc_urls = dict(urls)
