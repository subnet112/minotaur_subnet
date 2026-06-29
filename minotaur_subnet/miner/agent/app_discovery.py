"""App discovery — HTTP client for validator endpoints.

Fetches available apps, app details, and scores from the validator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Bundles everything the LLM needs to generate a strategy.

    Contains app metadata, Solidity code, ABI, and manifest — but NOT
    the JS scoring code (that stays a black box).
    """
    app_id: str
    name: str
    description: str
    intent_type: str
    supported_chains: list[int] = field(default_factory=list)
    solidity_code: str | None = None
    abi: list | None = None
    manifest: dict | None = None
    config: dict = field(default_factory=dict)
    contract_address: str | None = None


class AppDiscovery:
    """HTTP client for validator app discovery endpoints.

    Args:
        validator_url: Base URL of the validator (e.g., http://localhost:9100).
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        validator_url: str,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = validator_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def fetch_available_apps(self) -> list[dict[str, Any]]:
        """Fetch list of active apps from the API.

        Returns:
            List of app dicts with app_id, name, intent_type, description, config.
        """
        url = f"{self.base_url}/v1/apps/"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning("GET %s returned %d", url, resp.status)
                        return []
                    data = await resp.json()
                    return data.get("apps", [])
        except aiohttp.ClientError as exc:
            logger.warning("Failed to fetch available apps: %s", exc)
            return []

    async def fetch_app_details(self, app_id: str) -> AppContext | None:
        """Fetch full app context for strategy generation.

        Combines data from /v1/apps/{app_id}/status (app metadata + deployment)
        and /v1/apps/{app_id}/manifest (intent functions, params, examples).

        Returns:
            AppContext with Solidity code, ABI, manifest, etc., or None on error.
        """
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                # Fetch status (has app definition + deployment info)
                status_url = f"{self.base_url}/v1/apps/{app_id}/status"
                async with session.get(status_url) as resp:
                    if resp.status != 200:
                        logger.warning("GET %s returned %d", status_url, resp.status)
                        return None
                    status_data = await resp.json()

                # Fetch manifest (has intent functions, example params)
                manifest_url = f"{self.base_url}/v1/apps/{app_id}/manifest"
                manifest = None
                async with session.get(manifest_url) as resp:
                    if resp.status == 200:
                        manifest_data = await resp.json()
                        manifest = manifest_data.get("manifest")

                app_def = status_data.get("app", status_data)
                deployment = status_data.get("deployment", {})
                config = app_def.get("config", {})

                return AppContext(
                    app_id=app_def.get("app_id", app_id),
                    name=app_def.get("name", ""),
                    description=app_def.get("description", ""),
                    intent_type=app_def.get("intent_type", ""),
                    supported_chains=config.get("supported_chains", []),
                    solidity_code=app_def.get("solidity_code"),
                    abi=deployment.get("abi") if isinstance(deployment, dict) else None,
                    manifest=manifest,
                    config=config,
                    contract_address=deployment.get("contract_address") if isinstance(deployment, dict) else None,
                )
        except aiohttp.ClientError as exc:
            logger.warning("Failed to fetch app details for %s: %s", app_id, exc)
            return None

    async def fetch_app_scores(self, app_id: str) -> dict[str, Any]:
        """Fetch execution stats for an app.

        Uses the /v1/apps/{app_id}/status endpoint which returns execution
        statistics including execution_count, avg_score, best_score.

        Returns:
            Stats dict with total_executions, avg_score, best_score, recent_scores.
            Returns empty dict on error.
        """
        url = f"{self.base_url}/v1/apps/{app_id}/status"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning("GET %s returned %d", url, resp.status)
                        return {}
                    data = await resp.json()
                    # Normalize to the format ScoreTracker expects. Post-cutover
                    # avg_score/best_score are on-chain BPS (delivered quality) and
                    # champion_score is null (the champion is the relative baseline);
                    # the authoritative per-submission signal is the relative COUNTS
                    # the loop reads off the submission-status report. ``scoring_mode``
                    # tells the tracker the API is in relative mode.
                    return {
                        "total_executions": data.get("execution_count", 0),
                        "avg_score": data.get("avg_score", 0.0),
                        "best_score": data.get("best_score", 0.0),
                        "recent_scores": data.get("recent_scores", []),
                        "quote_stats": data.get("quote_stats", {}),
                        "champion_score": data.get("champion_score") or 0.0,
                        "scenario_scores": data.get("scenario_scores", {}),
                        "scoring_mode": data.get("scoring_mode", ""),
                    }
        except aiohttp.ClientError as exc:
            logger.warning("Failed to fetch scores for %s: %s", app_id, exc)
            return {}

    async def fetch_current_champion(self) -> dict[str, Any] | None:
        """GET /v1/solver/champion — last adopted champion snapshot.

        Used by the miner's cost gate to decide whether it is the current
        champion (skip iteration) or a challenger (iterate).
        """
        url = f"{self.base_url}/v1/solver/champion"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json()
        except aiohttp.ClientError as exc:
            logger.debug("fetch_current_champion failed: %s", exc)
            return None

    async def fetch_submissions_since(
        self,
        *,
        after: float,
        exclude_miner: str,
    ) -> int:
        """Count submissions created after *after* (unix seconds) from miners
        other than *exclude_miner*.

        Returns 0 on error (the gate fails open at the call site)."""
        if after <= 0:
            # Never submitted before — every other miner's submission counts.
            after = 0.0
        url = f"{self.base_url}/v1/submissions"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return 0
                    data = await resp.json()
                    items = data.get("submissions") if isinstance(data, dict) else data
                    if not isinstance(items, list):
                        return 0
                    count = 0
                    for sub in items:
                        if not isinstance(sub, dict):
                            continue
                        if sub.get("miner_id") == exclude_miner:
                            continue
                        # hotkey is the canonical id in many deployments
                        if sub.get("hotkey") == exclude_miner:
                            continue
                        created = float(sub.get("created_at") or 0)
                        if created > after:
                            count += 1
                    return count
        except (aiohttp.ClientError, ValueError) as exc:
            logger.debug("fetch_submissions_since failed: %s", exc)
            return 0
