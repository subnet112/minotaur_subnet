"""On-chain weights emitter utilities for the Bittensor validator."""
from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from async_substrate_interface.sync_substrate import SubstrateInterface

bt = None  # Lazy-imported bittensor module

U16_MAX = 65535


@dataclass
class _Node:
    node_id: int
    hotkey: str


def _normalize_max_weight(x: np.ndarray, limit: float = 0.1) -> np.ndarray:
    epsilon = 1e-7
    weights = x.copy()
    values = np.sort(weights)
    if x.sum() == 0 or len(x) * limit <= 1:
        return np.ones_like(x) / x.size
    estimation = values / values.sum()
    if estimation.max() <= limit:
        return weights / weights.sum()
    cumsum = np.cumsum(estimation, 0)
    estimation_sum = np.array([(len(values) - i - 1) * estimation[i] for i in range(len(values))])
    n_values = (estimation / (estimation_sum + cumsum + epsilon) < limit).sum()
    cutoff_scale = (limit * cumsum[n_values - 1] - epsilon) / (1 - (limit * (len(estimation) - n_values)))
    cutoff = cutoff_scale * values.sum()
    weights[weights > cutoff] = cutoff
    return weights / weights.sum()


def process_weights_for_netuid(
    uids: np.ndarray,
    weights: np.ndarray,
    netuid: int,
    substrate: SubstrateInterface,
    nodes: Optional[List] = None,
    exclude_quantile: int = 0,
) -> Tuple[List[int], List[float]]:
    if nodes is None:
        raise ValueError("nodes must be provided when processing weights")

    if not isinstance(weights, np.ndarray) or weights.dtype != np.float32:
        weights = weights.astype(np.float32)

    try:
        min_allowed_weights_query = substrate.query("SubtensorModule", "MinAllowedWeights", [netuid])
        max_weight_limit_query = substrate.query("SubtensorModule", "MaxWeightsLimit", [netuid])
        min_allowed_weights = int(min_allowed_weights_query.value) if min_allowed_weights_query else 8
        max_weight_limit = float(max_weight_limit_query.value) / U16_MAX if max_weight_limit_query else 0.1
    except Exception:
        min_allowed_weights = 8
        max_weight_limit = 0.1

    non_zero_weight_idx = np.argwhere(weights > 0).squeeze()
    non_zero_weight_idx = np.atleast_1d(non_zero_weight_idx)
    non_zero_weight_uids = uids[non_zero_weight_idx] if non_zero_weight_idx.size > 0 else np.array([], dtype=int)
    non_zero_weights = weights[non_zero_weight_idx] if non_zero_weight_idx.size > 0 else np.array([], dtype=np.float32)

    if non_zero_weights.size == 0 or len(nodes) < min_allowed_weights:
        final_weights = np.ones(len(nodes)) / max(1, len(nodes))
        processed_weight_uids = np.arange(len(final_weights))
        processed_weights = final_weights
    elif non_zero_weights.size < min_allowed_weights:
        temp_weights = np.ones(len(nodes)) * 1e-5
        temp_weights[non_zero_weight_idx] += non_zero_weights
        processed_weights = _normalize_max_weight(x=temp_weights, limit=max_weight_limit)
        processed_weight_uids = np.arange(len(processed_weights))
    else:
        max_exclude = max(0, len(non_zero_weights) - min_allowed_weights) / len(non_zero_weights)
        quantile = min([(exclude_quantile / U16_MAX), max_exclude])
        lowest_quantile = np.quantile(non_zero_weights, quantile)
        keep = lowest_quantile <= non_zero_weights
        non_zero_weight_uids = non_zero_weight_uids[keep]
        non_zero_weights = non_zero_weights[keep]
        processed_weights = _normalize_max_weight(x=non_zero_weights, limit=max_weight_limit)
        processed_weight_uids = non_zero_weight_uids

    node_weights = processed_weights.astype(float).tolist()
    node_ids = processed_weight_uids.astype(int).tolist()
    s = sum(node_weights)
    if s > 0:
        node_weights = [w / s for w in node_weights]
    return node_ids, node_weights


def _query_version_key(substrate: SubstrateInterface, netuid: int) -> Optional[int]:
    q = substrate.query("SubtensorModule", "WeightsVersionKey", [netuid])
    return None if q is None else int(q.value)


