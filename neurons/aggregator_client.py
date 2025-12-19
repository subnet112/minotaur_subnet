"""Async HTTP client for aggregator API.

This module provides the AggregatorClient class for interacting with the aggregator API,
including fetching pending orders, submitting validations, and checking health.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Any, Optional, List, Tuple

import aiohttp


class AggregatorClientError(Exception):
    pass


class AggregatorClient:
    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: int = 10,
        logger: Optional[logging.Logger] = None,
        verify_ssl: bool = True,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        page_limit: int = 500,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.logger = logger or logging.getLogger(__name__)
        self.verify_ssl = verify_ssl
        self.max_retries = max(0, int(max_retries))
        self.backoff_seconds = float(backoff_seconds)
        self.page_limit = max(1, min(int(page_limit), 1000))
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock: Optional[asyncio.Lock] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        # Create lock lazily in async context
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()

        async with self._session_lock:
            if self._session is None or self._session.closed:
                # Create connector
                connector = aiohttp.TCPConnector(ssl=self.verify_ssl)
                # Don't set timeout on session - we'll pass it per request as numeric value
                # This avoids ClientTimeout context manager issues
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    connector_owner=True
                )
            return self._session

    async def close(self) -> None:
        if self._session_lock is not None:
            async with self._session_lock:
                if self._session is not None:
                    await self._session.close()
                    self._session = None

    async def _request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        headers: Dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        attempt = 0
        while True:
            attempt += 1
            try:
                # Create a fresh session for each request to avoid timeout context manager issues
                # This ensures the timeout is created in the same async context where it's used
                connector = aiohttp.TCPConnector(ssl=self.verify_ssl)
                timeout_obj = aiohttp.ClientTimeout(total=self.timeout)
                
                async with aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout_obj
                ) as session:
                    request_kwargs = {"headers": headers}
                    if method == "GET" and params:
                        request_kwargs["params"] = params
                    elif method == "POST" and json:
                        request_kwargs["json"] = json
                        headers["Content-Type"] = "application/json"

                    async with session.request(method, url, **request_kwargs) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            raise AggregatorClientError(f"HTTP {resp.status}: {text}")
                        try:
                            return await resp.json()
                        except Exception as json_err:
                            # If JSON parsing fails, log the raw response
                            text = await resp.text()
                            self.logger.error(f"Failed to parse JSON response from {url}: {json_err}, response: {text[:200]}")
                            raise AggregatorClientError(f"Invalid JSON response: {json_err}") from json_err
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.logger.error(f"Aggregator request error ({url}): {e}")
                if attempt > self.max_retries:
                    raise AggregatorClientError(str(e)) from e
                await asyncio.sleep(self.backoff_seconds * attempt)
            except AggregatorClientError:
                raise
            except Exception as e:  # unexpected
                self.logger.error(f"Unexpected aggregator client error ({url}): {e}")
                raise AggregatorClientError(str(e)) from e

    async def fetch_pending_orders(self, validator_id: str) -> List[Dict[str, Any]]:
        """Fetch orders awaiting validation in paper trading mode.

        Filters out orders that this validator has already validated by passing
        the validator_id as a query parameter.
        """
        try:
            params = {"validator_id": validator_id}
            data = await self._request("/v1/validators/orders", method="GET", params=params)
            return data if isinstance(data, list) else []
        except AggregatorClientError as e:
            self.logger.error(f"Failed to fetch pending orders: {e}")
            return []

    async def submit_validation(
        self,
        order_id: str,
        validator_id: str,
        success: bool,
        notes: str = ""
    ) -> bool:
        """Submit validation result for an order."""
        payload = {
            "orderId": order_id,
            "validatorId": validator_id,
            "success": success,
        }
        if notes:
            payload["notes"] = notes

        try:
            await self._request("/v1/validators/validate", method="POST", json=payload)
            return True
        except AggregatorClientError as e:
            self.logger.error(f"Failed to submit validation for {order_id}: {e}")
            return False

    async def fetch_health(self) -> Optional[Dict[str, Any]]:
        """Fetch aggregator health status."""
        try:
            data = await self._request("/health", method="GET")
            if isinstance(data, dict):
                return data
            else:
                self.logger.warning(f"Health endpoint returned non-dict data: {type(data)} - {data}")
                return None
        except AggregatorClientError as e:
            self.logger.warning(f"Failed to fetch aggregator health: {e}")
            return None
        except Exception as e:
            self.logger.warning(f"Unexpected error fetching aggregator health: {e}")
            return None

    def _build_canonical_weights_payload(
        self,
        validator_id: str,
        epoch_key: str,
        timestamp: str,
        block_number: Optional[int],
        weights: Dict[str, float],
        stats: Dict[str, Any]
    ) -> str:
        """Build canonical payload string for weight signature.
        
        Format matches server expectations:
        validator-weights\n
        <validatorId>\n
        <epochKey>\n
        <timestamp>\n
        <blockNumber or "">\n
        <sorted_miner_keys>:<sorted_weight_values>\n
        <totalSimulations>\n
        <validMiners>\n
        <totalMiners>\n
        <burnPercentage>
        
        Note: Keys and values are separated by commas, then joined with ':'
        Example: "miner1,miner2:0.5,0.3"
        """
        def format_decimal(value: float) -> str:
            """Format float with trailing zeros removed."""
            s = f"{value:.12f}".rstrip('0').rstrip('.')
            return s if s else "0"
        
        # Sort weights by miner_hotkey for deterministic ordering
        sorted_weights = sorted(weights.items())
        
        # Build keys and values lines separately
        keys_line = ",".join(k for k, _ in sorted_weights)
        values_line = ",".join(format_decimal(v) for _, v in sorted_weights)
        
        # Join keys and values with ':'
        # Even if empty, we need ':' to maintain the format
        weights_line = f"{keys_line}:{values_line}" if keys_line else ":"
        
        block_line = str(block_number) if block_number is not None else ""
        
        lines = [
            "validator-weights",
            validator_id,
            epoch_key,
            timestamp,
            block_line,
            weights_line,
            str(stats.get("totalSimulations", 0)),
            str(stats.get("validMiners", 0)),
            str(stats.get("totalMiners", 0)),
            format_decimal(stats.get("burnPercentage", 0.0))
        ]
        
        return "\n".join(lines)

    async def submit_weights(
        self,
        validator_id: str,
        epoch_key: str,
        weights: Dict[str, float],
        stats: Dict[str, Any],
        timestamp: str,
        signature: str,
        signature_type: str = "sr25519",
        block_number: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Submit validator weights to aggregator.
        
        Args:
            validator_id: Validator's SS58 hotkey address
            epoch_key: Unique epoch identifier
            weights: Map of miner hotkey (SS58) to normalized weight (0.0-1.0)
            stats: Summary statistics dict with totalSimulations, validMiners, totalMiners, burnPercentage, weightsSum
            timestamp: ISO8601 UTC timestamp
            signature: Hex-encoded signature with 0x prefix
            signature_type: "sr25519" or "ed25519"
            block_number: Optional block number at epoch end
            
        Returns:
            Response dict with submission ID, or None on error
        """
        # Log what we're about to send
        self.logger.info(
            f"📨 Submitting weights to aggregator:\n"
            f"   validator_id: {validator_id}\n"
            f"   epoch_key: {epoch_key}\n"
            f"   weights count: {len(weights)}\n"
            f"   signature: {signature[:50]}... (length: {len(signature)})\n"
            f"   signature_type: {signature_type}"
        )
        
        payload = {
            "validatorId": validator_id,
            "epochKey": epoch_key,
            "timestamp": timestamp,
            "weights": weights,
            "stats": stats,
            "signature": signature,
            "signatureType": signature_type
        }
        
        if block_number is not None:
            payload["blockNumber"] = block_number
        
        try:
            return await self._request("/v1/validators/weights", method="POST", json=payload)
        except AggregatorClientError as e:
            self.logger.error(f"Failed to submit weights: {e}")
            # Log the full error details for debugging
            error_str = str(e)
            if "INVALID_SIGNATURE" in error_str:
                self.logger.error(
                    f"🔍 Signature verification failed. Details:\n"
                    f"   validator_id sent: {validator_id}\n"
                    f"   signature sent: {signature}\n"
                    f"   signature length: {len(signature)} chars ({len(signature) - 2} hex chars)\n"
                    f"   weights: {weights}\n"
                    f"   stats: {stats}"
                )
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error submitting weights: {e}", exc_info=True)
            return None


