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

"WITHIN the same call" is the whole design constraint, and it used to be a
scoring DIVERGENCE: the backend ran a plan's interactions as N separate
dry-runs, so nothing composed and chain 964 scored by different rules than
chains 1 and 8453 — a leg that wrapped native and then moved the wrapper
measured zero here and worked there. ``PlanRunner`` (tools/chopsticks-sim/,
installed at the executor via anvil_setCode) packs the whole plan into ONE
call, restoring parity, and samples watched addresses' native balances across
the span so a native delivery is measurable at all.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

from minotaur_subnet.shared.types import (
    plan_metadata_fields,
    ExecutionPlan,
    SimulationResult,
    TokenTransfer,
)

logger = logging.getLogger(__name__)

# keccak256("Transfer(address,address,uint256)")
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_DEFAULT_EXECUTOR = "0x000000000000000000000000000000000000c0de"
# What a native (non-ERC-20) delivery is recorded as. Matches
# blockchain.tokens.NATIVE_SENTINEL; kept literal so this module stays
# import-light (stdlib + shared.types only).
_NATIVE_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
# ── plan composition (chain-964 parity with anvil) ──────────────────────────
# Chopsticks cannot build blocks (pallet_drand's per-block hook needs a
# BLS12-381 host fn its executor lacks), so every ck_ethCall is an INDEPENDENT
# dry-run against the pinned fork. Running a plan's interactions as N separate
# calls therefore gave NO composition: interaction 2 could not see interaction
# 1's effects. A destination leg that wrapped native and then moved the wrapper
# measured nothing, because as far as the transfer was concerned the wrap had
# never happened — while the SAME plan on anvil (chains 1/8453) composed fine.
# Chain 964 was silently scored by different rules.
#
# PlanRunner restores parity the only way this fork allows: one call, every
# interaction inside it, state composing as it goes — the same measuring-router
# trick StakeMeter already uses for staking. Installed via anvil_setCode AT THE
# EXECUTOR ADDRESS so sub-calls carry msg.sender == executor exactly as on
# anvil (the idiom the anvil gas meter uses at the relayer address).
#
# Source: tools/chopsticks-sim/PlanRunner.sol (solc 0.8.33, 2293 bytes runtime).
_PLAN_RUNNER_SELECTOR = "92595a16"  # runPlan((address,uint256,bytes)[],address[])
_PLAN_RUNNER_RUNTIME_HEX = (
    "0x608060405260043610610021575f3560e01c806392595a161461002457610022"
    "565b5b005b61003e60048036038101906100399190610444565b610056565b60"
    "405161004d939291906106ad565b60405180910390f35b606080606084849050"
    "67ffffffffffffffff811115610078576100776106f7565b5b60405190808252"
    "80602002602001820160405280156100a6578160200160208202803683378082"
    "0191505090505b5092505f5b8585905081101561011f578585828181106100c9"
    "576100c8610724565b5b90506020020160208101906100de91906107ab565b73"
    "ffffffffffffffffffffffffffffffffffffffff163184828151811061010857"
    "610107610724565b5b6020026020010181815250508060010190506100ab565b"
    "508686905067ffffffffffffffff81111561013d5761013c6106f7565b5b6040"
    "5190808252806020026020018201604052801561017057816020015b60608152"
    "6020019060019003908161015b5790505b5090505f5b878790508110156102b6"
    "575f5f89898481811061019557610194610724565b5b90506020028101906101"
    "a791906107e2565b5f0160208101906101b891906107ab565b73ffffffffffff"
    "ffffffffffffffffffffffffffff168a8a858181106101e1576101e061072456"
    "5b5b90506020028101906101f391906107e2565b602001358b8b868181106102"
    "0a57610209610724565b5b905060200281019061021c91906107e2565b806040"
    "019061022b9190610809565b6040516102399291906108a7565b5f6040518083"
    "038185875af1925050503d805f8114610273576040519150601f19603f3d0116"
    "82016040523d82523d5f602084013e610278565b606091505b50915091508161"
    "028a57805160208201fd5b8084848151811061029e5761029d610724565b5b60"
    "200260200101819052505050806001019050610175565b508484905067ffffff"
    "ffffffffff8111156102d4576102d36106f7565b5b6040519080825280602002"
    "6020018201604052801561030257816020016020820280368337808201915050"
    "90505b5091505f5b8585905081101561037b5785858281811061032557610324"
    "610724565b5b905060200201602081019061033a91906107ab565b73ffffffff"
    "ffffffffffffffffffffffffffffffff16318382815181106103645761036361"
    "0724565b5b602002602001018181525050806001019050610307565b50945094"
    "5094915050565b5f5ffd5b5f5ffd5b5f5ffd5b5f5ffd5b5f5ffd5b5f5f83601f"
    "8401126103af576103ae61038e565b5b8235905067ffffffffffffffff811115"
    "6103cc576103cb610392565b5b6020830191508360208202830111156103e857"
    "6103e7610396565b5b9250929050565b5f5f83601f8401126104045761040361"
    "038e565b5b8235905067ffffffffffffffff8111156104215761042061039256"
    "5b5b60208301915083602082028301111561043d5761043c610396565b5b9250"
    "929050565b5f5f5f5f6040858703121561045c5761045b610386565b5b5f8501"
    "3567ffffffffffffffff8111156104795761047861038a565b5b610485878288"
    "0161039a565b9450945050602085013567ffffffffffffffff8111156104a857"
    "6104a761038a565b5b6104b4878288016103ef565b9250925050929591945092"
    "50565b5f81519050919050565b5f82825260208201905092915050565b5f8190"
    "50602082019050919050565b5f819050919050565b6104fd816104eb565b8252"
    "5050565b5f61050e83836104f4565b60208301905092915050565b5f60208201"
    "9050919050565b5f610530826104c2565b61053a81856104cc565b9350610545"
    "836104dc565b805f5b8381101561057557815161055c8882610503565b975061"
    "05678361051a565b925050600181019050610548565b50859350505050929150"
    "50565b5f81519050919050565b5f82825260208201905092915050565b5f8190"
    "50602082019050919050565b5f81519050919050565b5f828252602082019050"
    "92915050565b8281835e5f83830152505050565b5f601f19601f830116905091"
    "9050565b5f6105ed826105ab565b6105f781856105b5565b9350610607818560"
    "2086016105c5565b610610816105d3565b840191505092915050565b5f610626"
    "83836105e3565b905092915050565b5f602082019050919050565b5f61064482"
    "610582565b61064e818561058c565b9350836020820285016106608561059c56"
    "5b805f5b8581101561069b578484038952815161067c858261061b565b945061"
    "06878361062e565b925060208a01995050600181019050610663565b50829750"
    "879550505050505092915050565b5f6060820190508181035f8301526106c581"
    "86610526565b905081810360208301526106d98185610526565b905081810360"
    "408301526106ed818461063a565b9050949350505050565b7f4e487b71000000"
    "000000000000000000000000000000000000000000000000005f526041600452"
    "60245ffd5b7f4e487b7100000000000000000000000000000000000000000000"
    "0000000000005f52603260045260245ffd5b5f73ffffffffffffffffffffffff"
    "ffffffffffffffff82169050919050565b5f61077a82610751565b9050919050"
    "565b61078a81610770565b8114610794575f5ffd5b50565b5f813590506107a5"
    "81610781565b92915050565b5f602082840312156107c0576107bf610386565b"
    "5b5f6107cd84828501610797565b91505092915050565b5f5ffd5b5f5ffd5b5f"
    "5ffd5b5f823560016060038336030381126107fd576107fc6107d6565b5b8083"
    "0191505092915050565b5f5f8335600160200384360303811261082557610824"
    "6107d6565b5b80840192508235915067ffffffffffffffff8211156108475761"
    "08466107da565b5b602083019250600182023603831315610863576108626107"
    "de565b5b509250929050565b5f81905092915050565b828183375f8383015250"
    "5050565b5f61088e838561086b565b935061089b838584610875565b82840190"
    "509392505050565b5f6108b3828486610883565b9150819050939250505056fe"
    "a26469706673582212202732ce216a5529de350f1d85d6c3faccc2842ef8b554"
    "70516c2c1af6b99bd14964736f6c63430008210033"
)


