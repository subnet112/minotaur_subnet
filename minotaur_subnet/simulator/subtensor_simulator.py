"""Substrate (Bittensor / chain 964) simulation backend.

Anvil can't execute subtensor's NATIVE precompiles (staking 0x805, alpha 0x808,
swap) — they are Substrate runtime code, invisible on a revm fork. This backend
drives a **Chopsticks fork of the real subtensor runtime** instead, where those
precompiles execute. It conforms to the same duck-typed surface as
``AnvilSimulator`` (``simulate`` / ``pin_read_fork`` / ``get_block_timestamp`` /
``is_connected``) so ``MultiChainSimulator`` routes chain 964 here transparently
(see ``registry.ChainSpec.sim_backend == "substrate_chopsticks"``).

It talks to the anvil-dialect sidecar in ``tools/chopsticks-sim/`` (which owns the
Chopsticks fork + the polkadot.js encode/decode) over a tiny JSON-RPC:
  anvil_setBalance / anvil_setCode / anvil_setStorageAt  — cheatcodes
  ck_ethCall({from,to,data,value,gas})                   — dry-run execution
  sim_forkBlock / sim_health / sim_mappedAccount         — introspection

Scoring model (verified end-to-end, see tools/chopsticks-sim/README.md): a single
dry-run ``ck_ethCall`` executes the plan against the pinned fork and returns
``{success, returnData, usedGas, logs}``. Precompile state changes are visible to
later reads WITHIN the same call (so a measuring App/router can return delivered
alpha as return data), and EVM logs come back for DEX-style apps — covering both
scoring paths with no block-building.

FIRST-CUT SCOPE: runs the plan's interactions and reports success + gas +
token_transfers (from logs). Per-round re-pinning and the App scoreIntent decode
are follow-ups (see the methods below).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

from minotaur_subnet.shared.types import (
    ExecutionPlan,
    SimulationResult,
    TokenTransfer,
)

logger = logging.getLogger(__name__)

# keccak256("Transfer(address,address,uint256)")
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_DEFAULT_EXECUTOR = "0x000000000000000000000000000000000000c0de"
# Generous native funding for the executor's mapped account (rao; 1 TAO = 1e9 rao).
_DEFAULT_FUND_RAO = 100_000 * 1_000_000_000


class SubtensorSimulator:
    """Simulate execution plans on a Chopsticks fork of subtensor via the sidecar."""

    def __init__(
        self,
        sidecar_url: str,
        chain_id: int = 964,
        default_executor: str = _DEFAULT_EXECUTOR,
        rpc_timeout: float = 60.0,
    ) -> None:
        self.sidecar_url = sidecar_url.rstrip("/")
        self.chain_id = chain_id
        self.default_executor = default_executor
        self.rpc_timeout = rpc_timeout
        self._pinned_block: int | None = None
        try:
            h = self._rpc("sim_health")
            self._pinned_block = h.get("pinBlock")
            logger.info(
                "SubtensorSimulator connected: %s (chain %d, fork block %s)",
                self.sidecar_url, chain_id, h.get("block"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SubtensorSimulator: sidecar %s not reachable: %s", self.sidecar_url, exc)

    # ── sidecar JSON-RPC ──────────────────────────────────────────────────────
    def _rpc(self, method: str, params: list | None = None) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        req = urllib.request.Request(
            self.sidecar_url, data=body, headers={"content-type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self.rpc_timeout) as resp:
            msg = json.loads(resp.read())
        if msg.get("error"):
            raise RuntimeError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result")

    # ── cheatcodes ────────────────────────────────────────────────────────────
    def set_balance(self, h160: str, rao: int) -> None:
        self._rpc("anvil_setBalance", [h160, str(int(rao))])

    def set_code(self, h160: str, code_hex: str) -> None:
        self._rpc("anvil_setCode", [h160, code_hex])

    def set_storage_at(self, h160: str, slot: str, value: str) -> None:
        self._rpc("anvil_setStorageAt", [h160, slot, value])

    def mapped_account(self, h160: str) -> str:
        return self._rpc("sim_mappedAccount", [h160])

    def eth_call(self, to: str, data: str, from_addr: str | None = None,
                 value: int = 0, gas: str | None = None) -> dict:
        return self._rpc("ck_ethCall", [{
            "from": from_addr or self.default_executor,
            "to": to, "data": data, "value": value, "gas": gas,
        }])

    # ── the AnvilSimulator-compatible surface ─────────────────────────────────
    def is_connected(self) -> bool:
        try:
            return bool(self._rpc("sim_health").get("ok"))
        except Exception:  # noqa: BLE001
            return False

    def pin_read_fork(self, chain_id: int, block_number: int) -> bool:
        """The Chopsticks fork is pinned at container launch (``--block``). Live
        re-pinning to an arbitrary historical block needs a re-fork, so this
        reports whether the sidecar is already pinned at ``block_number`` and
        warns otherwise — the deterministic contract is that the sidecar is
        launched at the round's benchmark fork block (CK_BLOCK). Per-round live
        re-pin (dev_setHead) is a follow-up."""
        if self._pinned_block is None:
            try:
                self._pinned_block = self._rpc("sim_health").get("pinBlock")
            except Exception:  # noqa: BLE001
                return False
        if self._pinned_block == block_number:
            return True
        logger.warning(
            "SubtensorSimulator: requested pin block %s != launch pin %s (chain %s); "
            "chopsticks fork stays at its launch block — launch the sidecar at the "
            "round fork block for determinism", block_number, self._pinned_block, chain_id,
        )
        return False

    def get_block_timestamp(self, chain_id: int, block_number: int | None = None) -> int | None:
        """Timestamp of the pinned fork block (from pallet_timestamp via the sidecar)."""
        try:
            return self._rpc("sim_forkTimestamp", [block_number])
        except Exception:  # noqa: BLE001
            return None

    async def simulate(
        self,
        plan: ExecutionPlan,
        contract_address: str | None = None,
        intent_order: dict | None = None,
        token_balances: dict[str, int] | None = None,
        fork_block: int | None = None,
        meter_gas: bool = False,
    ) -> SimulationResult:
        """Execute ``plan`` against the pinned Chopsticks fork as a dry-run and
        report the delivered-output surface. Signature matches
        ``AnvilSimulator.simulate`` so MultiChainSimulator routes here unchanged.

        NOTE: each interaction is an isolated dry-run (no persisted state between
        them — block-building is BLS-blocked on subtensor's runtime), so plans
        whose steps depend on a prior step's *persisted* state should encode the
        whole flow in one App/router call (the measuring-router pattern). Single
        App-call plans (staking/vault/DEX-via-app) are the target and work today.
        """
        if not plan.interactions:
            return SimulationResult(success=False, error="empty plan")

        if fork_block is not None:
            self.pin_read_fork(self.chain_id, fork_block)

        executor = (plan.metadata.get("executor") if plan.metadata else None) or self.default_executor
        # Fund the executor's mapped (coldkey) account so precompile stakes/txs
        # have balance. token_balances is EVM-wei keyed by token; for native TAO
        # we fund generously in rao.
        try:
            self.set_balance(executor, _DEFAULT_FUND_RAO)
        except Exception as exc:  # noqa: BLE001
            return SimulationResult(success=False, error=f"fund failed: {exc}")

        transfers: list[TokenTransfer] = []
        total_gas = 0
        last_return = None
        for i, ix in enumerate(plan.interactions):
            if ix.chain_id and ix.chain_id != self.chain_id:
                continue
            try:
                r = self.eth_call(
                    to=ix.target, data=ix.call_data,
                    from_addr=executor, value=int(ix.value or "0"),
                )
            except Exception as exc:  # noqa: BLE001
                return SimulationResult(success=False, error=f"interaction {i} rpc: {exc}")
            if not r.get("success"):
                return SimulationResult(
                    success=False,
                    error=f"interaction {i} reverted",
                    revert_reason=json.dumps(r.get("exitReason")),
                    gas_used=total_gas,
                )
            total_gas += int(r.get("usedGas") or 0, 16) if isinstance(r.get("usedGas"), str) else int(r.get("usedGas") or 0)
            last_return = r.get("returnData")
            transfers.extend(self._parse_transfers(r.get("logs") or []))

        result = SimulationResult(
            success=True,
            gas_used=total_gas,
            token_transfers=transfers,
        )
        # Pre-refund metered gas: the dry-run usedGas IS pre-refund EVM gas
        # (Frontier meters it before EIP-3529 refunds), which is exactly the
        # GAS-PAR "scoreintent_prerefund_v1" intent — so we surface it directly.
        if meter_gas:
            result.gas_metered = total_gas
        # Surface the terminal call's raw return so an App/router that measures
        # delivered output (e.g. staking alpha delta) can be scored. The per-App
        # scorer reads state_changes; we stash the raw return so a substrate
        # scorer JS can decode it. (Full scoreIntent decode: follow-up.)
        if last_return and last_return != "0x":
            result.state_changes = [{
                "type": "return_data", "chain_id": self.chain_id, "data": last_return,
            }]
        return result

    @staticmethod
    def _parse_transfers(logs: list[dict]) -> list[TokenTransfer]:
        out: list[TokenTransfer] = []
        for lg in logs:
            topics = lg.get("topics") or []
            if len(topics) >= 3 and topics[0].lower() == _TRANSFER_TOPIC:
                out.append(TokenTransfer(
                    token=lg.get("address", ""),
                    from_addr="0x" + topics[1][-40:],
                    to_addr="0x" + topics[2][-40:],
                    amount=str(int(lg.get("data", "0x0"), 16)),
                ))
        return out