class OnchainWeightsEmitter:
    def __init__(
        self,
        netuid: int,
        validator_wallet_name: str,
        validator_hotkey_name: str,
        subtensor_network: str,
        subtensor_address: str,
        wait_for_finalization: bool = False,
        timeout_seconds: float = 120.0,
        logger: Optional[logging.Logger] = None,
        version_key: Optional[int] = None,
        wallet: Optional[Any] = None,
        subtensor: Optional[Any] = None,
        wallet_path: Optional[str] = None,
    ):
        self.netuid = int(netuid)
        self.wallet_name = validator_wallet_name
        self.hotkey_name = validator_hotkey_name
        self.subtensor_network = subtensor_network
        self.subtensor_address = subtensor_address
        self.wait_for_finalization = wait_for_finalization
        self.timeout_seconds = timeout_seconds
        self.logger = logger or logging.getLogger(__name__)
        self.version_key_override = version_key
        self._wallet: Optional[Any] = wallet
        self._subtensor: Optional[Any] = subtensor
        self.wallet_path = wallet_path

    def _import_bittensor(self):
        global bt
        if bt is not None:
            return bt
        try:
            bt = importlib.import_module("bittensor")
            return bt
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Failed to import bittensor; ensure it is installed and accessible") from exc

    def _resolve_wallet(self):
        if self._wallet is not None:
            return self._wallet

        from bittensor_wallet import Wallet as BtWallet

        wallet_path = (
            self.wallet_path
            or os.getenv("VALIDATOR_WALLET_PATH")
            or os.getenv("BT_WALLET_PATH")
            or os.getenv("WALLET_PATH")
            or os.path.join(os.path.expanduser("~"), ".bittensor", "wallets")
        )
        wallet = BtWallet(name=self.wallet_name, hotkey=self.hotkey_name, path=wallet_path)

        password = (
            os.getenv("VALIDATOR_HOTKEY_PASSWORD")
            or os.getenv("WALLET_HOTKEY_PASSWORD")
            or os.getenv("HOTKEY_PASSWORD")
        )
        wallet.get_hotkey(password=password)
        self._wallet = wallet
        return wallet

    def _resolve_subtensor(self):
        if self._subtensor is not None:
            return self._subtensor

        bt_module = self._import_bittensor()

        # Always use finney for transactions - archive nodes are read-only
        # Even if validator uses archive for historical queries, we need finney to submit weights
        network = self.subtensor_network
        if network == "archive":
            network = "finney"

        subtensor = bt_module.Subtensor(network=network or "finney")

        self._subtensor = subtensor
        return subtensor

    def _fetch_nodes(self, subtensor) -> List[_Node]:
        nodes: List[_Node] = []
        try:
            neurons = subtensor.neurons(self.netuid)
        except Exception as exc:
            self.logger.error(f"Failed to fetch neurons for netuid {self.netuid}: {exc}")
            return nodes

        for neuron in neurons:
            try:
                node_id = int(getattr(neuron, "uid"))
                hotkey = str(getattr(neuron, "hotkey"))
            except (TypeError, ValueError, AttributeError):
                continue
            nodes.append(_Node(node_id=node_id, hotkey=hotkey))
        return nodes

    def _build_scores(self, weights_mapping: Dict[str, float], nodes: List[Any]) -> Tuple[np.ndarray, np.ndarray]:
        scores = np.zeros(len(nodes), dtype=np.float32)
        hotkey_to_idx = {node.hotkey: idx for idx, node in enumerate(nodes)}
        for hotkey, weight in weights_mapping.items():
            idx = hotkey_to_idx.get(str(hotkey))
            if idx is not None:
                scores[idx] = float(weight)
            else:
                self.logger.warning(f"Hotkey {hotkey} not found among active nodes – skipping")
        uids = np.array([node.node_id for node in nodes], dtype=np.int64)
        return uids, scores

    def emit(self, weights_mapping: Dict[str, float]) -> bool:
        try:
            wallet = self._resolve_wallet()
            subtensor = self._resolve_subtensor()
            substrate: SubstrateInterface = subtensor.substrate  # type: ignore[assignment]

            hotkey_ss58 = getattr(getattr(wallet, "hotkey", None), "ss58_address", None)
            if hotkey_ss58 is None:
                keypair = wallet.get_hotkey()
                hotkey_ss58 = getattr(keypair, "ss58_address", None)

            if hotkey_ss58 is None:
                self.logger.error("Validator hotkey missing SS58 address – cannot emit weights")
                return False

            validator_node_id = subtensor.get_uid_for_hotkey_on_subnet(hotkey_ss58, self.netuid)
            if validator_node_id is None:
                self.logger.error("Failed to get validator node ID – aborting weight update")
                return False

            version_key = self.version_key_override if self.version_key_override is not None else _query_version_key(substrate, self.netuid)
            if version_key is None:
                self.logger.warning("Could not fetch WeightsVersionKey; defaulting to 6")
                version_key = 6

            nodes = self._fetch_nodes(subtensor)
            if not nodes:
                self.logger.error("No active nodes found on subnet – cannot emit weights")
                return False

            uids, scores = self._build_scores(weights_mapping, nodes)

            if abs(float(scores.sum()) - 1.0) > 1e-6 and scores.sum() > 0:
                self.logger.warning(f"Sum of input weights is not 1.0: {float(scores.sum())}")

            node_ids, node_weights = process_weights_for_netuid(
                uids=uids,
                weights=scores,
                netuid=self.netuid,
                substrate=substrate,
                nodes=nodes,
                exclude_quantile=int(os.getenv("EXCLUDE_QUANTILE", "0")),
            )

            if not node_ids:
                self.logger.warning("Empty node_ids after processing; skipping set_node_weights")
                return False

            success, message = subtensor.set_weights(
                wallet=wallet,
                netuid=self.netuid,
                uids=node_ids,
                weights=node_weights,
                version_key=int(version_key),
                wait_for_inclusion=True,
                wait_for_finalization=bool(self.wait_for_finalization),
            )

            if not success:
                self.logger.error(f"Error emitting weights via bittensor set_weights: {message}")
            return bool(success)

        except Exception as e:
            self.logger.error(f"Error emitting weights via bittensor: {e}", exc_info=True)
            return False

    async def emit_async(self, weights_mapping: Dict[str, float]) -> bool:
        import asyncio

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.emit, weights_mapping)


