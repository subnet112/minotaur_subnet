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
        # sidecar_url may be a comma-separated POOL of sidecars for horizontal
        # throughput (the JS-wasm executor is single-threaded, so scoring hundreds
        # of candidates/round means fanning out across replicas). Each simulate()
        # picks one sidecar round-robin and does ALL its work (re-pin, fund, call)
        # on that one — the operations are stateful per fork instance.
        self._urls = [u.strip().rstrip("/") for u in str(sidecar_url).split(",") if u.strip()]
        if not self._urls:
            self._urls = [str(sidecar_url).rstrip("/")]
        self.sidecar_url = self._urls[0]
        self.chain_id = chain_id
        self.default_executor = default_executor
        self.rpc_timeout = rpc_timeout
        self._rr = 0
        self._pinned: dict[str, int | None] = {u: None for u in self._urls}
        for url in self._urls:
            try:
                h = self._rpc("sim_health", url=url)
                self._pinned[url] = h.get("pinBlock")
                logger.info(
                    "SubtensorSimulator connected: %s (chain %d, fork block %s)",
                    url, chain_id, h.get("block"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("SubtensorSimulator: sidecar %s not reachable: %s", url, exc)

    @property
    def _pinned_block(self) -> int | None:
        """Back-compat: the first sidecar's pinned block."""
        return self._pinned.get(self.sidecar_url)

    def _pick_url(self) -> str:
        url = self._urls[self._rr % len(self._urls)]
        self._rr += 1
        return url

    # ── sidecar JSON-RPC ──────────────────────────────────────────────────────
    def _rpc(self, method: str, params: list | None = None, url: str | None = None) -> Any:
        target = (url or self.sidecar_url)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        req = urllib.request.Request(target, data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.rpc_timeout) as resp:
            msg = json.loads(resp.read())
        if msg.get("error"):
            raise RuntimeError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result")

    # ── cheatcodes (url defaults to the first sidecar; simulate() threads its own) ─
    def set_balance(self, h160: str, rao: int, url: str | None = None) -> None:
        self._rpc("anvil_setBalance", [h160, str(int(rao))], url=url)

    def set_code(self, h160: str, code_hex: str, url: str | None = None) -> None:
        self._rpc("anvil_setCode", [h160, code_hex], url=url)

    def set_storage_at(self, h160: str, slot: str, value: str, url: str | None = None) -> None:
        self._rpc("anvil_setStorageAt", [h160, slot, value], url=url)

    def mapped_account(self, h160: str, url: str | None = None) -> str:
        return self._rpc("sim_mappedAccount", [h160], url=url)

    def eth_call(self, to: str, data: str, from_addr: str | None = None,
                 value: int = 0, gas: str | None = None, url: str | None = None) -> dict:
        return self._rpc("ck_ethCall", [{
            "from": from_addr or self.default_executor,
            "to": to, "data": data, "value": value, "gas": gas,
        }], url=url)

    # ── the AnvilSimulator-compatible surface ─────────────────────────────────
    def is_connected(self) -> bool:
        try:
            return bool(self._rpc("sim_health").get("ok"))
        except Exception:  # noqa: BLE001
            return False

    def pin_read_fork(self, chain_id: int, block_number: int) -> bool:
        """Re-anchor the Chopsticks fork to ``block_number`` for this round via the
        sidecar's ``sim_repin`` (dev_setHead) — no restart. Verified that this
        re-anchors STATE, not just the block number (native precompile reads match
        the archive node at the re-pinned block). Idempotent: a no-op when already
        pinned there, so scoring many candidates at one block re-pins once.

        Requires the sidecar's upstream (CK_ENDPOINT) to be an ARCHIVE node for a
        jump beyond its pruning window — the leader's blockmachine node is archive.
        Re-pin drops cheatcode overrides, so ``simulate`` re-pins BEFORE funding.
        Pins EVERY sidecar in the pool; True iff all landed on ``block_number``."""
        ok = True
        for url in self._urls:
            ok = self._repin_one(url, chain_id, block_number) and ok
        return ok

    def _repin_one(self, url: str, chain_id: int, block_number: int) -> bool:
        if self._pinned.get(url) == block_number:
            return True
        try:
            new_head = self._rpc("sim_repin", [int(block_number)], url=url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SubtensorSimulator: re-pin %s to %s failed (chain %s): %s",
                           url, block_number, chain_id, exc)
            return False
        self._pinned[url] = new_head
        if new_head != block_number:
            logger.warning(
                "SubtensorSimulator: re-pin %s landed on %s, requested %s — upstream "
                "may lack that block's state (needs an archive node)",
                url, new_head, block_number)
            return False
        return True

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

        # Pick ONE sidecar for this whole simulate() (round-robin across the pool)
        # — re-pin, fund, and call all target the same fork instance.
        url = self._pick_url()

        if fork_block is not None:
            self._repin_one(url, self.chain_id, fork_block)

        executor = (plan.metadata.get("executor") if plan.metadata else None) or self.default_executor
        # Fund the executor's mapped (coldkey) account so precompile stakes/txs
        # have balance. token_balances is EVM-wei keyed by token; for native TAO
        # we fund generously in rao.
        try:
            self.set_balance(executor, _DEFAULT_FUND_RAO, url=url)
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
                    from_addr=executor, value=int(ix.value or "0"), url=url,
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

        # ── delivered-output convention ──────────────────────────────────────
        # A substrate App's scored (terminal) call returns the exact delivered
        # output as its LAST 32-byte return word — for StakeMeter.stakeAndMeasure
        # -> (before, after, delta) that's `delta` (alpha received); for an App
        # whose scoreIntent returns (…, rawOutput) it's rawOutput; for a bare
        # `rawOutput` return it's the only word. We surface it as a TYPED
        # state_change so the per-App raw-output scorer JS
        # (harness/scoring_shadow/subtensor_stake_raw.js) reads it exactly like
        # the DEX scorer reads token_transfers. raw_output stays an opaque BigInt
        # downstream, so relative_scoring is unchanged.
        state_changes: list[dict[str, Any]] = []
        if last_return and last_return != "0x":
            state_changes.append({
                "type": "return_data", "chain_id": self.chain_id, "data": last_return,
            })
            delivered = self._last_word(last_return)
            if delivered is not None:
                state_changes.append({
                    "type": "delivered_output", "chain_id": self.chain_id,
                    "token": "alpha", "amount": str(delivered),
                })
        result.state_changes = state_changes

        # ── optional on-chain score (BPS) ────────────────────────────────────
        # If the order carries pre-built scoreIntent calldata for the App, call it
        # (read-only) and decode (uint256 score, bool valid) into on_chain_score,
        # mirroring the anvil scoreIntent path. Best-effort: absent/failed leaves
        # on_chain_score=None (raw_output still drives scoring).
        sic = (intent_order or {}).get("score_intent_calldata") if intent_order else None
        if contract_address and sic:
            try:
                r = self.eth_call(to=contract_address, data=sic, from_addr=executor, url=url)
                if r.get("success"):
                    score = self._word((r.get("returnData") or "0x"), 0)  # (uint256 score, bool valid)
                    if score is not None:
                        result.on_chain_score = score
            except Exception:  # noqa: BLE001
                pass
        return result

    @staticmethod
    def _word(ret_hex: str, index: int) -> int | None:
        """Decode the ``index``-th 32-byte ABI word of a return blob as uint256."""
        h = ret_hex[2:] if ret_hex.startswith("0x") else ret_hex
        lo, hi = index * 64, (index + 1) * 64
        if len(h) < hi:
            return None
        return int(h[lo:hi], 16)

    @classmethod
    def _last_word(cls, ret_hex: str) -> int | None:
        """Decode the LAST 32-byte ABI word of a return blob as uint256."""
        h = ret_hex[2:] if ret_hex.startswith("0x") else ret_hex
        if len(h) < 64:
            return None
        return int(h[-64:], 16)

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
