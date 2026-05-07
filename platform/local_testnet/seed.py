"""Seed script — deploys DexAggregatorApp through the Minotaur API pipeline.

Runs once after the API is healthy.  Reads the Solidity and JS source from
the contracts directory, creates the app intent via API, triggers deployment,
and writes the resulting app ID and contract address to /config/testnet.env.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [seed] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

API_URL = os.environ.get("MINOTAUR_API_URL", "http://api:8080")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/testnet.env")
CONTRACTS_DIR = os.path.join(os.path.dirname(__file__), "../../contracts")

# Relayer address (Anvil account 0)
RELAYER_ADDRESS = os.environ.get(
    "RELAYER_ADDRESS",
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
)
FEE_BPS = os.environ.get("FEE_BPS", "5000")


def wait_for_api(timeout: int = 120) -> None:
    """Poll the API health endpoint until it responds."""
    logger.info("Waiting for API at %s ...", API_URL)
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(f"{API_URL}/health", timeout=5)
            if resp.status == 200:
                logger.info("API is healthy")
                return
        except Exception:
            pass
        time.sleep(2)

    raise RuntimeError(f"API not ready after {timeout}s")


def read_source(relative_path: str) -> str:
    """Read a source file from the contracts directory."""
    path = os.path.join(CONTRACTS_DIR, relative_path)
    with open(path) as f:
        return f.read()


def api_get(path: str) -> dict:
    """GET from the API and return the response dict."""
    url = f"{API_URL}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode() if exc.fp else str(exc)
        raise RuntimeError(f"API {exc.code} on GET {path}: {detail}") from exc


def api_post(path: str, data: dict) -> dict:
    """POST JSON to the API and return the response dict."""
    url = f"{API_URL}{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode() if exc.fp else str(exc)
        raise RuntimeError(f"API {exc.code} on POST {path}: {detail}") from exc


def append_config(lines: list[str]) -> None:
    """Append lines to the testnet config file."""
    with open(CONFIG_PATH, "a") as f:
        f.write("\n")
        for line in lines:
            f.write(line + "\n")
    logger.info("Appended to %s: %s", CONFIG_PATH, lines)


def find_existing_app(name: str) -> dict | None:
    """Check if an app with the given name already exists."""
    resp = api_get("/v1/apps/")
    apps = resp if isinstance(resp, list) else resp.get("apps", [])
    for app in apps:
        if app.get("name") == name:
            return app
    return None


def main() -> None:
    logger.info("=" * 60)
    logger.info("Minotaur Local Testnet — Seed")
    logger.info("=" * 60)

    wait_for_api()

    # Read source files
    solidity_code = read_source("src/DexAggregatorApp.sol")
    js_code = read_source("src/dex_aggregator_scoring.js")
    logger.info(
        "Loaded sources: DexAggregatorApp.sol (%d bytes), "
        "dex_aggregator_scoring.js (%d bytes)",
        len(solidity_code),
        len(js_code),
    )

    # Check for existing app (idempotency)
    existing = find_existing_app("DexAggregatorApp")

    if existing and existing.get("contract_address"):
        app_id = existing["app_id"]
        contract_address = existing["contract_address"]
        logger.info(
            "DexAggregatorApp already deployed: app_id=%s address=%s — skipping",
            app_id,
            contract_address,
        )
        append_config([
            f"DEX_APP_ID={app_id}",
            f"DEX_APP_ADDRESS={contract_address}",
        ])
        logger.info("=" * 60)
        logger.info("Seed complete — DexAggregatorApp already live!")
        logger.info("=" * 60)
        return

    if existing:
        # Draft app exists but not yet deployed — deploy it
        app_id = existing["app_id"]
        logger.info("DexAggregatorApp exists in draft: %s — deploying", app_id)
    else:
        # Create new app intent via API
        create_resp = api_post("/v1/apps/", {
            "name": "DexAggregatorApp",
            "description": "DEX aggregation via App Intent — multi-DEX routing with positive slippage fee capture",
            "supported_chains": [31337, 8453],
            "js_code": js_code,
            "solidity_code": solidity_code,
            "constructor_args": [
                ["address", RELAYER_ADDRESS],
                ["uint256", FEE_BPS],
            ],
        })

        if create_resp.get("error"):
            raise RuntimeError(f"create_app_intent failed: {create_resp['error']}")

        app_id = create_resp["app_id"]
        logger.info("DexAggregatorApp created: %s", app_id)

    # Deploy via API
    deploy_resp = api_post(f"/v1/apps/{app_id}/deploy", {})

    if deploy_resp.get("error"):
        raise RuntimeError(f"deploy_app_intent failed: {deploy_resp['error']}")

    contract_address = deploy_resp.get("contract_address", "")
    status = deploy_resp.get("status", "unknown")
    logger.info(
        "DexAggregatorApp deployed: address=%s status=%s",
        contract_address,
        status,
    )

    # Write to config
    append_config([
        f"DEX_APP_ID={app_id}",
        f"DEX_APP_ADDRESS={contract_address}",
    ])

    logger.info("=" * 60)
    logger.info("Seed complete — DexAggregatorApp is live!")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.error("Seed failed: %s", exc, exc_info=True)
        sys.exit(1)
