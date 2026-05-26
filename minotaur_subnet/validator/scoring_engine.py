"""Scoring engine — plan verification and scoring for consensus proposals.

Extracts the core verification and scoring logic from the validator's
consensus proposal handler. Handles:
- Proposer signature verification (EIP-191)
- Plan re-simulation on follower's Anvil fork
- JS re-scoring with leader's simulation data
- On-chain score gating
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, TYPE_CHECKING

from minotaur_subnet.shared.types import (
    ExecutionPlan,
    Interaction,
    IntentState,
    SimulationResult,
    TokenTransfer,
)
from minotaur_subnet.shared.simulation import build_mock_simulation

if TYPE_CHECKING:
    from minotaur_subnet.consensus import ConsensusManager
    from minotaur_subnet.consensus.peer_network import ValidatorPeerNetwork
    from minotaur_subnet.engine import JsExecutionEngine
    from minotaur_subnet.simulator.anvil_simulator import MultiChainSimulator
    from minotaur_subnet.store import AppIntentStore

logger = logging.getLogger("minotaur_subnet.validator.scoring_engine")


class ScoringEngine:
    """Encapsulates plan verification and scoring for consensus proposals."""

    def __init__(
        self,
        js_engine: JsExecutionEngine,
        store: AppIntentStore,
        simulator: MultiChainSimulator | None = None,
        consensus: ConsensusManager | None = None,
        peer_network: ValidatorPeerNetwork | None = None,
        validator_id: str = "",
    ) -> None:
        self.js_engine = js_engine
        self.store = store
        self.simulator = simulator
        self.consensus = consensus
        self.peer_network = peer_network
        self.validator_id = validator_id

    def verify_proposer_signature(self, body: dict) -> tuple[bool, str]:
        """Verify that a consensus proposal was signed by a registered validator.

        The proposer_signature field should be an EIP-191 personal-sign over
        the JSON-serialised proposal payload (excluding the signature field
        itself).  Returns (ok, reason).
        """
        require_signed = os.environ.get(
            "CONSENSUS_REQUIRE_SIGNED_PROPOSALS", "1"
        ).strip().lower() in ("1", "true", "yes", "on")

        signature_hex = body.get("proposer_signature")
        if not signature_hex:
            if require_signed:
                logger.warning(
                    "Rejecting unsigned consensus proposal (CONSENSUS_REQUIRE_SIGNED_PROPOSALS=1)"
                )
                return False, "Missing proposer_signature (required in production mode)"
            else:
                logger.warning(
                    "Accepting unsigned consensus proposal — "
                    "CONSENSUS_REQUIRE_SIGNED_PROPOSALS is disabled"
                )
                return True, ""

        # Reconstruct the canonical message the proposer should have signed.
        # Strip the signature field so both sides hash the same content.
        payload = {k: v for k, v in body.items() if k != "proposer_signature"}
        canonical_msg = json.dumps(payload, sort_keys=True, separators=(",", ":"))

        try:
            from eth_account import Account
            from eth_account.messages import encode_defunct

            msg = encode_defunct(text=canonical_msg)
            recovered_address = Account.recover_message(msg, signature=signature_hex)
        except Exception as exc:
            logger.warning("Proposal signature recovery failed: %s", exc)
            return False, f"Signature recovery failed: {exc}"

        # Leader-lock: when LOCKED_LEADER_EVM_ADDRESS is set, only that
        # signer may propose. Counterpart to the election-side lock in
        # elect_leader; prevents a registered-but-rogue validator from
        # spamming proposals or staging a competing-leadership scenario.
        # Any-validator acceptance is only used when the lock is cleared.
        from minotaur_subnet.validator.metagraph_sync import (
            LOCKED_LEADER_EVM_ADDRESS,
        )
        if LOCKED_LEADER_EVM_ADDRESS:
            if recovered_address.lower() != LOCKED_LEADER_EVM_ADDRESS.lower():
                logger.warning(
                    "Proposal signed by %s but locked leader is %s",
                    recovered_address, LOCKED_LEADER_EVM_ADDRESS,
                )
                return False, (
                    f"Proposer {recovered_address} is not the locked leader "
                    f"({LOCKED_LEADER_EVM_ADDRESS})"
                )
            return True, ""

        # Check that the recovered address is a known validator peer
        known_validators: set[str] = set()
        if self.consensus is not None:
            known_validators = {v.lower() for v in self.consensus.validators}
        if self.peer_network is not None:
            known_validators |= {p.validator_id.lower() for p in self.peer_network.peers}
        # Include our own ID
        if self.validator_id:
            known_validators.add(self.validator_id.lower())

        if recovered_address.lower() not in known_validators:
            logger.warning(
                "Proposal signed by unknown validator %s (known: %s)",
                recovered_address,
                [v[:10] for v in known_validators],
            )
            return False, f"Proposer {recovered_address} is not a registered validator"

        return True, ""

    async def _simulate_plan(
        self,
        plan: ExecutionPlan,
        plan_data: dict,
        params: dict,
        deployment: Any,
        order_id: str,
        submitted_by: str,
        chain_id: int,
        intent_function: str,
        deadline: int,
        perpetual: bool,
        max_executions: int,
        cooldown: int,
    ) -> SimulationResult | None:
        """Re-simulate a plan on the follower's Anvil fork.

        Returns a SimulationResult if simulation succeeds, None otherwise.
        """
        if self.simulator is None:
            return None

        try:
            contract_address = None
            if deployment and deployment.contract_address:
                contract_address = deployment.contract_address
            token_balances = None
            input_token = params.get("input_token")
            input_amount = params.get("input_amount")
            if input_token and input_amount:
                try:
                    token_balances = {input_token: int(input_amount)}
                except (TypeError, ValueError):
                    token_balances = None
            intent_order_dict = None
            if contract_address:
                # Resolve 4-byte intent selector
                _raw_sel = params.get("intent_selector") or ""
                if not _raw_sel or not all(
                    c in '0123456789abcdefABCDEF'
                    for c in _raw_sel.replace("0x", "")
                ):
                    from eth_hash.auto import keccak as _keccak
                    _fn = intent_function or "swap"
                    _KNOWN_SIGS = {
                        "swap": "swap(address,address,uint256,uint256,address)",
                        "execute": "swap(address,address,uint256,uint256,address)",
                        "buy": "buy(address,address,uint256,uint256,address)",
                    }
                    _sig = _KNOWN_SIGS.get(_fn, f"{_fn}()")
                    _raw_sel = _keccak(_sig.encode())[:4].hex()
                intent_order_dict = {
                    "order_id": order_id,
                    "app": contract_address,
                    "intent_selector": _raw_sel,
                    "intent_params": params.get("intent_params_hex", ""),
                    "submitted_by": submitted_by,
                    "chain_id": chain_id,
                    "deadline": deadline,
                    "nonce": int(params.get("user_nonce", 0) or 0),
                    "perpetual": perpetual,
                    "max_executions": max_executions,
                    "cooldown": cooldown,
                }
            # Mock bridge protocol calls for simulation
            sim_plan = plan
            plan_meta = plan_data.get("metadata", {})
            if (
                plan_meta.get("cross_chain")
                or plan_meta.get("multi_leg_plan")
                or params.get("_is_bridge_leg")
            ):
                from minotaur_subnet.shared.types import mock_bridge_interactions
                input_token = params.get("input_token", "")
                input_amount = int(params.get("input_amount", "0") or "0")
                mock_ixs = mock_bridge_interactions(
                    plan.interactions, token_address=input_token, amount=input_amount,
                )
                if mock_ixs != plan.interactions:
                    sim_plan = ExecutionPlan(
                        intent_id=plan.intent_id,
                        interactions=mock_ixs,
                        deadline=plan.deadline,
                        nonce=plan.nonce,
                        metadata=plan.metadata,
                    )
                    print(f"[VALIDATOR] using mock bridge for simulation", flush=True)

            print(
                f"[VALIDATOR] resim: contract={contract_address} "
                f"intent_params_len={len(intent_order_dict.get('intent_params', '') if intent_order_dict else '')} "
                f"nonce={intent_order_dict.get('nonce') if intent_order_dict else None} "
                f"selector={intent_order_dict.get('intent_selector') if intent_order_dict else None}",
                flush=True,
            )
            simulation = await self.simulator.simulate(
                sim_plan,
                contract_address=contract_address,
                intent_order=intent_order_dict,
                token_balances=token_balances,
            )
            return simulation
        except Exception as exc:
            logger.warning("Follower re-simulation failed: %s", exc)
            return None

    def _build_simulation_from_leader(
        self, leader_sim: dict,
    ) -> SimulationResult:
        """Build a SimulationResult from leader's simulation data."""
        return SimulationResult(
            success=leader_sim.get("success", False),
            gas_used=leader_sim.get("gas_used", 0),
            token_transfers=[
                TokenTransfer(
                    token=t.get("token", ""),
                    from_addr=t.get("from_addr", ""),
                    to_addr=t.get("to_addr", ""),
                    amount=t.get("amount", "0"),
                )
                for t in leader_sim.get("token_transfers", [])
            ],
            on_chain_score=leader_sim.get("on_chain_score"),
            error=leader_sim.get("error"),
        )

    async def _score_via_js(
        self,
        app_id: str,
        plan: ExecutionPlan,
        simulation: SimulationResult,
        state: IntentState,
    ) -> float:
        """Score a plan via JS engine. Returns the score value."""
        score_result = await self.js_engine.score(app_id, plan, simulation, state)
        return score_result.score

    async def verify_and_score_proposal(
        self,
        body: dict,
        score_threshold: float,
    ) -> dict:
        """Core proposal verification and scoring logic.

        Returns a dict with:
        - approved: bool
        - reason: str (if not approved)
        - local_score: float (if approved)
        - plan: ExecutionPlan | None
        - simulation: SimulationResult | None
        - contract_address: str | None (resolved for the app/chain)
        """
        order_id = body.get("order_id", "")
        plan_hash = body.get("plan_hash", "")
        score = body.get("score", 0.0)
        app_id = body.get("app_id", "")
        intent_function = body.get("intent_function", "execute")
        chain_id = int(body.get("chain_id", 1) or 1)
        params = body.get("params", {}) or {}
        submitted_by = body.get("submitted_by", "") or ""
        deadline = int(body.get("deadline", 0) or 0)
        perpetual = bool(body.get("perpetual", False))
        max_executions = int(body.get("max_executions", 1) or 1)
        cooldown = int(body.get("cooldown", 0) or 0)

        # SECURITY-CRITICAL: Default is "1" (re-simulate). Trusting leader
        # simulation data without independent verification is unsafe in
        # production — a compromised leader could fabricate simulation results
        # to get followers to sign malicious plans.
        resimulate_proposals = os.environ.get(
            "FOLLOWER_PROPOSAL_RESIMULATE",
            "1",
        ).strip().lower() in ("1", "true", "yes", "on")

        from minotaur_subnet.consensus.dissent import RejectionCode

        if not order_id or not plan_hash:
            return {
                "approved": False,
                "reason": "order_id and plan_hash required",
                "reason_code": RejectionCode.MALFORMED_PAYLOAD.value,
                "status": 400,
            }

        # Off-chain mirror of the on-chain AppRegistry gate. A compromised
        # leader could propose plans for unregistered apps; a follower that
        # signed anyway would be participating in unauthorised execution.
        # No-op on chains with no APP_REGISTRY_{chain_id} env configured.
        if app_id and self.store is not None:
            from minotaur_subnet.consensus.app_registry_cache import is_registered_app
            _dep = self.store.get_deployment(app_id, chain_id=chain_id)
            if _dep and _dep.contract_address and not is_registered_app(
                _dep.contract_address, chain_id,
            ):
                return {
                    "approved": False,
                    "reason": (
                        f"App {app_id} contract {_dep.contract_address} not "
                        f"registered in AppRegistry on chain {chain_id}"
                    ),
                    "reason_code": RejectionCode.APP_NOT_REGISTERED.value,
                    "status": 403,
                }

        # Re-score via JS engine if the app is loaded (CON-4, VAL-14)
        local_score = score  # Default: trust leader's score
        plan_data = body.get("plan", {})
        plan = None
        simulation = None
        contract_address = None

        if app_id and app_id in self.js_engine.list_loaded_intents() and plan_data:
            try:
                deployment = self.store.get_deployment(app_id, chain_id=chain_id)
                interactions = [
                    Interaction(
                        target=ix.get("target", ""),
                        value=ix.get("value", "0"),
                        call_data=ix.get("call_data", ""),
                        chain_id=ix.get("chain_id", 1),
                    )
                    for ix in plan_data.get("interactions", [])
                ]
                plan = ExecutionPlan(
                    intent_id=plan_data.get("intent_id", app_id),
                    interactions=interactions,
                    deadline=plan_data.get("deadline", 0),
                    nonce=plan_data.get("nonce", 0),
                    metadata=plan_data.get("metadata", {}),
                )

                # Follower re-simulation: use real Anvil if available.
                #
                # SECURITY (C3): when the operator has opted into full
                # re-simulation (FOLLOWER_PROPOSAL_RESIMULATE=1, the default
                # and the production setting) we must NOT silently fall back
                # to body["simulation"] when our local Anvil is unreachable.
                # That fallback is leader-supplied data and a compromised
                # leader can fabricate it. Reject with SIMULATOR_UNAVAILABLE
                # so the quorum sees this signer drop out rather than rubber-
                # stamp the leader's claim. The leader-sim path is only
                # reachable when the operator explicitly disabled
                # re-simulation by setting the env to 0.
                if resimulate_proposals:
                    simulation = await self._simulate_plan(
                        plan, plan_data, params, deployment,
                        order_id, submitted_by, chain_id, intent_function,
                        deadline, perpetual, max_executions, cooldown,
                    )
                    if simulation is None:
                        logger.warning(
                            "REFUSING TO SIGN: local Anvil unreachable for "
                            "order %s (resimulate=1). Not falling back to "
                            "leader-supplied simulation.", order_id,
                        )
                        return {
                            "approved": False,
                            "reason": (
                                "Local Anvil simulator unreachable; refusing "
                                "to sign on leader-supplied simulation data."
                            ),
                            "reason_code": RejectionCode.SIMULATOR_UNAVAILABLE.value,
                            "status": 503,
                        }

                if simulation is None:
                    # resimulate_proposals is False — operator explicitly
                    # disabled local re-simulation (e.g. hardware can't keep
                    # up). Fall back to leader's simulation result if
                    # included, else a mock. The startup warning at line
                    # 781-796 in main.py covers operators who hit this path.
                    leader_sim = body.get("simulation")
                    if leader_sim and isinstance(leader_sim, dict):
                        simulation = self._build_simulation_from_leader(leader_sim)
                    else:
                        simulation = build_mock_simulation(plan, params)

                state = IntentState(
                    contract_address=deployment.contract_address if deployment else "",
                    chain_id=chain_id,
                    nonce=int(params.get("user_nonce", 0) or 0),
                    owner=submitted_by,
                    raw_params=params,
                    control={"_intent_function": intent_function},
                )
                # Bridge legs: JS swap scoring doesn't apply. Use sim success.
                # Validate bridge flag by checking plan interactions for bridge selectors
                _is_bridge = params.get("_is_bridge_leg") and any(
                    (c.get("callData", "") or "")[2:10] == "81b4e8b4"
                    for c in (
                        plan.interactions
                        if hasattr(plan, "interactions")
                        else [
                            call
                            for call in (
                                plan.get("calls", [])
                                if isinstance(plan, dict)
                                else []
                            )
                        ]
                    )
                )
                if _is_bridge and simulation and simulation.success:
                    local_score = 0.6
                    print(f"[VALIDATOR] bridge leg sim passed, score={local_score}", flush=True)
                else:
                    local_score = await self._score_via_js(app_id, plan, simulation, state)
            except Exception as exc:
                logger.warning("Re-scoring failed, using leader score: %s", exc)

        # ── Platform verification + escrow gate ──────────────────────────
        # Verify cross-chain plans were platform-compiled (not solver-crafted)
        # and that destination legs have escrow on-chain.
        plan_meta = plan_data.get("metadata", {}) if plan_data else {}
        _leg_index = plan_meta.get("leg_index")

        from minotaur_subnet.bridge.verifier import verify_platform_compiled, verify_escrow_on_chain

        # Check 1: Plan structure (no bridge selectors in solver interactions)
        _plan_ok, _plan_reason = verify_platform_compiled(plan_data, params, _leg_index)
        if not _plan_ok:
            return {
                "approved": False,
                "reason": f"Plan verification failed: {_plan_reason}",
                "reason_code": RejectionCode.PLAN_HASH_MISMATCH.value,
            }

        # Check 2: Escrow for destination legs after bridge
        # Detect destination-after-bridge by checking prior legs for bridge calls
        _is_dest_after_bridge = False
        if _leg_index is not None and int(_leg_index) > 0:
            _mlp = plan_meta.get("multi_leg_plan") or params.get("multi_leg_plan") or {}
            _prior_legs = [
                l for l in _mlp.get("forward_legs", [])
                if l.get("leg_index", -1) < int(_leg_index)
            ]
            for _pl in _prior_legs:
                _pl_meta = _pl.get("metadata", {})
                # Platform-compiled bridge legs have type="bridge"
                if _pl_meta.get("type") == "bridge":
                    _is_dest_after_bridge = True
                    break
                # Legacy: check for bridge selectors in interactions
                _pl_ixs = _pl.get("interactions", [])
                if any(
                    (
                        ix.get("call_data", "")[2:10]
                        if ix.get("call_data", "").startswith("0x")
                        else ix.get("call_data", "")[:8]
                    )
                    in ("81b4e8b4",)
                    for ix in _pl_ixs
                ):
                    _is_dest_after_bridge = True
                    break

        # Resolve contract address for this app/chain
        if app_id:
            dep = self.store.get_deployment(app_id, chain_id=chain_id)
            if dep and dep.contract_address:
                contract_address = dep.contract_address

        if _is_dest_after_bridge and _leg_index is not None and contract_address:
            _has_escrow, _escrow_reason = verify_escrow_on_chain(
                contract_address, chain_id, order_id, int(_leg_index),
            )
            if not _has_escrow:
                return {
                    "approved": False,
                    "reason": f"Escrow not deposited for destination leg: {_escrow_reason}",
                    "reason_code": RejectionCode.SIMULATION_FAILED.value,
                }
            print(f"[VALIDATOR] escrow verified for leg {_leg_index}: {_escrow_reason}", flush=True)

        # On-chain score gate for follower (CON-6, CON-7)
        if simulation is not None and simulation.on_chain_score is not None:
            app_def = self.store.get_app(app_id) if app_id else None
            threshold = (
                app_def.config.on_chain_threshold
                if app_def and app_def.config
                else 5000
            )
            if simulation.on_chain_score < threshold:
                return {
                    "approved": False,
                    "reason": (
                        f"On-chain score {simulation.on_chain_score} BPS "
                        f"below threshold {threshold}"
                    ),
                    "reason_code": RejectionCode.ON_CHAIN_SCORE_BELOW_THRESHOLD.value,
                }

        # Check score threshold
        if local_score < score_threshold:
            return {
                "approved": False,
                "reason": f"Score {local_score:.4f} below threshold",
                "reason_code": RejectionCode.SCORE_BELOW_THRESHOLD.value,
            }

        return {
            "approved": True,
            "local_score": local_score,
            "plan": plan,
            "simulation": simulation,
            "contract_address": contract_address,
            "order_id": order_id,
            "plan_hash": plan_hash,
            "chain_id": chain_id,
            "app_id": app_id,
        }
