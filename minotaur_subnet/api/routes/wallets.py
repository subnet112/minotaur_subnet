"""Wallet management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from minotaur_subnet.api import services as _tools

router = APIRouter(tags=["wallets"])


# ── request models ───────────────────────────────────────────────────────────


class CreateWalletRequest(BaseModel):
    chain_ids: list[int] = Field(..., description="Chain IDs to support")


class FundWalletRequest(BaseModel):
    token: str = Field(..., description="Token address (plain 0x or CAIP-10)")
    amount: str = Field(..., description="Amount in wei (decimal string)")
    chain_id: int = Field(..., description="Target chain ID")
    depositor: str = Field("", description="User address to credit the deposit to (for DCA-style apps)")


# ── helpers ──────────────────────────────────────────────────────────────────


def _store():
    from minotaur_subnet.api.server import store
    return store


# ── routes ───────────────────────────────────────────────────────────────────


@router.post("/wallets/")
def create_wallet(body: CreateWalletRequest) -> dict[str, Any]:
    """Create a new managed wallet."""
    return _tools.create_wallet(_store(), body.chain_ids)


@router.get("/wallets/")
def list_wallets() -> dict[str, Any]:
    """List all managed wallets."""
    return _tools.list_wallets(_store())


@router.get("/wallets/{address}/balances")
def get_wallet_balances(address: str, chain_id: int = 31337) -> dict[str, Any]:
    """Query ETH + ERC-20 balances for an address. Works for any address."""
    return _tools.get_wallet_balances(address, chain_id)


@router.get("/wallets/{address:path}")
def get_wallet(address: str) -> dict[str, Any]:
    """Look up wallet information by address (plain 0x or CAIP-10)."""
    return _tools.get_wallet(_store(), address)


@router.post("/apps/{app_id}/fund")
def fund_wallet(app_id: str, body: FundWalletRequest) -> dict[str, Any]:
    """Deposit tokens into an App Intent's contract."""
    return _tools.fund_wallet(
        _store(),
        app_id=app_id,
        token=body.token,
        amount=body.amount,
        chain_id=body.chain_id,
        depositor=body.depositor,
    )


# ── testnet ─────────────────────────────────────────────────────────────────


class FaucetRequest(BaseModel):
    address: str = Field(..., description="Ethereum address (plain 0x or CAIP-10)")
    amount_eth: float = Field(10.0, description="Amount of ETH to fund")
    chain_id: int = Field(0, description="Target chain (0=first available, 31337=ETH fork, 8453=Base fork)")


@router.post("/testnet/faucet")
def faucet_eth(body: FaucetRequest) -> dict[str, Any]:
    """Fund an address with ETH on a local Anvil testnet fork."""
    return _tools.faucet_eth(body.address, body.amount_eth, chain_id=body.chain_id)


class FaucetErc20Request(BaseModel):
    token: str = Field(..., description="Token address, symbol (USDC), or chain-qualified (USDC@8453)")
    address: str = Field(..., description="Recipient address (plain 0x or CAIP-10)")
    amount: str = Field(..., description="Amount in token's smallest unit (decimal string)")
    chain_id: int = Field(0, description="Target chain (0=first available)")


@router.post("/testnet/faucet_erc20")
def faucet_erc20(body: FaucetErc20Request) -> dict[str, Any]:
    """Fund an address with ERC-20 tokens on a local Anvil testnet fork."""
    return _tools.faucet_erc20(body.token, body.address, body.amount, chain_id=body.chain_id)
