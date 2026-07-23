"""AnvilSimulator — executes execution plans on a running Anvil fork.

Uses snapshot/revert for isolation: each simulate() call leaves no
lasting state changes on the fork.

Defense-in-depth note (PR-7, audit finding C4):
  PRIMARY containment is now at the NETWORK layer: the anvil forks live
  ONLY on the `minotaur` net (the validator/simulator execution path) and
  are NO LONGER on the sealed `benchmark-sandbox` net. An untrusted
  benchmark solver on that net can reach only the read-only block-pin proxy
  (172.30.0.5), never a raw anvil RPC — so it cannot call the unauthenticated
  `anvil_*` / `hardhat_*` / `evm_*` cheat-code namespace (which anvil has no
  flag to disable) to bias its own benchmark or poison fork state the
  validator later re-reads. This baseline/probe boundary below is retained as
  a backstop for an IN-PROCESS state change (a simulation whose revert
  silently failed), not for a direct external attacker — which is now cut off.

  Our boundary: snapshot at startup ("baseline"), snapshot again per
  simulation, revert in a finally. If a revert fails, the baseline
  lets us recover. `_assert_baseline_alive()` periodically probes a
  known-stable storage slot to catch out-of-band state mutation; on
  mismatch, we force a re-fork (when upstream is available) or raise
  SimulatorStateError. The probe is cheap but does an eth_call per
  invocation, so it runs once every `BASELINE_PROBE_EVERY` simulate()
  calls (default 100) — trade-off between safety and per-sim overhead.

Requires a running Anvil instance (local testnet or standalone).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

import requests
from web3 import Web3

from minotaur_subnet.rpc_backoff import body_has_retryable_rpc_error, retry_sync
from minotaur_subnet.shared.types import (
    ExecutionPlan,
    SimulationResult,
    TokenTransfer,
    extract_leg_plan,
)
from minotaur_subnet.simulator.revert_decoder import (
    decode_call,
    extract_revert_via_trace,
)

logger = logging.getLogger(__name__)


class SimulatorStateError(RuntimeError):
    """Raised when the anvil fork's baseline state cannot be restored.

    Indicates state poisoning detected alongside a failed snapshot/revert
    recovery. With the anvils now off the benchmark-sandbox net, an untrusted
    solver can no longer reach the cheat-code namespace directly, so this most
    likely means an IN-PROCESS state change whose revert silently failed.
    Callers should treat it as a hard failure — the fork must be recycled
    (container restart) before further simulations can be trusted.
    """


# How often to run the baseline-alive probe inside simulate().
# 1-in-N: at N=100 with 12s tick * ~3 sims/tick, probe fires ~once every
# 7 minutes per simulator. Cheap (one eth_call) but not free.
BASELINE_PROBE_EVERY = 100

# ERC-20 Transfer(address,address,uint256) event topic
# keccak256("Transfer(address,address,uint256)")
_TRANSFER_TOPIC = bytes.fromhex(
    "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

# Default executor address (Anvil account 0, pre-funded with 10,000 ETH)
_DEFAULT_EXECUTOR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

# Deterministic sim-block timestamp pin (benchmark determinism).
#
# Without a pin, anvil stamps every block it mines with wall-clock time
# anchored at the fork point (fork_ts + seconds elapsed since the reset), so
# two validators simulating the same plan at the same fork pin see DIFFERENT
# block.timestamp values — a nondeterminism channel into scoreIntent (deadline
# math, TWAPs, any time-dependent app logic). Instead, every block mined
# inside a simulation is pinned to ``fork_block_timestamp + this offset`` via
# ``evm_setNextBlockTimestamp``, making block.timestamp a pure function of the
# fork pin. +12 ≈ one L1 slot past the fork block; any fixed value works, it
# only has to be fleet-uniform (CODE constant, never env-read).
#
# Empirically verified on anvil 1.5.1:
#   - the pin is ONE-SHOT: it applies to the next mined block only, after
#     which timestamps float back to wall clock → re-apply before EVERY
#     block-mining operation;
#   - a pinned timestamp EQUAL to the previous block's is accepted (only
#     strictly-lower is rejected), so every sim block can land on the same
#     pinned second regardless of how long the sim takes;
#   - ``evm_revert`` discards a pending pin and restores the time state of
#     the snapshot → the pin must be re-applied per simulation (it is: every
#     mining site pins, and ``_reset_fork`` runs per simulate() call);
#   - ``anvil_reset`` re-anchors time exactly to the fork block's timestamp
#     and rewinds the last-timestamp check, so the constant pin is always
#     applicable after the per-sim fork reset.
SIM_BLOCK_TIMESTAMP_OFFSET = 12

# ── GasMeter (benchmark-only pre-refund gas measurement) ─────────────────────
#
# Vendored RUNTIME bytecode of GasMeter.sol (source in PR body / spike dir).
# Provenance: solc 0.8.24, evm_version=cancun, optimizer=true, runs=200
# (forge build; runtime bytecode = deployedBytecode, 154 bytes). The contract
# is a raw-assembly fallback that:
#   - on a TOP-LEVEL call (tx.origin == msg.sender): copies calldata, brackets
#     ``g0=gas(); call(gas(), app, callvalue(), ...); used=g0-gas()`` where
#     ``app`` is read from storage slot 0, bubbles reverts unchanged, emits
#     ``GasMeasured(uint256 used)`` on success, and returns the app's
#     returndata unchanged;
#   - on an INNER call (some contract poking the relayer address): behaves
#     exactly like the code-less relayer EOA — accepts value, returns empty.
#
# It is installed via ``anvil_setCode`` AT THE RELAYER ADDRESS for the span of
# one metered probe tx (so the app's ``onlyRelayer`` check passes with zero
# contract changes) and never touches a live chain. ``gasleft()`` is
# refund-invisible (EIP-3529 refunds apply at tx finalization), so the emitted
# value is PRE-refund inner-call gas: refund farming ADDS metered cost, and
# intrinsic/calldata gas is excluded. Empirically verified 36/36 on anvil
# 1.5.1 (spike results.json: metered state/logs/returndata parity with the
# direct send, refund invariance, revert bubbling, msg.value forwarding).
GAS_METER_RUNTIME_HEX = (
    "0x608060405236600a57005b323314601257005b5f54365f80375a5f80365f34865af1"
    "91505a9003816032573d5f803e3d5ffd5b805f5250507f4a3f3d1bb56898b0a37c0749"
    "5fa253797670c64bc9f0917848aebb6463f0cd9f60205fa13d5f803e3d5ff3fea26469"
    "70667358221220c6c99b5b99ee385cd4c9242015a450a822b5e96742281e9bfa6fd8a2"
    "ea6d1c0c64736f6c63430008180033"
)

# keccak256("GasMeasured(uint256)") — topic0 of the meter's event. Logs only
# count when ALSO emitted from the relayer address (the meter's install
# address), so an app emitting a same-signature event cannot spoof it.
GAS_MEASURED_TOPIC0 = (
    "0x4a3f3d1bb56898b0a37c07495fa253797670c64bc9f0917848aebb6463f0cd9f"
)

# Fixed, code-less, impersonated sender EOA for the metered probe tx. It must
# differ from the relayer: with meter code installed at the relayer address, a
# tx FROM the relayer could trip EIP-3607 (sender-has-code) depending on the
# anvil version — a separate funded EOA is safe on every version.
GAS_METER_SENDER_EOA = "0x2222000000000000000000000000000000002222"

# Gas limit for the metered probe tx: the direct send's 2,000,000 plus
# headroom for the meter bracket (~2.6k CALL/copy overhead + ~3.3k epilogue,
# measured in the spike). 0x1F1FA0 == 2,040,000.
GAS_METER_TX_GAS = 0x1F1FA0


def _log_entry_field(entry: Any, name: str) -> Any:
    """Field access for a receipt log entry: dict key or attribute."""
    if isinstance(entry, dict):
        return entry.get(name)
    try:
        return entry[name]  # web3 AttributeDict supports item access
    except Exception:
        return getattr(entry, name, None)


def _as_hex_str(value: Any) -> str | None:
    """Normalize HexBytes/bytes/str log fields to a 0x-prefixed lowercase hex
    string; None when the value is missing or not hex-like."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex().lower()
    if isinstance(value, str):
        v = value.lower()
        return v if v.startswith("0x") else "0x" + v
    hex_fn = getattr(value, "hex", None)  # HexBytes and friends
    if callable(hex_fn):
        try:
            h = hex_fn()
            return h.lower() if h.startswith("0x") else "0x" + h.lower()
        except Exception:
            return None
    return None


def parse_gas_measured(logs: Any, relayer_address: str) -> int | None:
    """Parse the GasMeter's ``GasMeasured(uint256)`` value from receipt logs.

    Pure function (the meter self-test hook): filters for logs whose
    ``address`` equals ``relayer_address`` (the meter's install address) AND
    whose ``topics[0]`` equals :data:`GAS_MEASURED_TOPIC0`, then decodes the
    32-byte data word. Returns the LAST matching value (the meter emits
    exactly one per metered tx; the address+topic filter makes app-side
    spoofing impossible — app logs carry the app's address), or None when
    absent — which is structural on a revert: the meter never emits before
    bubbling a revert. Accepts web3 AttributeDict/HexBytes rows and plain
    dict/str rows; malformed rows are skipped, never raised on.
    """
    if not logs:
        return None
    want_addr = str(relayer_address).lower()
    if not want_addr.startswith("0x"):
        want_addr = "0x" + want_addr
    found: int | None = None
    for entry in logs:
        try:
            addr = _as_hex_str(_log_entry_field(entry, "address"))
            topics = _log_entry_field(entry, "topics") or []
            if not addr or addr != want_addr or not topics:
                continue
            topic0 = _as_hex_str(topics[0])
            if topic0 != GAS_MEASURED_TOPIC0:
                continue
            data = _as_hex_str(_log_entry_field(entry, "data"))
            if not data or data == "0x":
                continue
            found = int(data, 16)
        except Exception:  # noqa: BLE001 - malformed row: skip, never raise
            continue
    return found