# Generous native funding for the executor's mapped account (rao; 1 TAO = 1e9 rao).
_DEFAULT_FUND_RAO = 100_000 * 1_000_000_000
# Budget for the App scoreIntent read (see the call site). A cold fork's first
# mutating call costs ~60-90s of upstream state fetch before it does any work.
_SCORE_INTENT_TIMEOUT_S = 300.0


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
    def _rpc(self, method: str, params: list | None = None, url: str | None = None, timeout: float | None = None) -> Any:
        target = (url or self.sidecar_url)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        req = urllib.request.Request(target, data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=(timeout or self.rpc_timeout)) as resp:
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
                 value: int = 0, gas: str | None = None, url: str | None = None,
                 timeout: float | None = None) -> dict:
        """Dry-run a call on the fork. ``value`` is WEI (964's native TAO is
        18-decimal), and it is sent as a STRING — never as a JSON number.

        The sidecar is JavaScript. A bare JSON number is parsed as a double and
        handed to polkadot.js, whose ``U256`` codec refuses anything above
        ``Number.MAX_SAFE_INTEGER`` (9,007,199,254,740,991 ≈ 0.009 TAO):

            createType(PrimitiveTypesU256):: Number needs to be an integer
            <= Number.MAX_SAFE_INTEGER

        So EVERY value-bearing call carrying a realistic TAO amount failed at
        the RPC layer — 0.01 TAO and up, which is every cross-chain delivery
        anyone would benchmark. Measured against the live bench sidecar: as an
        int 6.993e18 raises the error above; as a decimal or hex string the
        same call succeeds. ``set_balance`` already stringifies (which is why
        FUNDING always worked while SPENDING never did) — this makes the two
        agree.

        Note the failures this does NOT explain, so they are not chased again:
        a value between 1 and 499 rao returns ``outOfFund``. That is subtensor's
        500-rao EXISTENTIAL DEPOSIT (measured: 200 rao fails, 500 rao passes),
        a property of the chain, not of the seeding — the executor is funded
        100,000 TAO by ``_DEFAULT_FUND_RAO`` and raising that changes nothing.
        """
        return self._rpc("ck_ethCall", [{
            "from": from_addr or self.default_executor,
            "to": to, "data": data, "value": str(int(value or 0)), "gas": gas,
        }], url=url, timeout=timeout)

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
        delivery_recipients: list[str] | None = None,
    ) -> SimulationResult:
        """Execute ``plan`` against the pinned Chopsticks fork as a dry-run and
        report the delivered-output surface. Signature matches
        ``AnvilSimulator.simulate`` so MultiChainSimulator routes here unchanged.

        Interactions COMPOSE: they are executed together inside one
        ``PlanRunner`` call, so a step sees the previous step's effects exactly
        as it would on an anvil chain. This is not free composition — block
        building is BLS-blocked on subtensor's runtime, so each ``ck_ethCall``
        is still an isolated dry-run — it is the whole plan packed into a
        single one of them. Plans that depend on state persisting BETWEEN
        top-level calls (across separate simulate() invocations) still cannot
        work here.

        ``delivery_recipients`` names addresses whose NATIVE balance is sampled
        across the span. A bridge that credits native (Tensorplex on 964) emits
        no ERC-20 Transfer log, so without this a correct delivery is
        indistinguishable from no delivery at all.
        """
        # An empty interaction list is not necessarily an empty PLAN. For an App
        # whose plan is DATA rather than code — AlphaYieldApp on 964 reads an
        # abi-encoded recommendation out of plan.metadata and does the work
        # itself, ignoring plan.calls entirely — `interactions: []` is the
        # CORRECT shape, and everything that scores it lives in the App's own
        # scoreIntent, read further down.
        #
        # Bailing here made that whole class unscoreable: the chain-964 dry-run
        # returned `simulation_failed: empty plan` for a perfectly formed plan
        # (2026-08-26). Only bail when there is genuinely nothing to do — no
        # calls AND no scoreIntent path to fall back on.
        if not plan.interactions and not (contract_address and intent_order):
            return SimulationResult(success=False, error="empty plan")

        # Pick ONE sidecar for this whole simulate() (round-robin across the pool)
        # — re-pin, fund, and call all target the same fork instance.
        url = self._pick_url()

        if fork_block is not None:
            self._repin_one(url, self.chain_id, fork_block)

        # Resolve the `from` address exactly as the anvil path does: discover the
        # App's OWN configured relayer via `relayer()` and use it for BOTH the plan
        # execution and the scoreIntent read (an AppIntentBase App gates both on
        # msg.sender == relayer()). Dry-runs accept an arbitrary `from`, so — unlike
        # anvil's state-changing send — NO impersonation is needed. Falls back to
        # metadata.executor / the default when the target has no relayer() (e.g. the
        # measuring router).
        # `if plan.metadata` is TRUTHY for the raw bytes an abi.decoding App
        # takes (#1617), so this used to call .get on bytes and raise. This is
        # the chain-964 scoring path — the one AlphaYieldApp actually runs on.
        executor = (plan_metadata_fields(plan).get("executor")) or self.default_executor
        if contract_address:
            relayer = self._discover_relayer(contract_address, url)
            if relayer:
                executor = relayer

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
        if plan.interactions:
            composed = self._run_plan_composed(
                plan, executor, url, delivery_recipients=delivery_recipients,
            )
            if composed.get("error"):
                return SimulationResult(
                    success=False,
                    error=composed["error"],
                    revert_reason=composed.get("revert_reason"),
                )
            transfers.extend(composed["transfers"])
            total_gas = composed["gas_used"]
            last_return = composed["last_return"]

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

        # ── on-chain score (BPS) via the App's scoreIntent ───────────────────
        # Build the generic AppIntentBase scoreIntent((IntentOrder),(ExecutionPlan))
        # calldata from the order the orchestrator passed (identical encoding to the
        # anvil path — the outer tuple is app-agnostic; app-specific data lives in
        # intent_params), call it read-only, decode (uint256 score, bool valid).
        # An order may still supply pre-built score_intent_calldata to override.
        # Best-effort: absent/failed leaves on_chain_score=None (raw_output drives
        # scoring). NOTE from=executor; an App that gates scoreIntent on a specific
        # relayer msg.sender would return invalid here — raw_output still scores.
        sic = (intent_order or {}).get("score_intent_calldata") if intent_order else None
        if contract_address and not sic and intent_order:
            try:
                sic = self._build_score_intent_calldata(contract_address, intent_order, plan)
            except Exception as exc:  # noqa: BLE001
                logger.warning("scoreIntent calldata build failed: %s", exc)
        if contract_address and sic:
            try:
                # LONG budget on purpose. This is a whole App scoreIntent on a
                # possibly COLD Chopsticks fork — AlphaYieldApp alone reads the
                # metagraph for every allowlisted candidate — and the first call
                # after a re-pin pulls all that state from upstream. The 60s
                # default kills it and surfaces as "scoreIntent read failed:
                # timed out", i.e. on_chain_score None for a plan that is fine
                # (chain 964, 2026-08-26). Only THIS read is widened; the
                # per-interaction calls keep the default so a genuinely hung
                # sidecar still fails fast.
                r = self.eth_call(to=contract_address, data=sic, from_addr=executor,
                                  url=url, timeout=_SCORE_INTENT_TIMEOUT_S)
                if r.get("success"):
                    from eth_abi import decode as abi_decode
                    ret = r.get("returnData") or "0x"
                    raw = bytes.fromhex(ret[2:] if ret.startswith("0x") else ret)
                    score_val, valid = abi_decode(["uint256", "bool"], raw)
                    result.on_chain_score = int(score_val) if valid else None
            except Exception as exc:  # noqa: BLE001
                logger.warning("scoreIntent read failed: %s", exc)
        return result

    def _discover_relayer(self, contract_address: str, url: str) -> str | None:
        """Call ``relayer()`` on the App (as the anvil path does) to get the address
        its scoreIntent/executeIntent gate on as msg.sender. Returns None if the
        target has no relayer() getter (e.g. the bare measuring router) or returns
        the zero address, so callers fall back to the default executor."""
        from eth_hash.auto import keccak
        try:
            r = self.eth_call(to=contract_address,
                              data="0x" + keccak(b"relayer()")[:4].hex(), url=url)
        except Exception:  # noqa: BLE001
            return None
        ret = (r or {}).get("returnData") or "0x"
        h = ret[2:] if ret.startswith("0x") else ret
        if not r.get("success") or len(h) < 64:
            return None
        addr = "0x" + h[-40:]
        return addr if int(addr, 16) != 0 else None

    def _build_score_intent_calldata(self, contract_address, intent_order, plan) -> str:
        """Encode scoreIntent((IntentOrder),(ExecutionPlan)) — ported verbatim from
        AnvilSimulator._simulate_via_score_intent so a 964 App following the generic
        AppIntentBase convention is scored identically. App-specific params live in
        the ``intent_params`` bytes (manifest-encoded upstream by the orchestrator)."""
        from eth_abi import encode as abi_encode
        from eth_hash.auto import keccak
        from eth_utils import to_checksum_address

        sig = ("scoreIntent((bytes32,address,bytes4,bytes,address,uint256,uint256,"
               "uint256,bool,uint256,uint256),((address,uint256,bytes)[],uint256,"
               "uint256,bytes))")
        selector = keccak(sig.encode())[:4]

        order_id = intent_order.get("order_id", b"\x00" * 32)
        if isinstance(order_id, str):
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
                intent_params = (bytes.fromhex(intent_params)
                                 if all(c in "0123456789abcdefABCDEF" for c in intent_params)
                                 else intent_params.encode())

        submitted_by = intent_order.get("submitted_by", "0x" + "00" * 20)
        chain_id = intent_order.get("chain_id", self.chain_id)
        deadline = intent_order.get("deadline", 0)
        nonce = intent_order.get("nonce", 0)
        perpetual = intent_order.get("perpetual", False)
        max_executions = intent_order.get("max_executions", 1)
        cooldown = intent_order.get("cooldown", 0)

        calls = []
        for ix in plan.interactions:
            cd = ix.call_data
            if isinstance(cd, str):
                cd = bytes.fromhex(cd[2:] if cd.startswith("0x") else cd) if cd else b""
            calls.append((to_checksum_address(ix.target), int(ix.value) if ix.value else 0, cd))

        # A solver may emit metadata that is ALREADY encoded bytes — an App whose
        # contract abi.decodes it needs the exact bytes, and JSON-wrapping them
        # destroys the plan. relayer/encoder.py and consensus/signatures.py
        # already guard this; the simulation and contract-call paths did not, so
        # a bytes-metadata plan was SIGNED correctly and SCORED as garbage.
        if not plan.metadata:
            plan_metadata = b""
        elif isinstance(plan.metadata, dict):
            plan_metadata = json.dumps(plan.metadata).encode()
        else:
            plan_metadata = plan.metadata

        encoded = abi_encode(
            ["(bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256)",
             "((address,uint256,bytes)[],uint256,uint256,bytes)"],
            [(order_id, to_checksum_address(app_addr), intent_sel, intent_params,
              to_checksum_address(submitted_by), chain_id, deadline, nonce,
              perpetual, max_executions, cooldown),
             (calls, plan.deadline, plan.nonce, plan_metadata)],
        )
        return "0x" + (selector + encoded).hex()

    @classmethod
    def _last_word(cls, ret_hex: str) -> int | None:
        """Decode the LAST 32-byte ABI word of a return blob as uint256."""
        h = ret_hex[2:] if ret_hex.startswith("0x") else ret_hex
        if len(h) < 64:
            return None
        return int(h[-64:], 16)

    def _run_plan_composed(
        self,
        plan: ExecutionPlan,
        executor: str,
        url: str,
        delivery_recipients: list[str] | None = None,
    ) -> dict:
        """Execute every interaction inside ONE dry-run, so state composes.

        Returns ``{transfers, gas_used, last_return}`` or ``{error, ...}``.

        Two things this buys that the previous per-interaction loop could not:

        * **Composition.** Each ``ck_ethCall`` is independent, so N calls meant
          N isolated dry-runs against the same pre-state. Inside PlanRunner the
          calls run in sequence within one EVM execution, exactly as they do on
          an anvil chain.
        * **Native delivery.** Native movement emits no ERC-20 Transfer log, so
          a bridge that credits native (Tensorplex on 964) was invisible to
          log-based accounting and scored ``nothing_delivered`` however
          correctly it had delivered. PlanRunner samples the watched addresses'
          balances either side of the span, and a rise becomes a synthetic
          transfer carrying the native sentinel as its token.
        """
        from eth_abi import decode as abi_decode, encode as abi_encode

        calls = [
            (
                self._addr(ix.target),
                int(ix.value or 0),
                bytes.fromhex(str(ix.call_data or "0x").removeprefix("0x")),
            )
            for ix in plan.interactions
            if not (ix.chain_id and ix.chain_id != self.chain_id)
        ]
        if not calls:
            return {"transfers": [], "gas_used": 0, "last_return": None}

        # The executor is always watched: it is where a bridge-seeded native
        # balance lands, so a leg that never forwards it is distinguishable
        # from one that did.
        watch: list[str] = [self._addr(executor)]
        for r in delivery_recipients or []:
            a = self._addr(r)
            if a and a not in watch:
                watch.append(a)

        # Install the runner AT the executor so sub-calls carry
        # msg.sender == executor. Must come after any re-pin: a re-pin drops
        # pending cheatcode overrides (see chopsticks_anvil.repin).
        try:
            self.set_code(executor, _PLAN_RUNNER_RUNTIME_HEX, url=url)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"plan runner install failed: {exc}"}

        data = "0x" + _PLAN_RUNNER_SELECTOR + abi_encode(
            ["(address,uint256,bytes)[]", "address[]"], [calls, watch],
        ).hex()

        total_value = sum(c[1] for c in calls)
        try:
            r = self.eth_call(
                to=executor, data=data, from_addr=executor,
                value=total_value, url=url,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"composed plan rpc: {exc}"}

        if not r.get("success"):
            # PlanRunner bubbles a failed interaction's revert unchanged, so
            # this is the underlying revert, not a wrapper error.
            return {
                "error": "plan reverted",
                "revert_reason": json.dumps(r.get("exitReason")),
            }

        used = r.get("usedGas") or 0
        gas_used = int(used, 16) if isinstance(used, str) else int(used)

        transfers = self._parse_transfers(r.get("logs") or [])

        last_return = None
        ret_hex = str(r.get("returnData") or "")
        if ret_hex:
            try:
                before, after, rets = abi_decode(
                    ["uint256[]", "uint256[]", "bytes[]"],
                    bytes.fromhex(ret_hex.removeprefix("0x")),
                )
                # Only a RISE counts. The executor's balance normally falls (it
                # is funding the calls), and a fall is not a delivery.
                for addr, b, a in zip(watch, before, after):
                    if a > b:
                        transfers.append(TokenTransfer(
                            token=_NATIVE_SENTINEL,
                            from_addr="",
                            to_addr=addr,
                            amount=int(a - b),
                        ))
                if rets:
                    last_return = "0x" + bytes(rets[-1]).hex()
            except Exception as exc:  # noqa: BLE001
                # A decode failure must not fail the whole leg: the calls DID
                # run and their logs are already counted. Report what is known.
                logger.warning("PlanRunner return decode failed: %s", exc)
                last_return = ret_hex

        return {"transfers": transfers, "gas_used": gas_used, "last_return": last_return}

    @staticmethod
    def _addr(a: Any) -> str:
        """Lowercase 0x-prefixed address, or "" when unusable."""
        t = str(a or "").strip().lower()
        return t if t.startswith("0x") and len(t) == 42 else ""

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