def _sim_offload_enabled() -> bool:
    """Kill-switch for running the synchronous scoreIntent simulation in a
    worker thread instead of inline on the asyncio event loop.

    A single scoreIntent sim is a multi-second, purely-synchronous block of
    web3 RPCs (``eth_call`` / ``evm_snapshot`` / ``evm_revert`` / mining). Run
    inline it freezes the API's single event loop for that whole window —
    starving ``/health``, quotes, and every other route (the 504s we saw under
    concurrent benchmark + frontend load). Offloading it to a thread keeps the
    loop responsive; the per-fork locks (see :meth:`AnvilSimulator.simulate`)
    preserve the exact same serialization and therefore byte-for-byte
    determinism WITHIN one simulation.

    DEFAULT OFF. This lands dark behind one remaining gate: ``SimulationRunner``
    (blockloop/simulation.py) seeds the fork (``_deal_erc20`` / allowances /
    platform fee) on the loop BEFORE calling ``simulate()``. Inline that seed is
    atomic with the sim it precedes (the loop never yields between them);
    offloaded, a concurrent flow's ``_reset_fork`` could wipe it mid-flight if
    both drive the same simulator instance. Before flipping this on in
    production, move that seeding inside the sim's locked window (pass the seed
    ops into ``simulate`` so ``_simulate_inner`` applies them AFTER its re-fork)
    — or prove no two flows ever share a simulator concurrently — and
    re-validate benchmark determinism against a live anvil. Set
    ``SIM_OFFLOAD_TO_THREAD=1`` to enable once that gate is cleared.
    """
    return (os.environ.get("SIM_OFFLOAD_TO_THREAD", "0") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


class AnvilSimulator:
    """Simulates execution plans on a running Anvil fork.

    Each simulation:
    1. Takes an EVM snapshot
    2. Impersonates the executor (vault/relayer/user)
    3. Funds the executor if needed
    4. Executes each interaction as a transaction
    5. Captures ERC-20 Transfer events and gas usage
    6. Reverts the snapshot (no lasting state change)

    Args:
        rpc_url: Anvil JSON-RPC endpoint (e.g., http://localhost:8545).
        default_executor: Address to execute from if not specified in plan.
        fund_executor: Whether to auto-fund the executor with ETH.
    """

    def __init__(
        self,
        rpc_url: str,
        default_executor: str = _DEFAULT_EXECUTOR,
        fund_executor: bool = True,
        sim_timeout: float = 30.0,
        upstream_rpc_url: str | None = None,
    ) -> None:
        self.rpc_url = rpc_url
        self.default_executor = Web3.to_checksum_address(default_executor)
        self.fund_executor = fund_executor
        self.sim_timeout = sim_timeout
        # Upstream RPC the local Anvil is forking from (e.g. Alchemy
        # Base mainnet). Used by _reset_fork to advance the fork to
        # the current upstream head before each simulation. Without
        # this, anvil_reset({}) silently no-ops back to the original
        # fork-block (a foundry quirk) and simulations run against
        # stale state. Optional — local-testnet sims (chain 31337,
        # not forked from anything) leave this unset and skip the
        # head-fetch path entirely.
        self.upstream_rpc_url = (upstream_rpc_url or "").strip() or None
        # SOCKET TIMEOUT (load-bearing): without request_kwargs the HTTPProvider
        # inherits requests' default timeout of None = INFINITE socket wait. Every
        # sim RPC below (eth_call / get_balance / make_request('anvil_reset'|
        # 'evm_*') / block_number) runs SYNCHRONOUSLY inside _simulate_inner while
        # holding _sim_lock, so a single wedged RPC freezes the whole event loop —
        # which starves BOTH the TOTAL_BENCHMARK_TIMEOUT check AND the round's
        # certification-deadline abort coroutine (they can't run on a blocked loop).
        # That is the confirmed cause of the 159-min round-e29746399 stall (aborted
        # +75 epochs past its own deadline) and the current-era "stale incumbent bar
        # (re-benchmark failed)" aborts. A bounded socket timeout converts an
        # unbounded freeze into a per-call exception the existing except-handlers
        # turn into a deterministic best-effort/zero result, so the round advances
        # on time. sim_timeout defaults 30s (generous: normal anvil calls are
        # sub-second, upstream anvil_reset a few seconds — only a genuine wedge
        # hits it), which is why this is behaviour-preserving in normal operation.
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": sim_timeout}))

        # Baseline snapshot taken immediately after the first connect.
        # Used by _reset_fork on no-upstream paths (local-testnet chain
        # 31337) where re-forking isn't possible — revert to baseline
        # instead. Also a recovery anchor if a per-simulation revert
        # fails. Tracked + refreshed inside _reset_fork.
        # Serializes simulate() calls on this fork. Concurrent sims (e.g. the
        # benchmark worker + a live quote hitting the same chain's simulator)
        # interleave evm_snapshot/evm_revert IDs — Anvil consumes a snapshot on
        # revert, so one sim's revert invalidates another's baseline → "baseline
        # revert failed" / fork-poison false positives. One lock per fork makes
        # the snapshot→execute→revert window atomic.
        self._sim_lock = asyncio.Lock()
        # With SIM_OFFLOAD_TO_THREAD (default on) the sim body runs in a worker
        # thread, so it can now execute CONCURRENTLY with loop-side code that
        # also touches this fork. Two threading locks bridge that boundary
        # (an asyncio.Lock can't be held across a thread):
        #   * _fork_mutation_lock — held by the offloaded sim body AND by the
        #     synchronous fork-mutators (pin_read_fork / simulate_with_trace) so
        #     their evm_snapshot/reset/revert can't interleave a sim's and
        #     corrupt its snapshot chain (the same hazard _sim_lock guards on
        #     the loop, extended across the thread boundary).
        #   * _anchor_lock — a micro-lock over the fork-anchor cache so the
        #     hot loop-side readers (get_block_timestamp / current_fork_block)
        #     never observe a torn write from _refresh_fork_anchor, which now
        #     runs inside the sim thread. Held only for in-memory reads/writes,
        #     never across an RPC, so it never stalls the loop.
        self._fork_mutation_lock = threading.Lock()
        self._anchor_lock = threading.Lock()
        self._baseline_snapshot_id: str | None = None

        # Per-process counter for the periodic baseline-alive probe.
        # See module docstring for the cost/safety trade-off.
        self._sim_count: int = 0

        # Recorded value of the probe slot at baseline-snapshot time.
        # If a later read disagrees, the fork has been mutated outside
        # our snapshot/revert window — force a re-fork or raise.
        self._baseline_probe_value: bytes | None = None

        # Fork anchor cache (benchmark determinism). Refreshed by _reset_fork
        # on every path: the current fork block's number + timestamp. The
        # timestamp drives the per-block sim pin (_pin_next_block_timestamp)
        # and the benchmark order deadline (via get_block_timestamp).
        # _block_ts_cache memoizes header timestamps by block number —
        # immutable data, so entries never go stale.
        self._fork_block_number: int | None = None
        self._fork_block_timestamp: int | None = None
        self._block_ts_cache: dict[int, int] = {}

        if not self.w3.is_connected():
            logger.warning("Anvil not reachable at %s", rpc_url)
        else:
            block = self.w3.eth.block_number
            logger.info(
                "AnvilSimulator connected: %s (block %d, upstream=%s)",
                rpc_url, block,
                "configured" if self.upstream_rpc_url else "none (fork stays static)",
            )
            # Best-effort baseline snapshot. If anvil isn't quite ready,
            # we'll lazily take it on the first _reset_fork() call.
            try:
                self._baseline_snapshot_id = self._snapshot()
                self._baseline_probe_value = self._read_probe_slot()
                self._refresh_fork_anchor()
                logger.info(
                    "AnvilSimulator baseline snapshot=%s probe=%s",
                    self._baseline_snapshot_id,
                    self._baseline_probe_value.hex()[:16] if self._baseline_probe_value else "?",
                )
            except Exception as exc:
                logger.warning(
                    "Could not take baseline snapshot at init (%s) — will retry lazily",
                    exc,
                )

    def _get_sim_lock(self) -> asyncio.Lock:
        """The per-fork asyncio lock, lazy-initialised so objects built via
        ``__new__`` (some tests / partial-construction paths that skip
        ``__init__``) still serialize. Safe under asyncio: no await between the
        check and the assignment.
        """
        lock = getattr(self, "_sim_lock", None)
        if lock is None:
            lock = self._sim_lock = asyncio.Lock()
        return lock

    def _get_fork_mutation_lock(self) -> threading.Lock:
        """Threading lock excluding synchronous fork-mutators from an offloaded
        sim body (see ``_fork_mutation_lock`` in ``__init__``). Lazy-init mirrors
        :meth:`_get_sim_lock`; the assignment is atomic under the GIL and these
        objects are single-threaded until a sim is offloaded.
        """
        lock = getattr(self, "_fork_mutation_lock", None)
        if lock is None:
            lock = self._fork_mutation_lock = threading.Lock()
        return lock

    def _get_anchor_lock(self) -> threading.Lock:
        """Micro-lock over the fork-anchor cache (see ``_anchor_lock`` in
        ``__init__``). Lazy-init mirrors :meth:`_get_fork_mutation_lock`.
        """
        lock = getattr(self, "_anchor_lock", None)
        if lock is None:
            lock = self._anchor_lock = threading.Lock()
        return lock

    async def simulate(self, *args: Any, **kwargs: Any) -> SimulationResult:
        """Serialized entrypoint — see :meth:`_simulate_inner`.

        Holds the per-fork asyncio lock for the whole snapshot→execute→revert
        window so concurrent callers can't corrupt each other's snapshot state.
        Under ``SIM_OFFLOAD_TO_THREAD`` (default OFF — see
        :func:`_sim_offload_enabled` for the enabling gate) the synchronous body
        runs in a worker thread so it can't freeze the event loop; the asyncio
        lock is only ever acquired/released ON the loop (never inside the
        thread), and the thread additionally holds ``_fork_mutation_lock`` so no
        loop-side fork-mutator can interleave. Serialization — and therefore
        byte-for-byte determinism — is identical to the inline path.
        """
        async with self._get_sim_lock():
            if _sim_offload_enabled():
                return await asyncio.to_thread(
                    self._simulate_inner_locked, *args, **kwargs,
                )
            return self._simulate_inner_locked(*args, **kwargs)

    def _simulate_inner_locked(self, *args: Any, **kwargs: Any) -> SimulationResult:
        """Run the sim body under ``_fork_mutation_lock``.

        The outer :meth:`simulate` already holds ``_sim_lock`` (so two sims
        never reach here at once); this inner lock additionally excludes the
        synchronous loop-side fork-mutators (:meth:`pin_read_fork`,
        :meth:`simulate_with_trace`), which can now run concurrently with this
        thread. Lock order is always _sim_lock → _fork_mutation_lock and the
        mutators take only _fork_mutation_lock, so there is no cycle.
        """
        with self._get_fork_mutation_lock():
            return self._simulate_inner(*args, **kwargs)

    def _simulate_inner(
        self,
        plan: ExecutionPlan,
        contract_address: str | None = None,
        intent_order: dict | None = None,
        token_balances: dict[str, int] | None = None,
        fork_block: int | None = None,
        *,
        meter_gas: bool = False,
        pin_only: bool = False,
    ) -> SimulationResult:
        """Execute a plan against the Anvil fork and return results.

        The plan's interactions are executed sequentially. All ERC-20
        Transfer events are captured from transaction receipts.

        The executor address comes from plan.metadata["executor"],
        falling back to the default executor.

        Args:
            plan: The execution plan to simulate.
            contract_address: Optional app contract for on-chain scoring.
            intent_order: Optional order dict for on-chain scoring.
            token_balances: Optional {token_address: amount_wei} to seed
                the executor with ERC-20 balances before simulation.
                Used by quote to ensure the executor has input tokens.
            fork_block: Optional historical block number. When set, the
                anvil fork rewinds to this block BEFORE simulating — used
                for Stage-2 historical-order replays so pool prices
                match the state at which the original order was filled.
                Default None = reset to upstream latest.
            meter_gas: BENCHMARK-ONLY. When True, the scoreIntent path
                additionally runs the GasMeter probe (a snapshot-bracketed
                side tx) and populates ``SimulationResult.gas_metered``
                with the PRE-REFUND inner-call gas. The default False keeps
                the direct-send path byte-identical to today — the live
                rail (order processing / fee certification) never sets it.
        """
        # SIM-10: Graceful fallback when Anvil is unavailable
        if not self.is_connected():
            logger.warning("Anvil unreachable at %s — returning failed simulation", self.rpc_url)
            return SimulationResult(
                success=False,
                gas_used=0,
                on_chain_score=None,
                error="Anvil unavailable",
            )

        # PR-7: cheap periodic probe of a known-stable storage slot. Catches
        # out-of-band state mutation. With the anvils now network-isolated from
        # the solver sandbox, the realistic source is an in-process state change
        # whose revert silently failed (not a direct external attacker). Raises
        # SimulatorStateError on poisoning evidence with no upstream to re-fork
        # from; surfaces as a failed simulation rather than a silently wrong score.
        try:
            self._assert_baseline_alive()
        except SimulatorStateError as exc:
            logger.error("Refusing to simulate on poisoned fork: %s", exc)
            return SimulationResult(
                success=False,
                gas_used=0,
                on_chain_score=None,
                error=f"fork-poisoning detected: {exc}",
            )

        # Re-fork at upstream head (or at fork_block for historical
        # replays) so each simulation sees the right pool state.
        # On no-upstream chains (local-testnet 31337) this reverts to
        # the baseline snapshot instead, undoing any state mutation
        # from a prior simulation whose own per-sim revert failed.
        # pin_only (QUOTE PATH) may reuse the fork in place — see
        # _reset_fork_for_sim.
        try:
            self._reset_fork_for_sim(fork_block, pin_only)
        except SimulatorStateError as exc:
            logger.error("Refusing to simulate; baseline revert failed: %s", exc)
            return SimulationResult(
                success=False,
                gas_used=0,
                on_chain_score=None,
                error=f"baseline revert failed: {exc}",
            )

        executor = plan.metadata.get("executor", self.default_executor)
        executor = Web3.to_checksum_address(executor)

        snap_id = self._snapshot()
        try:
            # ── Primary path: scoreIntent via contract ────────────────────
            # When we have the app contract and intent order, call scoreIntent
            # as a real transaction (impersonating the relayer). This mirrors
            # the actual executeIntent flow: the contract deploys a proxy,
            # pulls user tokens (which must be approved), executes plan calls
            # via the proxy, checks invariants, and returns a score.
            # Transfer events come from the receipt.
            if contract_address and intent_order:
                ip = intent_order.get('intent_params', '')
                ip_preview = ip[:40] if isinstance(ip, str) else ip[:20].hex() if isinstance(ip, bytes) else str(ip)[:40]
                print(f"[SIM] scoreIntent path: contract={contract_address[:10]}... user={intent_order.get('submitted_by','?')[:10]}... intent_params_len={len(ip) if isinstance(ip, (str,bytes)) else '?'} preview={ip_preview}", flush=True)
                result = self._simulate_via_score_intent(
                    contract_address, intent_order, plan, token_balances,
                    meter_gas=meter_gas,
                )
                if result is not None:
                    print(f"[SIM] scoreIntent result: success={result.success} gas={result.gas_used} transfers={len(result.token_transfers or [])} on_chain_score={result.on_chain_score}", flush=True)
                    return result
                # Fail closed. The manual-interaction fallback runs plan calls
                # directly from a funded executor, bypassing the contract's
                # proxy deploy / platform fee / invariant checks, which
                # inflates scores vs. real on-chain behavior and caused
                # validator divergence on ord_c88ce65d20764dee. The fallback
                # is only legitimate when no contract is provided (quotes).
                print("[SIM] scoreIntent reverted — fail closed (no fallback when contract provided)", flush=True)
                logger.warning("scoreIntent reverted — refusing to fall back to manual sim")
                return SimulationResult(
                    success=False,
                    gas_used=0,
                    error="scoreIntent simulation reverted",
                )

            # ── Fallback: manual interaction execution ────────────────────
            # Used when no contract is deployed (quotes, dry-runs). Runs plan
            # interactions one by one from the executor address.
            self._impersonate(executor)
            if self.fund_executor:
                self._fund(executor, 100 * 10**18)

            # Deal ERC-20 token balances to executor (for quotes / dry-runs)
            if token_balances:
                for token_addr, amount in token_balances.items():
                    ok = self._deal_erc20(token_addr, executor, amount)
                    if not ok:
                        logger.warning(
                            "Token deal failed: %s → %s (amount=%s). "
                            "Simulation may revert due to insufficient balance.",
                            token_addr, executor, amount,
                        )

            total_gas = 0
            all_transfers: list[TokenTransfer] = []
            state_changes: list[dict[str, Any]] = []

            eth_before = self.w3.eth.get_balance(executor)

            for i, ix in enumerate(plan.interactions):
                try:
                    receipt = self._execute_interaction(ix, executor)
                    total_gas += receipt["gasUsed"]
                    transfers = self._parse_transfer_events(receipt)
                    all_transfers.extend(transfers)
                    logger.debug(
                        "Interaction %d/%d: target=%s gas=%d transfers=%d",
                        i + 1,
                        len(plan.interactions),
                        ix.target,
                        receipt["gasUsed"],
                        len(transfers),
                    )
                except Exception as exc:
                    logger.warning(
                        "Interaction %d/%d failed: %s", i + 1, len(plan.interactions), exc
                    )
                    return SimulationResult(
                        success=False,
                        gas_used=total_gas,
                        error=f"Interaction {i + 1} failed: {exc}",
                        token_transfers=all_transfers,
                    )

            eth_after = self.w3.eth.get_balance(executor)
            eth_delta = eth_before - eth_after
            state_changes.append({
                "type": "balance_change",
                "address": executor,
                "token": "ETH",
                "delta": str(eth_delta),
            })

            return SimulationResult(
                success=True,
                gas_used=total_gas,
                token_transfers=all_transfers,
                state_changes=state_changes,
            )

        except Exception as exc:
            logger.error("Simulation error: %s", exc, exc_info=True)
            return SimulationResult(
                success=False,
                gas_used=0,
                error=str(exc),
            )
        finally:
            # PR-7: per-simulation revert. The snapshot taken at the start
            # of this call MUST be reverted before any other simulation
            # sees state — that's the boundary against in-sim cheat-code
            # state mutation by a malicious solver. If revert fails, clear
            # the baseline so the next _reset_fork() call re-takes a fresh
            # one (and surfaces a SimulatorStateError on no-upstream chains).
            try:
                reverted = self._evm_revert(snap_id)
                if not reverted:
                    logger.warning(
                        "evm_revert failed after simulate (snap=%s, rpc=%s) — "
                        "fork may be poisoned; forcing baseline re-take next call",
                        snap_id, self.rpc_url,
                    )
                    self._baseline_snapshot_id = None
            except Exception as exc:
                logger.warning(
                    "evm_revert raised after simulate (snap=%s): %s",
                    snap_id, exc,
                )
                self._baseline_snapshot_id = None
            self._stop_impersonating(executor)

    def _simulate_via_score_intent(
        self,
        contract_address: str,
        intent_order: dict,
        plan: ExecutionPlan,
        token_balances: dict[str, int] | None = None,
        *,
        meter_gas: bool = False,
    ) -> SimulationResult | None:
        """Call scoreIntent as a real transaction to simulate the full flow.

        Impersonates the contract's relayer, sends scoreIntent(order, plan)
        as a transaction, and captures Transfer events + gas from the receipt.
        This mirrors executeIntent exactly: proxy deploy, token pull, plan
        execution, invariant check, score return.

        With ``meter_gas=True`` (benchmark path ONLY), a GasMeter probe runs
        first inside its own snapshot/revert bracket — see
        :meth:`_measure_gas_via_meter` — populating ``gas_metered`` on the
        result. The direct send below is unaffected in either mode:
        ``gas_used`` keeps its receipt semantics.

        Returns SimulationResult on success, None if setup fails (caller
        should fall back to manual interaction execution).
        """
        try:
            from eth_abi import encode as abi_encode, decode as abi_decode
            from eth_hash.auto import keccak

            target = Web3.to_checksum_address(contract_address)

            # Resolve the contract's relayer address
            relayer_sig = keccak(b"relayer()")[:4]
            relayer_result = self.w3.eth.call({
                "to": target,
                "data": "0x" + relayer_sig.hex(),
            })
            relayer_addr = Web3.to_checksum_address(
                "0x" + relayer_result[-20:].hex()
            )

            # Impersonate relayer and fund with ETH for gas
            print(f"[SIM] impersonating relayer {relayer_addr}", flush=True)
            self._impersonate(relayer_addr)
            self._fund(relayer_addr, 100 * 10**18)
            # Verify impersonation works
            try:
                self._pin_next_block_timestamp()
                test_tx = self.w3.eth.send_transaction({"from": relayer_addr, "to": relayer_addr, "value": 0, "gas": 21000})
                print(f"[SIM] impersonation verified: {test_tx.hex()[:16]}...", flush=True)
            except Exception as imp_err:
                print(f"[SIM] impersonation FAILED: {imp_err}", flush=True)

            # Seed user tokens if needed (for scenarios where fork state is stale)
            if token_balances:
                submitted_by = intent_order.get("submitted_by", "")
                if submitted_by:
                    for tok, amt in token_balances.items():
                        self._deal_erc20(tok, submitted_by, amt)
                        self._set_erc20_allowance(tok, submitted_by, target, 2**256 - 1)
                    # Re-impersonate relayer — _set_erc20_allowance may have
                    # stopped impersonating if submitted_by == relayer
                    self._impersonate(relayer_addr)

            # Fund the app's fee paymaster so APP-mode protocol-fee settlement
            # can pull WETH (see _fund_app_paymaster). No-op for non-fee apps.
            self._fund_app_paymaster(target, relayer_addr)

            # Build scoreIntent calldata
            sig = "scoreIntent((bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256),((address,uint256,bytes)[],uint256,uint256,bytes))"
            selector = keccak(sig.encode())[:4]

            order_id = intent_order.get("order_id", b"\x00" * 32)
            if isinstance(order_id, str):
                # Order IDs like "ord_abc123" aren't hex — hash them to bytes32
                try:
                    order_id = bytes.fromhex(order_id.replace("0x", "").ljust(64, "0"))[:32]
                except ValueError:
                    order_id = keccak(order_id.encode())

            app_addr = intent_order.get("app", contract_address)
            intent_sel = intent_order.get("intent_selector", b"\x00" * 4)
            if isinstance(intent_sel, str):
                intent_sel = bytes.fromhex(intent_sel.replace("0x", ""))[:4]

            intent_params = intent_order.get("intent_params", b"")
            if isinstance(intent_params, str):
                if intent_params.startswith("0x"):
                    intent_params = bytes.fromhex(intent_params[2:])
                else:
                    intent_params = bytes.fromhex(intent_params) if all(c in '0123456789abcdefABCDEF' for c in intent_params) else intent_params.encode()

            submitted_by = intent_order.get("submitted_by", "0x" + "00" * 20)
            chain_id = intent_order.get("chain_id", 1)
            deadline = intent_order.get("deadline", 0)
            nonce = intent_order.get("nonce", 0)
            perpetual = intent_order.get("perpetual", False)
            max_executions = intent_order.get("max_executions", 1)
            cooldown = intent_order.get("cooldown", 0)

            # Build ExecutionPlan calls
            calls = []
            for ix in plan.interactions:
                cd = ix.call_data
                if isinstance(cd, str):
                    cd = bytes.fromhex(cd[2:] if cd.startswith("0x") else cd) if cd else b""
                calls.append((
                    Web3.to_checksum_address(ix.target),
                    int(ix.value) if ix.value else 0,
                    cd,
                ))

            plan_metadata = b""
            if plan.metadata:
                import json as _json
                plan_metadata = _json.dumps(plan.metadata).encode()

            encoded = abi_encode(
                [
                    "(bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256)",
                    "((address,uint256,bytes)[],uint256,uint256,bytes)",
                ],
                [
                    (
                        order_id,
                        Web3.to_checksum_address(app_addr),
                        intent_sel,
                        intent_params,
                        Web3.to_checksum_address(submitted_by),
                        chain_id,
                        deadline,
                        nonce,
                        perpetual,
                        max_executions,
                        cooldown,
                    ),
                    (calls, plan.deadline, plan.nonce, plan_metadata),
                ],
            )

            calldata = "0x" + (selector + encoded).hex()

            # For native ETH input (user submits with msg.value), the contract
            # expects msg.value > 0 to trigger the wrap path in _fundAndExecute.
            # Without this, the ERC-20 safeTransferFrom path runs and reverts.
            tx_value = 0
            if intent_order and intent_order.get("_input_token_is_native"):
                try:
                    tx_value = int(intent_order.get("_input_amount", 0))
                except (ValueError, TypeError):
                    pass

            # Capture the on-chain score via eth_call against the FUNDED pre-tx
            # state. scoreIntent returns (uint256 score, bool valid), but the
            # transaction below CONSUMES the funding (safeTransferFrom drains the
            # input tokens), so reading the return value AFTER the tx reverts and
            # yields None. eth_call doesn't persist state, so the tx still runs.
            on_chain_score = None
            try:
                _score_call = {"from": relayer_addr, "to": target,
                               "data": calldata, "gas": 2_000_000}
                if tx_value > 0:
                    _score_call["value"] = tx_value
                _ret = self.w3.eth.call(_score_call)
                _score_val, _valid = abi_decode(["uint256", "bool"], _ret)
                on_chain_score = _score_val if _valid else None
            except Exception as _score_exc:
                logger.warning("scoreIntent on-chain score read failed: %s", _score_exc)

            # BENCHMARK-ONLY GasMeter probe: measure PRE-REFUND inner-call
            # gas by replaying the SAME calldata through the meter inside a
            # snapshot/revert bracket. Runs after the impersonation
            # self-check and the pre-tx on_chain_score eth_call, before the
            # direct send — which then sees byte-identical state.
            gas_metered: int | None = None
            if meter_gas:
                gas_metered = self._measure_gas_via_meter(
                    target, relayer_addr, calldata, tx_value,
                )
                print(f"[SIM] gas-meter probe: gas_metered={gas_metered}", flush=True)

            # Send as a raw RPC call (bypasses Web3.py's signer middleware)
            tx_params = {
                "from": relayer_addr,
                "to": target,
                "data": calldata,
                "gas": hex(2_000_000),
            }
            if tx_value > 0:
                tx_params["value"] = hex(tx_value)
            # Deterministic block.timestamp for the scoreIntent tx itself —
            # the block anvil mines for this send lands on the pinned second.
            self._pin_next_block_timestamp()
            raw_result = self.w3.provider.make_request("eth_sendTransaction", [tx_params])
            tx_hash_hex = raw_result.get("result", "")
            if not tx_hash_hex:
                print(f"[SIM] scoreIntent send_tx failed: {raw_result}", flush=True)
                return None
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash_hex, timeout=30)

            if receipt["status"] == 0:
                # Decode WHY it reverted so the miner can see it (benchmark
                # report + dry-run surface this). Prefer Anvil's
                # debug_traceTransaction — it decodes Error(string)/Panic/known
                # custom errors from the revert payload — and fall back to an
                # eth_call replay string when the trace has no revert data.
                revert_reason = ""
                try:
                    revert_reason = extract_revert_via_trace(self.w3, tx_hash_hex)
                except Exception:
                    revert_reason = ""
                if not revert_reason:
                    try:
                        self.w3.eth.call({
                            "from": relayer_addr,
                            "to": target,
                            "data": calldata,
                            "gas": 2_000_000,
                        })
                        revert_reason = "reverted (no revert data)"
                    except Exception as revert_exc:
                        revert_reason = str(revert_exc)
                print(f"[SIM] scoreIntent REVERTED: {revert_reason}", flush=True)
                # Carry the reason up instead of discarding it. Fail-closed
                # semantics are unchanged (success=False), exactly like the
                # generic path simulate() used to build from a None return.
                return SimulationResult(
                    success=False,
                    gas_used=receipt.get("gasUsed", 0),
                    error=f"scoreIntent reverted: {revert_reason}",
                    revert_reason=revert_reason,
                )

            # Parse transfer events and gas
            all_transfers = self._parse_transfer_events(receipt)
            total_gas = receipt["gasUsed"]

            # on_chain_score was captured pre-tx (above) — reading it here, after
            # the tx drained the funding, would revert and yield None.

            logger.info(
                "scoreIntent simulation: gas=%d transfers=%d on_chain_score=%s",
                total_gas, len(all_transfers), on_chain_score,
            )

            return SimulationResult(
                success=True,
                gas_used=total_gas,
                token_transfers=all_transfers,
                on_chain_score=on_chain_score,
                gas_metered=gas_metered,
            )

        except Exception as exc:
            import traceback
            print(f"[SIM] scoreIntent exception: {exc}", flush=True)
            traceback.print_exc()
            logger.warning("scoreIntent simulation failed: %s", exc)
            return None

    def _measure_gas_via_meter(
        self,
        target: str,
        relayer_addr: str,
        calldata: str,
        tx_value: int,
    ) -> int | None:
        """BENCHMARK-ONLY: measure PRE-REFUND scoreIntent gas via the GasMeter.

        Exact sequence (spike-proven, 36/36 on anvil 1.5.1), all inside its
        own evm_snapshot/evm_revert bracket so the direct send that follows
        sees byte-identical state:

          1. impersonate the fixed code-less :data:`GAS_METER_SENDER_EOA`
             (≠ relayer, so EIP-3607 can never apply) + setBalance 100 ETH;
          2. ``anvil_setCode`` the meter runtime AT the relayer address;
          3. ``anvil_setStorageAt`` slot 0 = the app address (scoreIntent
             target the meter forwards to);
          4. ``eth_sendTransaction`` from the sender EOA TO the relayer with
             the SAME calldata (+ the order's native value when present),
             gas :data:`GAS_METER_TX_GAS`;
          5. parse ``GasMeasured`` (topic0 :data:`GAS_MEASURED_TOPIC0`, log
             address == relayer) from the receipt → the metered int; absent
             (i.e. the inner call reverted) → None;
          6. ``anvil_setCode`` the relayer back to "0x" (belt-and-braces;
             the bracket's evm_revert restores code + storage anyway).

        Measurement only: returns None on any failure, never raises, never
        influences the simulation result beyond ``gas_metered``.
        """
        if relayer_addr.lower() == GAS_METER_SENDER_EOA.lower():
            # Astronomically unlikely, but the meter model requires a sender
            # distinct from the meter's install address.
            logger.warning(
                "[gas-meter] relayer == meter sender EOA (%s) — skipping",
                relayer_addr,
            )
            return None
        snap_id: str | None = None
        try:
            snap_id = self._snapshot()
            self._impersonate(GAS_METER_SENDER_EOA)
            self._fund(GAS_METER_SENDER_EOA, 100 * 10**18)
            self.w3.provider.make_request(
                "anvil_setCode", [relayer_addr, GAS_METER_RUNTIME_HEX],
            )
            self.w3.provider.make_request(
                "anvil_setStorageAt",
                [
                    relayer_addr,
                    "0x0",
                    "0x" + "00" * 12 + target.lower().replace("0x", ""),
                ],
            )
            tx_params = {
                "from": GAS_METER_SENDER_EOA,
                "to": relayer_addr,
                "data": calldata,
                "gas": hex(GAS_METER_TX_GAS),
            }
            if tx_value > 0:
                tx_params["value"] = hex(tx_value)
            # The metered block is pinned like every other sim block (the pin
            # is one-shot; the direct-send site re-pins for its own block).
            self._pin_next_block_timestamp()
            raw = self.w3.provider.make_request(
                "eth_sendTransaction", [tx_params],
            )
            tx_hash = raw.get("result", "")
            if not tx_hash:
                logger.warning("[gas-meter] metered send rejected: %s", raw)
                return None
            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=30,
            )
            return parse_gas_measured(receipt.get("logs"), relayer_addr)
        except Exception as exc:  # noqa: BLE001 - measurement only, never abort the sim
            logger.warning("[gas-meter] metering failed (probe skipped): %s", exc)
            return None
        finally:
            # Uninstall the meter, then revert the bracket: the direct send
            # must run against the exact pre-probe state (funding, nonces,
            # block height, relayer code+storage).
            try:
                self.w3.provider.make_request(
                    "anvil_setCode", [relayer_addr, "0x"],
                )
            except Exception:  # noqa: BLE001
                pass
            if snap_id is not None:
                try:
                    if not self._evm_revert(snap_id):
                        logger.warning(
                            "[gas-meter] bracket revert failed (snap=%s) — "
                            "forcing baseline re-take next call", snap_id,
                        )
                        self._baseline_snapshot_id = None
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[gas-meter] bracket revert raised (snap=%s): %s",
                        snap_id, exc,
                    )
                    self._baseline_snapshot_id = None

    def pin_read_fork(self, chain_id: int, block_number: int) -> bool:
        """Pin this fork to ``block_number`` for the SOLVER's reads.

        The solver currently quotes/routes against the fork at live HEAD while
        the simulator SCORES at ``fork_block`` — a block mismatch that (a) makes
        the solver's RPC call sequence host-dependent (non-deterministic across
        validators) and (b) misprices its quotes relative to the executed block
        (a likely source of "Too little received" reverts). Resetting the read
        fork to the round's pinned block makes the solver read exactly the state
        it is scored against, identically on every validator. No-op when already
        at the block (the prior scenario's simulate leaves the fork pinned, so we
        avoid a redundant, expensive re-fork). ``chain_id`` is accepted for a
        uniform interface with :class:`MultiChainSimulator` and ignored here
        (this is a single fork). Returns True iff a re-fork happened.

        Kept synchronous, but it re-forks (an evm mutation), so it takes
        ``_fork_mutation_lock`` to exclude a concurrently-offloaded sim body —
        otherwise the ``anvil_reset`` here could interleave that sim's
        snapshot/revert bracket and corrupt its result. This is dormant today
        (the caller is gated behind PIN_SOLVER_READ_BLOCK, off), so the lock is
        uncontended; NOTE for whoever flips that flag with SIM_OFFLOAD_TO_THREAD
        on: this acquire is synchronous, so a collision with an in-flight sim
        would briefly block the loop — make this path async first.
        """
        with self._get_fork_mutation_lock():
            try:
                if int(self.w3.eth.block_number) == int(block_number):
                    return False
            except Exception:  # noqa: BLE001 - fall through to a reset on any read error
                pass
            self._reset_fork(block_number=int(block_number))
            return True

    def current_fork_block(self, chain_id: int | None = None) -> int | None:
        """The block this fork is currently anchored at (best-effort), so the
        quote path can pin subsequent sims to a stable, cache-warm block instead
        of chasing upstream head on every call. ``chain_id`` is accepted for a
        uniform interface with :class:`MultiChainSimulator` and ignored here
        (single fork). None when unknown → caller falls back to a head re-fork.

        Guarded by ``_anchor_lock`` so it never reads ``_fork_block_number``
        mid-write from an offloaded sim's :meth:`_refresh_fork_anchor`.
        """
        with self._get_anchor_lock():
            return getattr(self, "_fork_block_number", None)

    def _reset_fork_for_sim(self, fork_block: int | None, pin_only: bool) -> None:
        """Prepare the fork for one simulation.

        Default (scoring / order-processing): a full re-fork to ``fork_block``
        (or upstream head when None) — unchanged, deterministic behaviour.

        ``pin_only`` (QUOTE PATH ONLY): the caller has already pinned this fork
        to ``fork_block`` and only needs a fresh pool read there. Because the
        snapshot→execute→revert bracket in :meth:`_simulate_inner` — NOT the
        re-fork — is what isolates one sim's mutations from the next, the fork
        can be REUSED when it is already at ``fork_block``: we skip a redundant,
        upstream-hitting ``anvil_reset`` and keep every touched slot warm in the
        fork-cache. A re-fork still happens on a block mismatch (e.g. an order
        sim moved the fork) so the read is never on the wrong block. Scoring and
        order-processing never pass ``pin_only``, so their per-sim re-fork is
        byte-for-byte unchanged.

        Raises SimulatorStateError on a baseline-revert failure (no-upstream path).
        """
        if pin_only and fork_block is not None:
            try:
                if int(self.w3.eth.block_number) == int(fork_block):
                    return  # already pinned here — reuse the warm fork
            except Exception:  # noqa: BLE001 - any read error → take the safe re-fork
                pass
        self._reset_fork(block_number=fork_block)

    def _reset_fork(self, block_number: int | None = None) -> None:
        """Reset the fork (see :meth:`_reset_fork_inner`) + refresh the fork
        anchor cache (block number/timestamp) on EVERY exit path — including
        the early-return failure paths, where the fork stays at its previous
        block and the cache must describe THAT block, not the requested one.
        """
        try:
            self._reset_fork_inner(block_number=block_number)
        finally:
            self._refresh_fork_anchor()

    def _refresh_fork_anchor(self) -> None:
        """Cache the current fork block's number + timestamp (best-effort).

        One local ``eth_getBlockByNumber("latest")`` against the anvil — the
        header is always in memory post-reset, no upstream fetch. On failure
        the cache clears, which disables the timestamp pin (floating-time
        behavior, exactly the pre-pin world) rather than pinning to a stale
        anchor.
        """
        # The eth_getBlockByNumber RPC is done OUTSIDE _anchor_lock so the lock
        # only ever wraps the in-memory writes below (microseconds) — it must
        # never be held across an RPC or it would stall the loop-side readers
        # (get_block_timestamp / current_fork_block).
        try:
            block = self.w3.eth.get_block("latest")
            num: int | None = int(block["number"])
            ts: int | None = int(block["timestamp"])
        except Exception as exc:  # noqa: BLE001 - cache is best-effort by design
            logger.warning("fork anchor refresh failed (%s): %s", self.rpc_url, exc)
            num = None
            ts = None
        # Publish the new anchor atomically w.r.t. the loop-side readers. Lazy-
        # init keeps objects built via __new__ (some tests / partial
        # construction paths) working — mirrors the _sim_lock discipline.
        with self._get_anchor_lock():
            if not isinstance(getattr(self, "_block_ts_cache", None), dict):
                self._block_ts_cache = {}
            self._fork_block_number = num
            self._fork_block_timestamp = ts
            if num is not None:
                if len(self._block_ts_cache) > 256:
                    self._block_ts_cache.clear()
                self._block_ts_cache[num] = ts

    def get_block_timestamp(
        self, chain_id: int | None = None, block_number: int | None = None,
    ) -> int | None:
        """Timestamp of ``block_number`` on this fork (``None`` = the current
        fork anchor). Serves from the fork-anchor / header caches when
        possible; otherwise one ``eth_getBlockByNumber`` against the anvil
        (which forwards upstream for a not-yet-pinned historical block).
        Returns None when unresolvable. ``chain_id`` is accepted for interface
        uniformity with :class:`MultiChainSimulator` and ignored here (single
        fork) — mirrors :meth:`pin_read_fork`.
        """
        # Snapshot the anchor + cache under _anchor_lock (a shallow copy of a
        # small int->int dict) so a concurrent _refresh_fork_anchor .clear()
        # from an offloaded sim can't turn our `in`-then-lookup into a KeyError
        # or a torn read. The upstream RPC below runs OUTSIDE the lock.
        with self._get_anchor_lock():
            anchor_ts = getattr(self, "_fork_block_timestamp", None)
            cache: dict[int, int] = dict(getattr(self, "_block_ts_cache", None) or {})
        if block_number is None:
            return anchor_ts
        block_number = int(block_number)
        if block_number in cache:
            return cache[block_number]
        try:
            ts = int(self.w3.eth.get_block(block_number)["timestamp"])
        except Exception as exc:  # noqa: BLE001 - callers fall back on None
            logger.warning(
                "get_block_timestamp(%s) failed (%s): %s",
                block_number, self.rpc_url, exc,
            )
            return None
        with self._get_anchor_lock():
            if isinstance(getattr(self, "_block_ts_cache", None), dict):
                if len(self._block_ts_cache) > 256:
                    self._block_ts_cache.clear()
                self._block_ts_cache[block_number] = ts
        return ts

    def _pin_next_block_timestamp(self) -> None:
        """Pin the NEXT mined block to the fork anchor's deterministic
        timestamp (``fork_ts + SIM_BLOCK_TIMESTAMP_OFFSET``).

        One-shot by anvil semantics (and discarded by ``evm_revert``), so
        every block-mining site inside the simulation paths calls this
        immediately before mining; equal-to-last pins are accepted, so all
        sim blocks land on the same pinned second. Best-effort: on any
        failure the block falls back to floating wall-clock time (the
        pre-pin behavior) and we log rather than abort the simulation.
        """
        ts = getattr(self, "_fork_block_timestamp", None)
        if ts is None:
            return
        pinned = int(ts) + SIM_BLOCK_TIMESTAMP_OFFSET
        try:
            result = self.w3.provider.make_request(
                "evm_setNextBlockTimestamp", [pinned],
            )
            if isinstance(result, dict) and result.get("error"):
                logger.warning(
                    "evm_setNextBlockTimestamp(%s) rejected (%s): %s",
                    pinned, self.rpc_url, result["error"],
                )
        except Exception as exc:  # noqa: BLE001 - pin is best-effort by design
            logger.warning(
                "evm_setNextBlockTimestamp(%s) failed (%s): %s",
                pinned, self.rpc_url, exc,
            )

    def _reset_fork_inner(self, block_number: int | None = None) -> None:
        """Reset the Anvil fork to a clean baseline.

        Two paths, dispatched by whether an upstream RPC is configured:

        1. **Upstream configured** (mainnet/testnet fork — Base, BT EVM
           in prod). Calls ``anvil_reset`` with an explicit
           ``forking.blockNumber``. When ``block_number`` is None we
           fetch the current upstream head; every current-state sim
           sees fresh pool prices + any newly-deployed contracts. When
           explicit, the fork rewinds to that block — used by
           historical-order replays. After reset, the prior snapshot
           ID is invalidated, so we take a fresh baseline.

        2. **No upstream** (local-testnet chain 31337, or any anvil
           started without ``--fork-url``). Re-forking isn't possible.
           Instead, ``evm_revert`` to the baseline snapshot taken at
           ``__init__`` time. Anvil consumes snapshot IDs on revert,
           so immediately take a new baseline. If we have no baseline
           yet (lazy-init path, or recovery after a prior revert
           failure), take one now and return.

        Subtle: ``anvil_reset`` with empty params ``[{}]`` is a no-op
        in Foundry — the fork stays at its initial block. The explicit
        ``forking.blockNumber`` is what actually advances the fork.
        That's why the upstream-head fetch is required.

        Raises:
            SimulatorStateError: when the no-upstream path tries to
                revert to baseline and the revert fails — anvil state
                may be poisoned and the operator must recycle the fork.
        """
        if self.upstream_rpc_url:
            # ── Upstream path: full re-fork at head (preserves old behavior) ──
            if block_number is None:
                try:
                    block_number = self._fetch_upstream_head()
                except Exception as exc:
                    logger.warning(
                        "Could not fetch upstream head for fork reset (upstream=%s): %s",
                        self.upstream_rpc_url, exc,
                    )
                    # Best-effort: leave fork at its current block. Better
                    # than a half-reset that leaves Anvil in an inconsistent
                    # state.
                    return

            try:
                params = [{"forking": {"blockNumber": int(block_number)}}]
                self.w3.provider.make_request("anvil_reset", params)
            except Exception as exc:
                logger.warning("anvil_reset failed (block=%s): %s", block_number, exc)
                return

            # After re-fork, the previous snapshot is invalidated. Take a
            # fresh baseline so subsequent no-upstream-style reverts (if
            # the simulator is reconfigured) and recovery paths still work.
            try:
                self._baseline_snapshot_id = self._snapshot()
                self._baseline_probe_value = self._read_probe_slot()
            except Exception as exc:
                logger.warning("post-reset baseline snapshot failed: %s", exc)
                self._baseline_snapshot_id = None
            return

        # ── No-upstream path: revert to baseline snapshot ─────────────
        # (local-testnet chain 31337; the historical "no-op" path that
        # the audit flagged as the C4 fork-poisoning vector.)
        if self._baseline_snapshot_id is None:
            # Lazy-init or post-failure recovery: just take a baseline now.
            try:
                self._baseline_snapshot_id = self._snapshot()
                self._baseline_probe_value = self._read_probe_slot()
            except Exception as exc:
                logger.warning("lazy baseline snapshot failed: %s", exc)
            return

        reverted = self._evm_revert(self._baseline_snapshot_id)
        if not reverted:
            # Recovery attempt: clear the dead ID and try once to take
            # a fresh snapshot at whatever state we're in. Then raise —
            # the caller should treat this as a hard failure.
            self._baseline_snapshot_id = None
            raise SimulatorStateError(
                "evm_revert to baseline snapshot failed — anvil state may be "
                "poisoned by a malicious solver via the unauthenticated "
                f"anvil_* JSON-RPC namespace (rpc={self.rpc_url}). Operator "
                "should recycle the anvil container; see "
                "docs/operator/anvil-isolation.md."
            )

        # Anvil consumes snapshot IDs on revert; take a fresh baseline.
        try:
            self._baseline_snapshot_id = self._snapshot()
            self._baseline_probe_value = self._read_probe_slot()
        except Exception as exc:
            logger.warning("post-revert baseline snapshot failed: %s", exc)
            self._baseline_snapshot_id = None

    def _evm_revert(self, snap_id: str) -> bool:
        """Revert to a snapshot. Returns True on success, False otherwise.

        Anvil's evm_revert returns `true` on success and `false` if the
        snapshot ID was already consumed or never existed. Treats any
        RPC error as a failed revert.
        """
        try:
            result = self.w3.provider.make_request("evm_revert", [snap_id])
            ok = result.get("result", False)
            if not ok:
                logger.warning(
                    "evm_revert(%s) returned %s — snapshot may be stale",
                    snap_id, result,
                )
            return bool(ok)
        except Exception as exc:
            logger.warning("evm_revert(%s) raised: %s", snap_id, exc)
            return False

    def _evm_snapshot(self) -> str:
        """Take an EVM state snapshot, returning the snapshot ID."""
        return self._snapshot()

    # ── Baseline-alive probe ─────────────────────────────────────────
    # Reads storage slot 0 of the zero address (cheap, stable, never
    # written to in any well-known protocol). If a later read disagrees
    # with the value captured at baseline time, the fork has been
    # mutated OUTSIDE our snapshot/revert window — now almost certainly a
    # state change inside a simulation whose revert silently failed without
    # raising (a direct cheat-code RPC from a solver is cut off at the network
    # layer — anvils are off the benchmark-sandbox net). Treat as poisoning.

    # A well-known stable slot: storage[0] of address(0). The zero
    # address has no code and no canonical mutator; we expect this
    # slot to be zero on every chain and stay zero forever. If you
    # ever see a non-zero value here, something is very wrong.
    _PROBE_ADDRESS = "0x0000000000000000000000000000000000000000"
    _PROBE_SLOT = "0x0000000000000000000000000000000000000000000000000000000000000000"

    def _read_probe_slot(self) -> bytes:
        """Read the probe storage slot. Cheap (one eth_call).

        Returns the raw bytes; caller compares to the baseline value
        captured at snapshot time.
        """
        try:
            val = self.w3.eth.get_storage_at(self._PROBE_ADDRESS, 0)
            return bytes(val) if val else b""
        except Exception as exc:
            logger.debug("probe slot read failed: %s", exc)
            return b""

    def _assert_baseline_alive(self) -> None:
        """Verify the fork hasn't been mutated outside our snapshot window.

        Runs once every BASELINE_PROBE_EVERY simulate() calls. If the
        probe slot disagrees with the recorded baseline value, force a
        re-fork (upstream available) or raise SimulatorStateError (no
        upstream — local-testnet, no recovery possible without operator
        intervention).
        """
        self._sim_count += 1
        if self._sim_count % BASELINE_PROBE_EVERY != 0:
            return
        if self._baseline_probe_value is None:
            # Never captured — best-effort capture now, nothing to compare.
            self._baseline_probe_value = self._read_probe_slot()
            return
        observed = self._read_probe_slot()
        if observed == self._baseline_probe_value:
            return

        logger.error(
            "Baseline probe mismatch on %s: expected=%s observed=%s — fork "
            "appears poisoned; forcing recovery",
            self.rpc_url,
            self._baseline_probe_value.hex()[:16],
            observed.hex()[:16],
        )
        if self.upstream_rpc_url:
            # Best chance of recovery: full re-fork at head.
            self._reset_fork(block_number=None)
            return
        raise SimulatorStateError(
            f"Baseline storage probe mismatch on {self.rpc_url} and no "
            "upstream RPC configured to re-fork. Anvil state is poisoned; "
            "operator must recycle the container. See "
            "docs/operator/anvil-isolation.md."
        )

    def _fetch_upstream_head(self) -> int:
        """Query the upstream RPC for the current head block number.

        Retries transient provider failures (429 / -32005 CU / 5xx / timeout /
        reset) with backoff — a single hiccup here otherwise fails the fork reset
        before a sim. Idempotent read (a head query), safe to retry."""
        if not self.upstream_rpc_url:
            raise RuntimeError("No upstream_rpc_url configured")

        def _once() -> "requests.Response":
            resp = requests.post(
                self.upstream_rpc_url,
                json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
                timeout=5,
            )
            resp.raise_for_status()
            return resp

        resp = retry_sync(
            _once,
            retry_on_result=lambda r: body_has_retryable_rpc_error(r.content),
            label="anvil-upstream-head",
        )
        result = resp.json().get("result")
        if not result:
            raise RuntimeError(f"Upstream RPC returned no result: {resp.text[:200]}")
        return int(result, 16)

    def _snapshot(self) -> str:
        """Take an EVM state snapshot."""
        result = self.w3.provider.make_request("evm_snapshot", [])
        snap_id = result.get("result", "0x0")
        logger.debug("Snapshot taken: %s", snap_id)
        return snap_id

    def _revert(self, snap_id: str) -> None:
        """Revert to a previous snapshot."""
        self.w3.provider.make_request("evm_revert", [snap_id])
        logger.debug("Reverted to snapshot: %s", snap_id)

    def _impersonate(self, address: str) -> None:
        """Impersonate an account on Anvil."""
        self.w3.provider.make_request("anvil_impersonateAccount", [address])

    def _stop_impersonating(self, address: str) -> None:
        """Stop impersonating an account."""
        try:
            self.w3.provider.make_request(
                "anvil_stopImpersonatingAccount", [address]
            )
        except Exception:
            pass  # Best-effort cleanup

    def _fund(self, address: str, amount_wei: int) -> None:
        """Fund an address with ETH via Anvil cheat code."""
        self.w3.provider.make_request(
            "anvil_setBalance", [address, hex(amount_wei)]
        )

    def _deal_erc20(self, token: str, to: str, amount: int) -> bool:
        """Set an ERC-20 token balance for an address via Anvil cheat code.

        Uses the standard ERC-20 balanceOf storage slot discovery:
        tries common mapping slots (0-10) by computing
        keccak256(abi.encode(address, slot)) and checking if the
        balance changes after writing.

        Returns True if the balance was successfully set, False otherwise.
        """
        token = Web3.to_checksum_address(token)
        to = Web3.to_checksum_address(to)

        # Read current balance via balanceOf(address)
        balance_of_sig = "0x70a08231" + to[2:].lower().zfill(64)
        try:
            current = self.w3.eth.call({"to": token, "data": balance_of_sig})
            current_balance = int.from_bytes(current, "big")
        except Exception:
            logger.warning(
                "Cannot read balanceOf(%s) for token %s — deal skipped", to, token,
            )
            return False

        # Try standard mapping slots 0-10
        amount_hex = hex(amount)[2:].zfill(64)
        to_padded = to[2:].lower().zfill(64)

        for slot in range(11):
            # Storage key = keccak256(abi.encode(address, uint256(slot)))
            slot_hex = hex(slot)[2:].zfill(64)
            key_input = bytes.fromhex(to_padded + slot_hex)
            from eth_hash.auto import keccak
            storage_key = "0x" + keccak(key_input).hex()

            self.w3.provider.make_request(
                "anvil_setStorageAt",
                [token, storage_key, "0x" + amount_hex],
            )

            # Verify it worked
            try:
                result = self.w3.eth.call({"to": token, "data": balance_of_sig})
                new_balance = int.from_bytes(result, "big")
                if new_balance == amount:
                    logger.info(
                        "Dealt %s of %s to %s (slot %d)", amount, token, to, slot
                    )
                    return True
            except Exception:
                pass

            # Revert this slot's write if it didn't work
            self.w3.provider.make_request(
                "anvil_setStorageAt",
                [token, storage_key, "0x" + hex(current_balance)[2:].zfill(64)],
            )

        logger.warning(
            "Could not find balanceOf slot for %s — deal failed "
            "(tried slots 0-10, amount=%s, to=%s)",
            token, amount, to,
        )
        return False

    def _set_erc20_allowance(
        self, token: str, owner: str, spender: str, amount: int,
    ) -> None:
        """Set ERC-20 allowance via Anvil impersonation + approve() call.

        Faster and more reliable than trying to find the allowance storage
        slot (which is a nested mapping: keccak(spender . keccak(owner . slot))).
        """
        token = Web3.to_checksum_address(token)
        owner = Web3.to_checksum_address(owner)
        spender = Web3.to_checksum_address(spender)

        try:
            self._impersonate(owner)
            self._fund(owner, 10**18)  # Need ETH for gas
            # approve(address spender, uint256 amount)
            approve_data = (
                "0x095ea7b3"
                + spender[2:].lower().zfill(64)
                + hex(amount)[2:].zfill(64)
            )
            self._pin_next_block_timestamp()
            tx_hash = self.w3.eth.send_transaction({
                "from": owner,
                "to": token,
                "data": approve_data,
                "gas": 100_000,
            })
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=10)
            self._stop_impersonating(owner)
        except Exception as exc:
            logger.warning("Set allowance failed: %s → %s for %s: %s", owner, spender, token, exc)
            try:
                self._stop_impersonating(owner)
            except Exception:
                pass

    def _fund_app_paymaster(self, target: str, relayer_addr: str) -> None:
        """Fund APP-mode protocol-fee settlement: V1 paymaster pull AND V2
        app-held float.

        In ``FeeMode.APP`` the protocol fee is settled in WETH. Where it comes
        from depends on the contract generation:

        - **V1 (AppIntentBase apps)**: pulled from the ``appPaymaster`` via
          ``IERC20(WETH).safeTransferFrom(appPaymaster, platformFeeCollector,
          fee)`` — needs the paymaster funded AND an allowance to THIS fork
          instance (any inherited allowance targets the LIVE app).
        - **V2 (AppIntentBaseV2 apps, e.g. DexAggregatorAppV2)**: paid via
          ``WETH.safeTransfer`` from a float held by the APP CONTRACT itself —
          no paymaster, no allowance. NOTE: V2 still exposes ``appPaymaster()``
          (informational "recommended source of funds"), so its presence does
          NOT identify the generation; fund both models unconditionally.

        On a fresh fork neither account holds WETH, so every order with a
        nonzero fee (``tokenOut != WETH`` on V1; ALL on V2) reverts with empty
        data (WETH9's message-less ``require``) at the fee step, scoring 0
        regardless of solver quality and starving the benchmark signal.

        The app float is keyed on ``wrappedNativeToken()`` alone — our own
        deployer passes ``appPaymaster = 0x0`` (deployer.py), and a V2 app
        deployed that way still needs its float. Funding an account the app
        never draws from is inert (V1 ignores its own balance; V2 ignores the
        paymaster), so no generation detection is needed. No-op for non-fee
        apps: the ``wrappedNativeToken()`` view reverts -> ``None`` -> skipped.
        """
        weth = self._read_view_address(target, b"wrappedNativeToken()")
        if not (weth and int(weth, 16)):
            return
        # V2: app-held float, paid out via safeTransfer from the app itself.
        self._deal_erc20(weth, target, 100 * 10**18)
        # V1: paymaster pull — balance + allowance to this fork instance.
        paymaster = self._read_view_address(target, b"appPaymaster()")
        if paymaster and int(paymaster, 16):
            self._deal_erc20(weth, paymaster, 100 * 10**18)
            # _set_erc20_allowance stops impersonating the paymaster.
            self._set_erc20_allowance(weth, paymaster, target, 2**256 - 1)
        # The scoreIntent tx is sent from the relayer — (re-)impersonate it.
        self._impersonate(relayer_addr)

    def _read_view_address(self, target: str, signature: bytes) -> str | None:
        """eth_call a no-arg view returning an address; None if it reverts.

        Used to read app config (e.g. ``appPaymaster()``,
        ``wrappedNativeToken()``) off a deployed contract for benchmark
        funding. Returns None when the function is absent (the call reverts)
        so callers can treat it as "not applicable".
        """
        from eth_hash.auto import keccak

        try:
            result = self.w3.eth.call({
                "to": Web3.to_checksum_address(target),
                "data": "0x" + keccak(signature)[:4].hex(),
            })
        except Exception:
            return None
        if not result or len(result) < 32:
            return None
        return Web3.to_checksum_address("0x" + result[-20:].hex())

    def _erc20_balance(self, token: str, holder: str) -> int:
        """Read ERC-20 balanceOf via eth_call. Returns 0 on any error."""
        try:
            data = bytes.fromhex("70a08231") + bytes.fromhex(holder.lower().replace("0x", "").rjust(64, "0"))
            out = self.w3.eth.call({
                "to": Web3.to_checksum_address(token), "data": data,
            })
            return int.from_bytes(bytes(out), "big") if out else 0
        except Exception:
            return 0

    def _erc20_allowance(self, token: str, owner: str, spender: str) -> int:
        """Read ERC-20 allowance(owner,spender) via eth_call. Returns 0 on error."""
        try:
            data = (
                bytes.fromhex("dd62ed3e")
                + bytes.fromhex(owner.lower().replace("0x", "").rjust(64, "0"))
                + bytes.fromhex(spender.lower().replace("0x", "").rjust(64, "0"))
            )
            out = self.w3.eth.call({
                "to": Web3.to_checksum_address(token), "data": data,
            })
            return int.from_bytes(bytes(out), "big") if out else 0
        except Exception:
            return 0

    def _snapshot_state(
        self, executor: str, tokens: list[str], allowance_target: str | None,
    ) -> dict[str, Any]:
        """Capture executor balances + allowance to a target."""
        snap: dict[str, Any] = {"executor": executor, "balances": {}}
        for t in tokens:
            if not t:
                continue
            try:
                snap["balances"][t.lower()] = str(self._erc20_balance(t, executor))
            except Exception:
                snap["balances"][t.lower()] = "?"
        if allowance_target:
            snap["allowances"] = {}
            for t in tokens:
                if not t:
                    continue
                try:
                    snap["allowances"][t.lower()] = str(
                        self._erc20_allowance(t, executor, allowance_target),
                    )
                except Exception:
                    snap["allowances"][t.lower()] = "?"
        return snap

    def simulate_with_trace(
        self,
        plan: Any,
        token_balances: dict[str, int] | None = None,
        focus_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fork-mutation-locked wrapper over :meth:`_simulate_with_trace_inner`.

        The trace body runs its own snapshot→execute→revert bracket, so with
        SIM_OFFLOAD_TO_THREAD on it must be excluded from a concurrently
        offloaded sim (which would consume/invalidate each other's snapshots).
        Kept synchronous (this is a debug/replay path that already blocked the
        loop pre-offload); the lock is uncontended unless it collides with an
        in-flight sim, in which case it briefly blocks — acceptable here.
        """
        with self._get_fork_mutation_lock():
            return self._simulate_with_trace_inner(
                plan, token_balances=token_balances, focus_tokens=focus_tokens,
            )

    def _simulate_with_trace_inner(
        self,
        plan: Any,
        token_balances: dict[str, int] | None = None,
        focus_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run plan via the manual-execution path with rich per-step trace.

        For deep debugging of revert mysteries: returns per-interaction
        snapshots of executor balances + allowances to each target,
        decoded function names, gas, status, and revert reason. Uses the
        simulator's snapshot/revert isolation so it's safe to call.

        Args:
            plan: ExecutionPlan to trace.
            token_balances: optional input-token funding for the executor.
            focus_tokens: which tokens to snapshot (default: all unique
                ERC-20s referenced as targets in the plan plus any in
                ``token_balances``).
        """
        from minotaur_subnet.simulator.revert_decoder import decode_call

        executor = self.default_executor
        focus = list(focus_tokens or [])
        for ix in plan.interactions:
            if ix.target and Web3.to_checksum_address(ix.target) not in (focus + [executor]):
                focus.append(ix.target)
        for t in (token_balances or {}).keys():
            if t not in focus:
                focus.append(t)

        snapshot_id = self._snapshot()
        try:
            self._impersonate(executor)
            if self.fund_executor:
                self._fund(executor, 100 * 10**18)
            if token_balances:
                for token_addr, amount in token_balances.items():
                    self._deal_erc20(token_addr, executor, amount)

            interactions_trace: list[dict[str, Any]] = []
            total_gas = 0
            for i, ix in enumerate(plan.interactions):
                cd = ix.call_data or "0x"
                cd_hex = cd[2:] if isinstance(cd, str) and cd.startswith("0x") else (cd if isinstance(cd, str) else cd.hex())
                cd_bytes = bytes.fromhex(cd_hex) if cd_hex else b""
                fn = decode_call(cd_bytes)
                pre = self._snapshot_state(executor, focus, ix.target)
                try:
                    receipt = self._execute_interaction(ix, executor)
                    post = self._snapshot_state(executor, focus, ix.target)
                    total_gas += receipt["gasUsed"]
                    interactions_trace.append({
                        "index": i,
                        "target": ix.target,
                        "fn": fn,
                        "calldata": cd if isinstance(cd, str) else "0x" + cd_hex,
                        "value": str(ix.value or 0),
                        "status": "ok",
                        "gas_used": receipt["gasUsed"],
                        "pre_state": pre,
                        "post_state": post,
                    })
                except Exception as exc:
                    interactions_trace.append({
                        "index": i,
                        "target": ix.target,
                        "fn": fn,
                        "calldata": cd if isinstance(cd, str) else "0x" + cd_hex,
                        "value": str(ix.value or 0),
                        "status": "reverted",
                        "revert_reason": str(exc)[:400],
                        "gas_used": 0,
                        "pre_state": pre,
                    })
                    return {
                        "interactions": interactions_trace,
                        "total_gas": total_gas,
                        "summary": (
                            f"reverted at step {i + 1}/{len(plan.interactions)}: "
                            f"{str(exc)[:200]}"
                        ),
                    }
            return {
                "interactions": interactions_trace,
                "total_gas": total_gas,
                "summary": (
                    f"all {len(plan.interactions)} interactions succeeded; "
                    f"gas={total_gas}"
                ),
            }
        finally:
            try:
                self._stop_impersonating(executor)
            except Exception:
                pass
            self._revert(snapshot_id)

    def _execute_interaction(
        self, ix: Any, sender: str
    ) -> dict[str, Any]:
        """Execute a single plan interaction as a transaction."""
        value = int(ix.value) if ix.value else 0
        call_data = ix.call_data if ix.call_data and ix.call_data != "0x" else b""

        # Convert hex string calldata to bytes if needed
        if isinstance(call_data, str):
            if call_data.startswith("0x"):
                call_data = bytes.fromhex(call_data[2:])
            elif call_data:
                call_data = bytes.fromhex(call_data)
            else:
                call_data = b""

        target = Web3.to_checksum_address(ix.target)

        tx = {
            "from": sender,
            "to": target,
            "value": value,
            "data": call_data,
            # 1M gas: multi-hop Uniswap V3 swaps use ~150k per hop
            "gas": 1_000_000,
        }

        self._pin_next_block_timestamp()
        tx_hash = self.w3.eth.send_transaction(tx)
        # Mine immediately to avoid waiting for block time. The pin is
        # one-shot (consumed by the tx's own block), so re-pin: this extra
        # block must not float back to wall clock, or the NEXT pinned send
        # would be "lower than previous block's timestamp" and get rejected.
        self._pin_next_block_timestamp()
        self.w3.provider.make_request("evm_mine", [])
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.sim_timeout)

        if receipt["status"] != 1:
            # Decode the failure: function selector from calldata + revert
            # reason from debug_traceTransaction. Without this, every
            # mystery revert costs ~$2 of LLM time guessing what broke.
            fn = decode_call(call_data)
            reason = extract_revert_via_trace(self.w3, tx_hash) or "no revert data"
            raise RuntimeError(
                f"Transaction reverted: target={ix.target} fn={fn} "
                f"reason={reason} value={ix.value}"
            )

        return dict(receipt)

    def _parse_transfer_events(self, receipt: dict) -> list[TokenTransfer]:
        """Parse ERC-20 Transfer events from a transaction receipt."""
        transfers: list[TokenTransfer] = []

        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if not topics:
                continue

            # Check if this is an ERC-20 Transfer event
            topic0 = topics[0]
            if isinstance(topic0, bytes):
                topic0_bytes = topic0
            elif isinstance(topic0, str):
                topic0_bytes = bytes.fromhex(
                    topic0[2:] if topic0.startswith("0x") else topic0
                )
            else:
                continue

            if topic0_bytes != _TRANSFER_TOPIC or len(topics) < 3:
                continue

            # Decode indexed parameters
            token = log.get("address", "")
            from_addr = _topic_to_address(topics[1])
            to_addr = _topic_to_address(topics[2])

            # Decode non-indexed amount from log data
            data = log.get("data", "0x")
            if isinstance(data, bytes):
                amount = int.from_bytes(data, "big") if data else 0
            elif isinstance(data, str):
                hex_data = data[2:] if data.startswith("0x") else data
                amount = int(hex_data, 16) if hex_data else 0
            else:
                amount = 0

            transfers.append(
                TokenTransfer(
                    token=token,
                    from_addr=from_addr,
                    to_addr=to_addr,
                    amount=str(amount),
                )
            )

        return transfers

    def _call_score_intent(
        self,
        contract_address: str,
        intent_order: dict,
        plan: ExecutionPlan,
        sender: str,
    ) -> int | None:
        """Call scoreIntent(order, plan) on the app contract.

        Returns the BPS score (0-10000) if valid, None if invalid or failed.
        The intent_order dict must contain the Solidity IntentOrder struct fields.
        """
        try:
            from eth_abi import encode as abi_encode, decode as abi_decode
            from eth_hash.auto import keccak

            # scoreIntent((IntentOrder),(ExecutionPlan)) selector
            sig = "scoreIntent((bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256),((address,uint256,bytes)[],uint256,uint256,bytes))"
            selector = keccak(sig.encode())[:4]

            # Encode IntentOrder tuple
            order_id = intent_order.get("order_id", b"\x00" * 32)
            if isinstance(order_id, str):
                try:
                    order_id = bytes.fromhex(order_id.replace("0x", "").ljust(64, "0"))[:32]
                except ValueError:
                    order_id = keccak(order_id.encode())

            app_addr = intent_order.get("app", "0x" + "00" * 20)
            intent_sel = intent_order.get("intent_selector", b"\x00" * 4)
            if isinstance(intent_sel, str):
                intent_sel = bytes.fromhex(intent_sel.replace("0x", ""))[:4]
            intent_params = intent_order.get("intent_params", b"")
            if isinstance(intent_params, str):
                intent_params = intent_params.encode()
            submitted_by = intent_order.get("submitted_by", "0x" + "00" * 20)
            chain_id = intent_order.get("chain_id", 1)
            deadline = intent_order.get("deadline", 0)
            nonce = intent_order.get("nonce", 0)
            perpetual = intent_order.get("perpetual", False)
            max_executions = intent_order.get("max_executions", 1)
            cooldown = intent_order.get("cooldown", 0)

            # Build calls tuple list for ExecutionPlan
            calls = []
            for ix in plan.interactions:
                cd = ix.call_data
                if isinstance(cd, str):
                    cd = bytes.fromhex(cd[2:] if cd.startswith("0x") else cd) if cd else b""
                calls.append((
                    Web3.to_checksum_address(ix.target),
                    int(ix.value) if ix.value else 0,
                    cd,
                ))

            plan_deadline = plan.deadline
            plan_nonce = plan.nonce
            plan_metadata = b""
            if plan.metadata:
                import json
                plan_metadata = json.dumps(plan.metadata).encode()

            # ABI-encode the full calldata
            encoded = abi_encode(
                [
                    "(bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256)",
                    "((address,uint256,bytes)[],uint256,uint256,bytes)",
                ],
                [
                    (
                        order_id,
                        Web3.to_checksum_address(app_addr),
                        intent_sel,
                        intent_params,
                        Web3.to_checksum_address(submitted_by),
                        chain_id,
                        deadline,
                        nonce,
                        perpetual,
                        max_executions,
                        cooldown,
                    ),
                    (calls, plan_deadline, plan_nonce, plan_metadata),
                ],
            )

            calldata = selector + encoded
            target = Web3.to_checksum_address(contract_address)

            # scoreIntent has onlyRelayer modifier — resolve and impersonate
            # the contract's relayer address instead of using the executor.
            score_sender = sender
            try:
                relayer_result = self.w3.eth.call({
                    "to": target,
                    "data": "0x" + keccak(b"relayer()")[:4].hex(),
                })
                relayer_addr = Web3.to_checksum_address(
                    "0x" + relayer_result[-20:].hex()
                )
                self._impersonate(relayer_addr)
                self._fund(relayer_addr, 10 ** 18)
                score_sender = relayer_addr
            except Exception:
                pass  # Fall back to executor if relayer() call fails

            result = self.w3.eth.call({
                "from": score_sender,
                "to": target,
                "data": "0x" + calldata.hex(),
                "gas": 500_000,
            })

            # Decode (uint256 score, bool valid)
            score, valid = abi_decode(["uint256", "bool"], result)
            return score if valid else None

        except Exception as exc:
            logger.debug("scoreIntent call failed: %s", exc)
            return None

    def is_connected(self) -> bool:
        """Check if the Anvil instance is reachable."""
        try:
            return self.w3.is_connected()
        except Exception:
            return False


class MultiChainSimulator:
    """Routes simulations to the correct AnvilSimulator by chain_id.

    Wraps one AnvilSimulator per chain so that orders targeting different
    chains are simulated against the correct fork state.

    Usage::

        sim = MultiChainSimulator({
            31337: "http://anvil:8545",
            8453:  "http://anvil-base:8546",
        })
        result = await sim.simulate(plan)  # routes by plan chain_id
    """

    def __init__(
        self,
        rpc_urls: dict[int, str],
        default_chain_id: int | None = None,
        upstream_rpc_urls: dict[int, str] | None = None,
        **kwargs: Any,
    ) -> None:
        if default_chain_id is None:
            from minotaur_subnet.chains import registry
            default_chain_id = registry.default_chain_id()
        self.simulators: dict[int, AnvilSimulator] = {}
        self.default_chain_id = default_chain_id
        upstream_rpc_urls = upstream_rpc_urls or {}

        for chain_id, url in rpc_urls.items():
            try:
                sim = self._build_backend(chain_id, url, upstream_rpc_urls.get(chain_id), kwargs)
                self.simulators[chain_id] = sim
                logger.info(
                    "MultiChainSimulator: chain %d → %s (%s, upstream %s)",
                    chain_id, url, type(sim).__name__,
                    "configured" if upstream_rpc_urls.get(chain_id) else "none",
                )
            except Exception as exc:
                logger.warning(
                    "MultiChainSimulator: failed to init chain %d: %s",
                    chain_id, exc,
                )

    @staticmethod
    def _build_backend(chain_id: int, url: str, upstream: str | None, kwargs: dict):
        """Pick the simulation backend for a chain from its registry ``sim_backend``.

        ``"substrate_chopsticks"`` (Bittensor 964) → SubtensorSimulator, which drives
        a Chopsticks fork of the real subtensor runtime so native staking/alpha/swap
        precompiles execute. Everything else → AnvilSimulator. Both conform to the
        same duck-typed surface (simulate / pin_read_fork / get_block_timestamp /
        is_connected), so MultiChainSimulator routes to either transparently."""
        backend = "evm"
        try:
            from minotaur_subnet.chains import registry
            spec = registry.spec(chain_id)
            if spec is not None:
                backend = spec.sim_backend
        except Exception:
            pass
        # Activation gate: use the substrate backend ONLY when the Chopsticks
        # sidecar env is actually deployed (BITTENSOR_CHOPSTICKS_SIM_RPC_URL set).
        # Without it, 964 stays on anvil-btevm — byte-identical to today, so this
        # ships INERT and is turned on fleet-wide (a coordinated step, like any
        # backend change) by deploying the sidecar + setting the env everywhere.
        if backend == "substrate_chopsticks" and os.environ.get(
            "BITTENSOR_CHOPSTICKS_SIM_RPC_URL", ""
        ).strip():
            from minotaur_subnet.simulator.subtensor_simulator import SubtensorSimulator
            return SubtensorSimulator(sidecar_url=url, chain_id=chain_id)
        return AnvilSimulator(rpc_url=url, upstream_rpc_url=upstream, **kwargs)

    def _get_simulator(self, plan: ExecutionPlan) -> AnvilSimulator | None:
        """Resolve the correct simulator for a plan's chain.

        Resolution order:
        1. plan.metadata["chain_id"] — explicit hint
        2. plan.interactions[0].chain_id — inferred from the plan itself
        3. self.default_chain_id — last-resort (typically local testnet)
        """
        chain_id = plan.metadata.get("chain_id")
        if chain_id is None and plan.interactions:
            # Fallback: infer from the plan's first interaction. Callers
            # (including /v1/apps/{id}/score) don't always stuff chain_id
            # into metadata, but every Interaction carries it.
            chain_id = plan.interactions[0].chain_id
        if chain_id is None:
            chain_id = self.default_chain_id
        if isinstance(chain_id, str):
            try:
                chain_id = int(chain_id)
            except ValueError:
                chain_id = self.default_chain_id

        sim = self.simulators.get(chain_id)
        if sim is None:
            sim = self.simulators.get(self.default_chain_id)
        return sim

    def pin_read_fork(self, chain_id: int, block_number: int) -> bool:
        """Pin the SOLVER's read fork for ``chain_id`` to ``block_number``.

        Routes to the per-chain sub-simulator (falling back to the default
        chain) and pins it so the solver reads the same block the simulator
        scores at. See :meth:`AnvilSimulator.pin_read_fork`. Returns True iff a
        re-fork happened.
        """
        try:
            cid = int(chain_id)
        except (TypeError, ValueError):
            cid = self.default_chain_id
        sim = self.simulators.get(cid) or self.simulators.get(self.default_chain_id)
        if sim is None:
            return False
        return sim.pin_read_fork(cid, block_number)

    def get_block_timestamp(
        self, chain_id: int, block_number: int | None = None,
    ) -> int | None:
        """Timestamp of ``block_number`` on ``chain_id``'s fork (``None`` =
        that fork's current anchor). Routes to the per-chain sub-simulator
        like :meth:`pin_read_fork`; returns None when unresolvable.
        """
        try:
            cid = int(chain_id)
        except (TypeError, ValueError):
            cid = self.default_chain_id
        sim = self.simulators.get(cid) or self.simulators.get(self.default_chain_id)
        if sim is None:
            return None
        return sim.get_block_timestamp(cid, block_number)

    def current_fork_block(self, chain_id: int) -> int | None:
        """The block ``chain_id``'s fork is currently anchored at (best-effort),
        routed to the per-chain sub-simulator like :meth:`pin_read_fork`. None
        when unresolvable — the quote path then falls back to a head re-fork.
        """
        try:
            cid = int(chain_id)
        except (TypeError, ValueError):
            cid = self.default_chain_id
        sim = self.simulators.get(cid) or self.simulators.get(self.default_chain_id)
        return sim.current_fork_block(cid) if sim is not None else None

    async def simulate(
        self,
        plan: ExecutionPlan,
        **kwargs: Any,
    ) -> SimulationResult:
        """Simulate a plan on the correct chain's Anvil fork."""
        sim = self._get_simulator(plan)
        if sim is None:
            chain_id = plan.metadata.get("chain_id", self.default_chain_id)
            return SimulationResult(
                success=False,
                gas_used=0,
                error=f"No simulator configured for chain {chain_id}",
            )
        return await sim.simulate(plan, **kwargs)

    async def simulate_cross_chain(
        self,
        plan: ExecutionPlan,
        bridge_registry: Any = None,
        **kwargs: Any,
    ) -> SimulationResult:
        """Simulate a cross-chain plan by running each leg independently.

        Source and destination legs are simulated on their respective chain
        forks.  Bridge legs are not simulated — a quote estimate is used
        instead.  Falls back to single-chain ``simulate()`` when the plan
        has no ``metadata["legs"]``.

        Args:
            plan: Execution plan (may contain ``metadata["legs"]``).
            bridge_registry: Optional ``BridgeRegistry`` for bridge quotes.
            **kwargs: Forwarded to per-chain ``AnvilSimulator.simulate()``.

        Returns:
            Combined ``SimulationResult`` with ``leg_results`` and
            ``bridge_estimate`` populated.
        """
        legs = plan.metadata.get("legs")
        if not legs:
            return await self.simulate(plan, **kwargs)

        leg_results: dict[int, Any] = {}
        bridge_estimate: dict[str, Any] | None = None

        for leg in sorted(legs, key=lambda l: l["leg_id"]):
            leg_id = leg["leg_id"]
            leg_chain = leg.get("chain_id", self.default_chain_id)
            leg_plan = extract_leg_plan(plan, leg_id)

            # Skip substrate legs — they execute extrinsics, not EVM txs.
            # Substrate operations are deterministic (valid or not), so
            # simulation isn't needed. The proxy executor validates before exec.
            if leg.get("runtime") == "substrate":
                leg_results[leg_id] = {
                    "success": True,
                    "type": "substrate",
                    "skipped": True,
                    "reason": "Substrate legs are not simulated on Anvil",
                }
                # For bridge legs with substrate runtime, extract bridge estimate
                if leg.get("type") == "bridge":
                    est = leg.get("estimated_output")
                    fee = leg.get("fee")
                    token_out = leg.get("token_out")
                    if est:
                        bridge_estimate = {
                            "protocol": leg.get("bridge_protocol", "tensorplex"),
                            "token_out": token_out or "",
                            "estimated_output": int(est),
                            "fee": int(fee) if fee else 0,
                        }
                continue

            # Skip wait legs (bridge finality placeholder)
            if leg.get("type") == "wait" or leg.get("runtime") == "none":
                leg_results[leg_id] = {
                    "success": True,
                    "type": "wait",
                    "skipped": True,
                }
                continue

            if leg.get("type") == "bridge":
                # Don't simulate bridge — use quote estimate
                if bridge_registry is not None:
                    try:
                        token_in = plan.metadata.get("bridge_token", "")
                        amount = plan.metadata.get("bridge_amount", 0)
                        src = plan.metadata.get("src_chain_id", 1)
                        dst = plan.metadata.get("dst_chain_id", 1)
                        quote = await bridge_registry.best_quote(
                            token_in, int(amount), src, dst,
                        )
                        if quote:
                            bridge_estimate = {
                                "protocol": quote.protocol,
                                "token_in": quote.token_in,
                                "token_out": quote.token_out,
                                "amount_in": quote.amount_in,
                                "estimated_output": quote.estimated_output,
                                "fee": quote.fee,
                                "estimated_duration_s": quote.estimated_duration_s,
                            }
                    except Exception as exc:
                        logger.warning("Bridge quote failed: %s", exc)
                        bridge_estimate = {"error": str(exc)}
                leg_results[leg_id] = {
                    "success": True,
                    "type": "bridge",
                    "bridge_estimate": bridge_estimate,
                }
                continue

            sim = self.simulators.get(leg_chain)
            if sim is None:
                leg_results[leg_id] = {
                    "success": False,
                    "error": f"No simulator for chain {leg_chain}",
                    "gas_used": 0,
                }
                continue

            # Seed destination legs with bridged token balance.
            # Bridge legs are not simulated on-chain, so the dest
            # fork has no bridged tokens — deal them to the executor.
            leg_kwargs = dict(kwargs)
            if leg.get("type") == "destination" and bridge_estimate:
                token_out = bridge_estimate.get("token_out", "")
                est_output = bridge_estimate.get("estimated_output", 0)
                if token_out and est_output:
                    existing = leg_kwargs.get("token_balances") or {}
                    leg_kwargs["token_balances"] = {
                        **existing, token_out: int(est_output),
                    }

            result = await sim.simulate(leg_plan, **leg_kwargs)
            leg_results[leg_id] = {
                "success": result.success,
                "gas_used": result.gas_used,
                "error": result.error,
                "token_transfers": [
                    {"token": t.token, "from": t.from_addr,
                     "to": t.to_addr, "amount": t.amount}
                    for t in (result.token_transfers or [])
                ],
            }

        # Combine into single SimulationResult
        all_transfers: list[TokenTransfer] = []
        total_gas = 0
        all_success = True
        first_error = None

        for lr in leg_results.values():
            total_gas += lr.get("gas_used", 0)
            if not lr.get("success", False):
                all_success = False
                if first_error is None:
                    first_error = lr.get("error")
            for t in lr.get("token_transfers", []):
                all_transfers.append(TokenTransfer(
                    token=t["token"],
                    from_addr=t["from"],
                    to_addr=t["to"],
                    amount=t["amount"],
                ))

        return SimulationResult(
            success=all_success,
            gas_used=total_gas,
            error=first_error,
            token_transfers=all_transfers,
            leg_results=leg_results,
            bridge_estimate=bridge_estimate,
        )

    def is_connected(self) -> bool:
        """True if at least one chain simulator is connected."""
        return any(s.is_connected() for s in self.simulators.values())


def _topic_to_address(topic: Any) -> str:
    """Extract an Ethereum address from a log topic (last 20 bytes)."""
    if isinstance(topic, bytes):
        return Web3.to_checksum_address("0x" + topic[-20:].hex())
    if isinstance(topic, str):
        hex_str = topic[2:] if topic.startswith("0x") else topic
        return Web3.to_checksum_address("0x" + hex_str[-40:])
    return ""
