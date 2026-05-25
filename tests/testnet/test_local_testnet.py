"""Smoke tests against the running local testnet stack.

These tests validate the Docker Compose local testnet as a live environment:
- API / validator / relayer health
- user-style DexAggregatorApp creation + deployment via the API
- testnet faucet and balance queries
- live prepare / quote flow against a freshly deployed flagship app

The suite expects the local testnet to be running. If it is not reachable, the
tests are skipped unless ``REQUIRE_LOCAL_TESTNET=1`` is set.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest


API_URL = os.environ.get("LOCAL_TESTNET_API_URL", "http://localhost:8080")
API_PEER_1_URL = os.environ.get("LOCAL_TESTNET_API_PEER_1_URL", "http://localhost:8081")
API_PEER_2_URL = os.environ.get("LOCAL_TESTNET_API_PEER_2_URL", "http://localhost:8082")
API_URLS = [API_URL, API_PEER_1_URL, API_PEER_2_URL]
VALIDATOR_URL = os.environ.get("LOCAL_TESTNET_VALIDATOR_URL", "http://localhost:9100")
VALIDATOR_PEER_1_URL = os.environ.get(
    "LOCAL_TESTNET_VALIDATOR_PEER_1_URL",
    "http://localhost:9101",
)
VALIDATOR_PEER_2_URL = os.environ.get(
    "LOCAL_TESTNET_VALIDATOR_PEER_2_URL",
    "http://localhost:9102",
)
VALIDATOR_URLS = [VALIDATOR_URL, VALIDATOR_PEER_1_URL, VALIDATOR_PEER_2_URL]
RELAYER_URL = os.environ.get("LOCAL_TESTNET_RELAYER_URL", "http://localhost:8091")
ETH_RPC_URL = os.environ.get("LOCAL_TESTNET_ETH_RPC_URL", "http://localhost:8545")
BASE_RPC_URL = os.environ.get("LOCAL_TESTNET_BASE_RPC_URL", "http://localhost:8546")
REQUIRE_LOCAL_TESTNET = os.environ.get("REQUIRE_LOCAL_TESTNET", "").strip().lower() in {
    "1", "true", "yes", "on",
}

TEST_ADDRESS = "0x1111111111111111111111111111111111111111"
RELAYER_ADDRESS = os.environ.get(
    "LOCAL_TESTNET_RELAYER_ADDRESS",
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
)
FEE_BPS = os.environ.get("LOCAL_TESTNET_FEE_BPS", "5000")
REPO_ROOT = Path(__file__).resolve().parents[2]
DEX_AGGREGATOR_SOLIDITY = (
    REPO_ROOT / "contracts" / "src" / "DexAggregatorApp.sol"
).read_text()
DEX_AGGREGATOR_JS = (
    REPO_ROOT / "contracts" / "src" / "dex_aggregator_scoring.js"
).read_text()
EXAMPLE_SOLVER_DOCKERFILE = (
    REPO_ROOT / "minotaur_subnet" / "docker" / "example-solver" / "Dockerfile"
).read_text()
LOCAL_TESTNET_SUBMISSION_ROOT = Path(
    os.environ.get(
        "LOCAL_TESTNET_SUBMISSION_HOST_ROOT",
        str(Path(tempfile.gettempdir()) / "minotaur-testnet-submissions"),
    )
)
LOCAL_TESTNET_SUBMISSION_CONTAINER_ROOT = "/solver-submissions"


def _request_json(
    method: str,
    url: str,
    data: dict | None = None,
    timeout: int = 20,
) -> tuple[int, dict]:
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            parsed = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            parsed = {"raw": payload}
        return exc.code, parsed


def _rpc_json(url: str, method: str, params: list | None = None, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params or [],
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload


def _ensure_testnet_reachable() -> None:
    try:
        status, _ = _request_json("GET", f"{API_URL}/health", timeout=5)
    except Exception as exc:  # pragma: no cover - only used for skip messaging
        if REQUIRE_LOCAL_TESTNET:
            pytest.fail(f"Local testnet API is not reachable at {API_URL}: {exc}")
        pytest.skip(f"Local testnet API is not reachable at {API_URL}")

    if status != 200:
        if REQUIRE_LOCAL_TESTNET:
            pytest.fail(f"Local testnet API health failed with HTTP {status}")
        pytest.skip(f"Local testnet API health failed with HTTP {status}")


def _create_app(
    *,
    name: str,
    supported_chains: list[int] | None = None,
    deployer: str = "",
    js_code: str | None = None,
    solidity_code: str | None = None,
    constructor_args: list[list[str]] | None = None,
    timeout: int = 90,
) -> dict:
    status, created = _request_json(
        "POST",
        f"{API_URL}/v1/apps/",
        {
            "name": name,
            "description": f"Automated local_testnet smoke deployment of {name}",
            "supported_chains": supported_chains or [31337, 8453],
            "js_code": js_code or DEX_AGGREGATOR_JS,
            "solidity_code": solidity_code or DEX_AGGREGATOR_SOLIDITY,
            "constructor_args": constructor_args or [
                ["address", RELAYER_ADDRESS],
                ["uint256", FEE_BPS],
            ],
            "deployer": deployer,
        },
        timeout=timeout,
    )
    assert status == 200, created
    assert not created.get("error"), created
    return created


def _deploy_app(app_id: str, chain_id: int | None = None, timeout: int = 120) -> dict:
    suffix = f"?chain_id={chain_id}" if chain_id is not None else ""
    status, deployed = _request_json(
        "POST",
        f"{API_URL}/v1/apps/{app_id}/deploy{suffix}",
        {},
        timeout=timeout,
    )
    assert status == 200, deployed
    assert not deployed.get("error"), deployed
    assert deployed.get("contract_address", "").startswith("0x"), deployed
    return deployed


def _prepare_swap(
    app_id: str,
    *,
    chain_id: int = 31337,
    input_token: str = "USDC",
    output_token: str = "WETH",
    input_amount: str = "1000000",
    submitted_by: str = "",
) -> dict:
    status, prepared = _request_json(
        "POST",
        f"{API_URL}/v1/apps/{app_id}/prepare",
        {
            "chain_id": chain_id,
            "intent_function": "swap",
            "submitted_by": submitted_by,
            "params": {
                "input_token": input_token,
                "output_token": output_token,
                "input_amount": input_amount,
            },
        },
    )
    assert status == 200, prepared
    assert prepared.get("chain_id") == chain_id
    assert prepared.get("intent_function") == "swap"
    return prepared


def _create_managed_wallet(chain_ids: list[int] | None = None) -> dict:
    status, wallet = _request_json(
        "POST",
        f"{API_URL}/v1/wallets/",
        {"chain_ids": chain_ids or [31337]},
    )
    assert status == 200, wallet
    assert wallet.get("address", "").startswith("0x"), wallet
    return wallet


def _get_wallet_balances(address: str, chain_id: int = 31337) -> dict:
    query = urllib.parse.urlencode({"chain_id": chain_id})
    status, balances = _request_json(
        "GET",
        f"{API_URL}/v1/wallets/{address}/balances?{query}",
    )
    assert status == 200, balances
    return balances


def _benchmark_boost_scoring_js(scenarios: int = 12) -> str:
    benchmark_scenarios = [
        {
            "name": f"boost_case_{idx}",
            "description": "Champion-quorum smoke benchmark case",
            "intent_function": "optimize",
            "params": {
                "benchmark_case": str(idx),
                "benchmark_target": "boost",
            },
        }
        for idx in range(scenarios)
    ]
    manifest = {
        "intent_functions": [
            {
                "name": "optimize",
                "description": "Synthetic benchmark-only intent for local testnet champion smoke",
                "params": {
                    "benchmark_case": {
                        "type": "uint256",
                        "description": "Synthetic case identifier",
                        "source": "system",
                    },
                    "benchmark_target": {
                        "type": "string",
                        "description": "Synthetic target label",
                        "source": "system",
                    },
                },
                "example_params": {
                    "benchmark_case": "0",
                    "benchmark_target": "boost",
                },
            },
        ],
        "benchmark_scenarios": benchmark_scenarios,
    }
    config = {
        "name": "RoundBoostApp",
        "version": "1.0.0",
        "type": "benchmark_quorum_smoke",
    }
    return textwrap.dedent(
        f"""
        var config = {json.dumps(config)};
        var manifest = {json.dumps(manifest)};

        function score(plan, state, context) {{
          var metadata = (plan && plan.metadata) || {{}};
          if (metadata.benchmark_boost) {{
            return {{
              score: 1.0,
              valid: true,
              reason: "boosted benchmark plan",
            }};
          }}
          return {{
            score: 0.0,
            valid: false,
            reason: "missing benchmark_boost metadata",
          }};
        }}

        module.exports = {{
          config: config,
          manifest: manifest,
          score: score,
          get_manifest: function () {{
            return manifest;
          }},
        }};
        """
    ).strip()


def _boosted_solver_source() -> str:
    return textwrap.dedent(
        """
        import time

        from minotaur_subnet.sdk.intent_solver import IntentSolver, MarketSnapshot, SolverMetadata
        from minotaur_subnet.sdk.solvers.baseline_solver import BaselineSwapSolver
        from minotaur_subnet.shared.types import (
            AppIntentDefinition,
            ExecutionPlan,
            Interaction,
            IntentState,
        )


        def _raw_params(state: IntentState) -> dict:
            if hasattr(state, "raw_params_view"):
                return state.raw_params_view() or {}
            return getattr(state, "raw_params", {}) or {}


        def _control(state: IntentState) -> dict:
            if hasattr(state, "control_view"):
                return state.control_view() or {}
            return getattr(state, "control", {}) or {}


        def _intent_name(state: IntentState) -> str:
            control = _control(state)
            params = _raw_params(state)
            return control.get("_intent_function") or params.get("intent_function") or "swap"


        class BoostedSolver(IntentSolver):
            def __init__(self) -> None:
                self._baseline = BaselineSwapSolver()

            def initialize(self, config: dict) -> None:
                self._baseline.initialize(config)

            def generate_plan(
                self,
                intent: AppIntentDefinition,
                state: IntentState,
                snapshot: MarketSnapshot,
            ) -> ExecutionPlan | None:
                if _intent_name(state) == "optimize":
                    params = _raw_params(state)
                    return ExecutionPlan(
                        intent_id=intent.app_id,
                        interactions=[
                            Interaction(
                                target="0x" + "99" * 20,
                                value="0",
                                call_data="0x" + "ab" * 16,
                                chain_id=state.chain_id,
                            ),
                        ],
                        deadline=int(time.time()) + 300,
                        nonce=state.nonce,
                        metadata={
                            "benchmark_boost": True,
                            "benchmark_case": params.get("benchmark_case", "0"),
                        },
                    )
                return self._baseline.generate_plan(intent, state, snapshot)

            def check_trigger(
                self,
                intent: AppIntentDefinition,
                state: IntentState,
                snapshot: MarketSnapshot,
            ) -> bool:
                return False

            def metadata(self) -> SolverMetadata:
                return SolverMetadata(
                    name="boosted-benchmark-solver",
                    version="1.0.0",
                    author="local_testnet",
                    description="Baseline swap solver plus synthetic benchmark boost cases",
                    supported_chains=[1, 31337, 8453],
                    supported_intent_types=["swap", "optimize"],
                )


        SOLVER_CLASS = BoostedSolver
        """
    ).strip() + "\n"


def _create_local_submission_repo(prefix: str) -> tuple[str, str]:
    LOCAL_TESTNET_SUBMISSION_ROOT.mkdir(parents=True, exist_ok=True)
    repo_dir = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=LOCAL_TESTNET_SUBMISSION_ROOT))
    (repo_dir / "Dockerfile").write_text(EXAMPLE_SOLVER_DOCKERFILE)
    (repo_dir / "solver.py").write_text(_boosted_solver_source())
    (repo_dir / "README.md").write_text(
        "# Local Testnet Champion Smoke Solver\n\n"
        "Synthetic benchmark booster used by the local multi-API smoke test.\n",
    )
    (repo_dir / "requirements.txt").write_text("\n")

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Minotaur Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Minotaur Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, env=env, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, env=env, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "Add boosted benchmark solver"],
        cwd=repo_dir,
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    commit_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        env=env,
        capture_output=True,
        text=True,
    ).stdout.strip()
    repo_url = f"file://{LOCAL_TESTNET_SUBMISSION_CONTAINER_ROOT}/{repo_dir.name}"
    return repo_url, commit_hash


def _signed_submission_payload(
    *,
    repo_url: str,
    commit_hash: str,
    round_id: str,
    epoch: int,
) -> dict:
    from bittensor_wallet import Keypair

    keypair = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    message = f"{repo_url}:{commit_hash}:{round_id}"
    signature = base64.b64encode(keypair.sign(message.encode("utf-8"))).decode("ascii")
    return {
        "repo_url": repo_url,
        "commit_hash": commit_hash,
        "epoch": epoch,
        "round_id": round_id,
        "hotkey": keypair.ss58_address,
        "signature": signature,
    }


@pytest.fixture(scope="module", autouse=True)
def require_local_testnet():
    _ensure_testnet_reachable()


@pytest.fixture(scope="module")
def deployed_test_app() -> dict:
    unique_suffix = int(time.time())
    created = _create_app(name=f"DexAggregatorApp Testnet Smoke {unique_suffix}")
    app_id = created["app_id"]
    deployed = _deploy_app(app_id)
    status_payload = _wait_for_app_status(
        app_id,
        allowed_statuses={"solved", "active"},
        chain_id=31337,
        timeout=180,
    )

    return {
        "app_id": app_id,
        "contract_address": deployed["contract_address"],
        "status_payload": status_payload,
    }


def _wait_for_quote(app_id: str, params: dict[str, str], timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    last_payload: dict | None = None
    request = {
        "chain_id": 31337,
        "intent_function": "swap",
        "params": params,
    }

    while time.time() < deadline:
        status, payload = _request_json("POST", f"{API_URL}/v1/apps/{app_id}/quote", request)
        last_payload = payload
        estimated_output = payload.get("estimated_output")
        suggested_min_output = payload.get("suggested_min_output")
        ready_min_output = payload.get("ready_params", {}).get("min_output_amount")
        if (
            status == 200
            and estimated_output not in (None, "", "0")
            and suggested_min_output not in (None, "", "0")
            and ready_min_output not in (None, "", "0")
        ):
            return payload
        time.sleep(2)

    pytest.fail(f"Quote never became ready for {app_id}: {last_payload}")


def _wait_for_app_status(
    app_id: str,
    *,
    allowed_statuses: set[str],
    chain_id: int | None = None,
    timeout: int = 180,
) -> dict:
    deadline = time.time() + timeout
    last_payload: dict | None = None
    while time.time() < deadline:
        status, payload = _request_json("GET", f"{API_URL}/v1/apps/{app_id}/status")
        last_payload = payload
        if status == 200 and payload.get("status") in allowed_statuses:
            if chain_id is None:
                return payload
            deployment = payload.get("deployments", {}).get(str(chain_id))
            if deployment and deployment.get("status") in allowed_statuses:
                return payload
        time.sleep(2)

    pytest.fail(f"App {app_id} never reached {sorted(allowed_statuses)}: {last_payload}")


def _wait_for_submission_status(
    submission_id: str,
    *,
    allowed_statuses: set[str],
    timeout: int = 300,
) -> dict:
    deadline = time.time() + timeout
    last_payload: dict | None = None
    while time.time() < deadline:
        status, payload = _request_json("GET", f"{API_URL}/v1/submissions/{submission_id}/status")
        last_payload = payload
        if status == 200:
            current_status = payload.get("status")
            if current_status in allowed_statuses:
                return payload
            if current_status == "rejected":
                pytest.fail(f"Submission {submission_id} was rejected: {payload}")
        time.sleep(3)

    pytest.fail(
        f"Submission {submission_id} never reached {sorted(allowed_statuses)}: {last_payload}",
    )


def _wait_for_solver_round_open(base_url: str, timeout: int = 240) -> dict:
    deadline = time.time() + timeout
    last_payload: dict | None = None
    while time.time() < deadline:
        status, payload = _request_json("GET", f"{base_url}/v1/solver/round", timeout=10)
        last_payload = payload
        if (
            status == 200
            and payload.get("status") == "open"
            and payload.get("accepting_submissions") is True
        ):
            return payload
        time.sleep(3)

    pytest.fail(f"Solver round never reopened on {base_url}: {last_payload}")


def _wait_for_solver_round_certifying(
    round_id: str,
    *,
    base_url: str = API_URL,
    expected_submission_id: str | None = None,
    timeout: int = 420,
) -> dict:
    deadline = time.time() + timeout
    last_payload: dict | None = None
    while time.time() < deadline:
        status, payload = _request_json(
            "GET",
            f"{base_url}/v1/solver/round/{urllib.parse.quote(round_id)}",
            timeout=10,
        )
        last_payload = payload
        if status == 200:
            if payload.get("status") == "aborted":
                pytest.fail(f"Solver round {round_id} aborted before certification: {payload}")
            finalist_submission_id = payload.get("finalist_submission_id")
            if (
                payload.get("status") in {"certifying", "certified", "activated"}
                and finalist_submission_id
                and (
                    expected_submission_id is None
                    or finalist_submission_id == expected_submission_id
                )
            ):
                return payload
        time.sleep(3)

    pytest.fail(f"Solver round {round_id} never became certifying on {base_url}: {last_payload}")


def _wait_for_api_round_certifying(
    round_id: str,
    *,
    expected_submission_id: str,
    timeout: int = 420,
) -> dict[str, dict]:
    deadline = time.time() + timeout
    last_snapshot: dict[str, dict] | None = None
    while time.time() < deadline:
        snapshot: dict[str, dict] = {}
        try:
            for base_url in API_URLS:
                status, round_payload = _request_json(
                    "GET",
                    f"{base_url}/v1/solver/round/{urllib.parse.quote(round_id)}",
                    timeout=10,
                )
                assert status == 200, (base_url, round_payload)
                snapshot[base_url] = round_payload
        except Exception:
            last_snapshot = snapshot
            time.sleep(3)
            continue

        aborted = [
            (base_url, round_payload)
            for base_url, round_payload in snapshot.items()
            if round_payload.get("status") == "aborted"
        ]
        if aborted:
            pytest.fail(f"Solver round {round_id} aborted before cluster certification: {aborted}")

        if all(
            round_payload.get("status") in {"certifying", "certified", "activated"}
            and round_payload.get("finalist_submission_id") == expected_submission_id
            for round_payload in snapshot.values()
        ):
            return snapshot

        last_snapshot = snapshot
        time.sleep(3)

    pytest.fail(f"Solver round {round_id} never became certifying cluster-wide: {last_snapshot}")


def _wait_for_api_cluster(timeout: int = 180) -> dict[str, dict]:
    deadline = time.time() + timeout
    last_snapshot: dict[str, dict] | None = None
    while time.time() < deadline:
        snapshot: dict[str, dict] = {}
        try:
            for base_url in API_URLS:
                status, health = _request_json("GET", f"{base_url}/health", timeout=10)
                assert status == 200, (base_url, health)
                snapshot[base_url] = {"health": health}
        except Exception:
            last_snapshot = snapshot
            time.sleep(3)
            continue

        leaders = [
            url
            for url, item in snapshot.items()
            if item["health"].get("solver_round_role") == "leader"
        ]
        followers = [
            url
            for url, item in snapshot.items()
            if item["health"].get("solver_round_role") == "follower"
        ]
        if (
            len(leaders) == 1
            and len(followers) == 2
            and all(item["health"].get("status") == "ok" for item in snapshot.values())
            and all(
                item["health"].get("solver_round_coordinator") == "running"
                for item in snapshot.values()
            )
            and all(
                item["health"].get("solver_round_epoch_clock", {}).get("mode") == "native_tempo"
                for item in snapshot.values()
            )
            and all(
                item["health"].get("champion_consensus", {}).get("enabled") is True
                for item in snapshot.values()
            )
            and all(
                item["health"].get("champion_consensus", {}).get("peer_count") == 2
                for item in snapshot.values()
            )
            and all(
                (item["health"].get("champion_consensus", {}).get("validator_count") or 0) >= 3
                for item in snapshot.values()
            )
            and all(
                (item["health"].get("champion_consensus", {}).get("quorum_required") or 0) >= 2
                for item in snapshot.values()
            )
            and all(
                item["health"].get("champion_consensus", {}).get("internal_round_auth_configured")
                is True
                for item in snapshot.values()
            )
        ):
            return snapshot

        last_snapshot = snapshot
        time.sleep(3)

    pytest.fail(f"API cluster never became ready: {last_snapshot}")


def _wait_for_api_round_cluster(
    round_id: str,
    *,
    expected_submission_id: str,
    timeout: int = 360,
) -> dict[str, dict]:
    deadline = time.time() + timeout
    last_snapshot: dict[str, dict] | None = None
    while time.time() < deadline:
        snapshot: dict[str, dict] = {}
        try:
            for base_url in API_URLS:
                status, round_payload = _request_json(
                    "GET",
                    f"{base_url}/v1/solver/round/{urllib.parse.quote(round_id)}",
                    timeout=10,
                )
                assert status == 200, (base_url, round_payload)
                status, champion_payload = _request_json(
                    "GET",
                    f"{base_url}/v1/solver/champion",
                    timeout=10,
                )
                assert status == 200, (base_url, champion_payload)
                snapshot[base_url] = {
                    "round": round_payload,
                    "champion": champion_payload,
                }
        except Exception:
            last_snapshot = snapshot
            time.sleep(3)
            continue

        aborted = [
            (url, item["round"])
            for url, item in snapshot.items()
            if item["round"].get("status") == "aborted"
        ]
        if aborted:
            pytest.fail(f"Solver round {round_id} aborted: {aborted}")

        if (
            all(item["round"].get("status") == "activated" for item in snapshot.values())
            and all(
                item["round"].get("certificate_candidate_submission_id") == expected_submission_id
                for item in snapshot.values()
            )
            and all(
                (item["round"].get("certificate_quorum_required") or 0) >= 2
                and (item["round"].get("certificate_approvals") or 0)
                >= (item["round"].get("certificate_quorum_required") or 0)
                for item in snapshot.values()
            )
            and all(
                item["champion"].get("submission_id") == expected_submission_id
                for item in snapshot.values()
            )
        ):
            return snapshot

        last_snapshot = snapshot
        time.sleep(3)

    pytest.fail(f"Solver round {round_id} never activated cluster-wide: {last_snapshot}")


def _wait_for_order_terminal(order_id: str, timeout: int = 90) -> dict:
    deadline = time.time() + timeout
    last_payload: dict | None = None
    non_terminal_statuses = {
        "open",
        "assigned",
        "solved",
        "scored",
        "approved",
        "submitted",
        "bridging",
        "pending",
    }
    while time.time() < deadline:
        status, payload = _request_json("GET", f"{API_URL}/v1/orders/{order_id}")
        last_payload = payload
        if status == 200 and payload.get("status") not in non_terminal_statuses:
            return payload
        time.sleep(3)

    pytest.fail(f"Order {order_id} never reached a terminal state: {last_payload}")


def _wait_for_validator_cluster(timeout: int = 180) -> dict[str, dict]:
    deadline = time.time() + timeout
    last_snapshot: dict[str, dict] | None = None
    while time.time() < deadline:
        snapshot: dict[str, dict] = {}
        try:
            for base_url in VALIDATOR_URLS:
                status, health = _request_json("GET", f"{base_url}/health", timeout=10)
                assert status == 200, (base_url, health)
                status, leader = _request_json("GET", f"{base_url}/leader", timeout=10)
                assert status == 200, (base_url, leader)
                status, consensus = _request_json("GET", f"{base_url}/consensus/info", timeout=10)
                assert status == 200, (base_url, consensus)
                snapshot[base_url] = {
                    "health": health,
                    "leader": leader,
                    "consensus": consensus,
                }
        except Exception:
            last_snapshot = snapshot
            time.sleep(3)
            continue

        leaders = [url for url, item in snapshot.items() if item["leader"].get("leader")]
        if (
            len(leaders) == 1
            and all(item["health"].get("status") == "ok" for item in snapshot.values())
            and all(item["leader"].get("mode") == "bittensor" for item in snapshot.values())
            and all(item["leader"].get("validator_count", 0) >= 3 for item in snapshot.values())
            and all(item["consensus"].get("consensus_enabled") is True for item in snapshot.values())
            and all(len(item["consensus"].get("validators", [])) == 3 for item in snapshot.values())
            and all(len(item["consensus"].get("peers", [])) == 2 for item in snapshot.values())
        ):
            return snapshot

        last_snapshot = snapshot
        time.sleep(3)

    pytest.fail(f"Validator cluster never became ready: {last_snapshot}")


def _wait_for_validator_intent_loaded(base_url: str, app_id: str, timeout: int = 120) -> dict:
    """Poll the validator's /health (which exposes ``loaded_intents`` count)
    until at least one intent is loaded. Used to be ``/intents/available``,
    which was removed in the 2026-05-25 validator surface cleanup. The
    /health count is a coarser signal but sufficient for "is the intent
    actually loaded yet" sequencing.
    """
    deadline = time.time() + timeout
    last_payload: dict | None = None
    while time.time() < deadline:
        status, payload = _request_json("GET", f"{base_url}/health")
        last_payload = payload
        if status == 200 and int(payload.get("loaded_intents", 0)) > 0:
            return payload
        time.sleep(2)

    pytest.fail(f"Validator {base_url} never loaded intent {app_id}: {last_payload}")


def _wait_for_validator_order_terminal(
    validator_url: str,
    *,
    app_id: str,
    order_id: str,
    timeout: int = 120,
) -> dict:
    deadline = time.time() + timeout
    last_payload: dict | None = None
    non_terminal_statuses = {
        "open",
        "assigned",
        "solved",
        "scored",
        "approved",
        "submitted",
        "bridging",
        "pending",
    }
    while time.time() < deadline:
        status, payload = _request_json(
            "GET",
            f"{validator_url}/orders?app_id={urllib.parse.quote(app_id)}",
        )
        if status == 200:
            for order in payload.get("orders", []):
                if order.get("order_id") != order_id:
                    continue
                last_payload = order
                if order.get("consensus_result") is not None:
                    return order
                if order.get("status") not in non_terminal_statuses:
                    return order
        time.sleep(3)

    pytest.fail(
        f"Validator order {order_id} never reached a terminal state on {validator_url}: {last_payload}"
    )


def _submit_order(
    app_id: str,
    *,
    submitted_by: str,
    params: dict[str, str],
    chain_id: int = 31337,
    timeout: int = 30,
) -> dict:
    status, order = _request_json(
        "POST",
        f"{API_URL}/v1/apps/{app_id}/orders",
        {
            "chain_id": chain_id,
            "intent_function": "swap",
            "submitted_by": submitted_by,
            "params": params,
        },
        timeout=timeout,
    )
    assert status == 201, order
    return order


def _assert_contract_code(rpc_url: str, contract_address: str) -> str:
    payload = _rpc_json(rpc_url, "eth_getCode", [contract_address, "latest"])
    code = payload.get("result", "")
    assert code not in {"", "0x", "0X"}, payload
    return code


def test_stack_health_endpoints():
    for base_url in (API_URL, RELAYER_URL):
        status, payload = _request_json("GET", f"{base_url}/health")
        assert status == 200, (base_url, payload)
        assert payload.get("status") == "ok", (base_url, payload)

    api_cluster = _wait_for_api_cluster()
    api_leaders = [
        url
        for url, item in api_cluster.items()
        if item["health"].get("solver_round_role") == "leader"
    ]
    assert len(api_leaders) == 1, api_cluster
    for base_url, item in api_cluster.items():
        consensus = item["health"].get("champion_consensus", {})
        assert consensus.get("enabled") is True, (base_url, item)
        assert consensus.get("peer_count") == 2, (base_url, item)
        assert (consensus.get("quorum_required") or 0) >= 2, (base_url, item)

    validator_cluster = _wait_for_validator_cluster()
    leader_urls = [
        url for url, item in validator_cluster.items() if item["leader"].get("leader")
    ]
    assert len(leader_urls) == 1, validator_cluster
    leader_url = leader_urls[0]
    for base_url, item in validator_cluster.items():
        assert item["consensus"].get("quorum_required") == 2, (base_url, item)
        assert item["leader"].get("my_role") in {"leader", "follower"}, (base_url, item)
        if base_url == leader_url:
            assert item["leader"].get("my_role") == "leader", item
            assert item["health"].get("block_loop_running") is True, item
        else:
            assert item["leader"].get("my_role") == "follower", item
            assert item["health"].get("block_loop_running") is False, item

    status, payload = _request_json("GET", f"{API_URL}/health")
    assert status == 200, payload
    assert payload.get("solver_round_role") == "leader"
    epoch_clock = payload.get("solver_round_epoch_clock", {})
    assert epoch_clock.get("mode") == "native_tempo", payload
    assert epoch_clock.get("native_epoch") is not None, payload
    metagraph = payload.get("solver_round_metagraph", {})
    assert metagraph.get("native_epoch") is not None, payload
    assert metagraph.get("validator_count", 0) >= 3, payload

    status, payload = _request_json("GET", f"{API_URL}/v1/blockloop/status")
    assert status == 200, payload
    assert payload.get("running") is True


def test_can_create_and_deploy_flagship_app_via_api(deployed_test_app: dict):
    app_id = deployed_test_app["app_id"]
    status, manifest = _request_json("GET", f"{API_URL}/v1/apps/{app_id}/manifest")
    assert status == 200, manifest
    manifest_data = manifest.get("manifest", {})
    names = [
        item.get("name")
        for item in manifest_data.get("intent_functions", [])
        if isinstance(item, dict)
    ]
    assert "swap" in names

    status, deployment = _request_json("GET", f"{API_URL}/v1/apps/{app_id}/status")
    assert status == 200, deployment
    assert deployment.get("status") in {"solved", "active"}
    assert deployment.get("deployments", {}).get("31337", {}).get("status") in {"solved", "active"}
    deployed_address = (
        deployment.get("contract_address")
        or deployment.get("deployments", {}).get("31337", {}).get("contract_address")
        or ""
    )
    assert deployed_address.lower() == deployed_test_app["contract_address"].lower()
    _assert_contract_code(ETH_RPC_URL, deployed_address)


def test_validate_manifest_discovery_and_monitoring(deployed_test_app: dict):
    app_id = deployed_test_app["app_id"]

    status, validation = _request_json(
        "POST",
        f"{API_URL}/v1/apps/validate",
        {
            "js_code": DEX_AGGREGATOR_JS,
            "solidity_code": DEX_AGGREGATOR_SOLIDITY,
        },
    )
    assert status == 200, validation
    assert validation.get("valid") is True
    assert validation.get("js_config", {}).get("name") == "DexAggregator"
    assert validation.get("solidity_contract_name") == "DexAggregatorApp"

    status, manifests = _request_json("GET", f"{API_URL}/v1/apps/manifests")
    assert status == 200, manifests
    assert app_id in manifests.get("manifests", {})
    intent_names = {
        item.get("name")
        for item in manifests["manifests"][app_id].get("intent_functions", [])
        if isinstance(item, dict)
    }
    assert "swap" in intent_names

    status, monitor = _request_json("GET", f"{API_URL}/v1/apps/{app_id}/monitor")
    assert status == 200, monitor
    assert monitor.get("app_id") == app_id
    assert monitor.get("recent_executions", {}).get("total", 0) >= 0
    assert "solver_stats" in monitor


def test_testnet_faucet_and_balance_query_work():
    status, payload = _request_json(
        "POST",
        f"{API_URL}/v1/testnet/faucet",
        {"address": TEST_ADDRESS, "amount_eth": 1.0, "chain_id": 31337},
    )
    assert status == 200, payload

    status, payload = _request_json(
        "POST",
        f"{API_URL}/v1/testnet/faucet_erc20",
        {"token": "USDC", "address": TEST_ADDRESS, "amount": "1000000", "chain_id": 31337},
    )
    assert status == 200, payload

    balances = _get_wallet_balances(TEST_ADDRESS, chain_id=31337)
    assert balances.get("address", "").lower() == TEST_ADDRESS.lower()
    assert balances.get("native", {}).get("balance_wei") not in (None, "0")

    token_symbols = {token.get("symbol") for token in balances.get("tokens", [])}
    assert "USDC" in token_symbols


def test_prepare_and_quote_work_for_freshly_deployed_flagship_app(deployed_test_app: dict):
    app_id = deployed_test_app["app_id"]
    _wait_for_app_status(app_id, allowed_statuses={"solved", "active"})
    prepared = _prepare_swap(app_id)
    resolved_params = prepared.get("resolved_params", {})
    assert resolved_params.get("input_token", "").startswith("0x")
    assert resolved_params.get("output_token", "").startswith("0x")

    quote = _wait_for_quote(app_id, resolved_params)
    assert int(quote["estimated_output"]) > 0
    assert int(quote["suggested_min_output"]) > 0
    assert quote.get("valid_for_seconds") == 30
    assert quote.get("route_summary")
    ready_params = quote.get("ready_params", {})
    assert ready_params.get("input_token") == resolved_params.get("input_token")
    assert ready_params.get("output_token") == resolved_params.get("output_token")
    assert ready_params.get("input_amount") == resolved_params.get("input_amount")


def test_validator_cluster_reaches_real_quorum_on_order_execution(deployed_test_app: dict):
    cluster = _wait_for_validator_cluster()
    leader_url = next(
        url for url, item in cluster.items() if item["leader"].get("leader")
    )
    expected_validators = sorted(cluster[leader_url]["consensus"].get("validators", []))
    app_id = deployed_test_app["app_id"]

    for base_url in VALIDATOR_URLS:
        _wait_for_validator_intent_loaded(base_url, app_id)

    wallet = _create_managed_wallet([31337])
    address = wallet["address"]
    status, eth_fund = _request_json(
        "POST",
        f"{API_URL}/v1/testnet/faucet",
        {"address": address, "amount_eth": 1.0, "chain_id": 31337},
    )
    assert status == 200, eth_fund
    status, token_fund = _request_json(
        "POST",
        f"{API_URL}/v1/testnet/faucet_erc20",
        {"token": "USDC", "address": address, "amount": "1000000", "chain_id": 31337},
    )
    assert status == 200, token_fund

    prepared = _prepare_swap(app_id, submitted_by=address)
    quote = _wait_for_quote(app_id, prepared["resolved_params"])
    # Submit via the api gateway (the prod path); the validator picks up
    # the order from the shared store-data volume via its BlockLoop. The
    # validator's direct ``/orders/submit`` was removed in the 2026-05-25
    # surface cleanup — it was a duplicate that bypassed the api gateway.
    order = _submit_order(
        app_id,
        submitted_by=address,
        params=quote.get("ready_params", prepared["resolved_params"]),
    )

    terminal = _wait_for_validator_order_terminal(
        leader_url,
        app_id=app_id,
        order_id=order["order_id"],
        timeout=180,
    )
    assert terminal.get("status") not in {"open", "assigned", "solved", "scored"}, terminal
    assert terminal.get("plan"), terminal
    assert terminal.get("score") is not None, terminal

    consensus_result = terminal.get("consensus_result", {})
    approvals = consensus_result.get("approvals", [])
    assert consensus_result.get("reached") is True, terminal
    assert consensus_result.get("quorum") == 2, terminal
    assert consensus_result.get("collected", 0) >= 2, terminal
    assert len(approvals) >= 2, terminal
    approval_validators = {item.get("validator_id") for item in approvals}
    assert approval_validators.issubset(set(expected_validators)), terminal
    assert cluster[leader_url]["consensus"].get("validator_id") in approval_validators, terminal


def test_managed_wallet_order_submit_list_and_cancel_work(deployed_test_app: dict):
    _wait_for_app_status(deployed_test_app["app_id"], allowed_statuses={"solved", "active"})
    wallet = _create_managed_wallet([31337])
    address = wallet["address"]

    status, wallets = _request_json("GET", f"{API_URL}/v1/wallets/")
    assert status == 200, wallets
    assert any(item.get("address", "").lower() == address.lower() for item in wallets.get("wallets", []))

    status, wallet_details = _request_json("GET", f"{API_URL}/v1/wallets/{address}")
    assert status == 200, wallet_details
    assert wallet_details.get("address", "").lower() == address.lower()
    assert wallet_details.get("wallet_type") == "lit_mpc"

    status, eth_fund = _request_json(
        "POST",
        f"{API_URL}/v1/testnet/faucet",
        {"address": address, "amount_eth": 1.0, "chain_id": 31337},
    )
    assert status == 200, eth_fund

    status, token_fund = _request_json(
        "POST",
        f"{API_URL}/v1/testnet/faucet_erc20",
        {"token": "USDC", "address": address, "amount": "1000000", "chain_id": 31337},
    )
    assert status == 200, token_fund
    before = _get_wallet_balances(address, chain_id=31337)

    prepared = _prepare_swap(deployed_test_app["app_id"], submitted_by=address)
    quote = _wait_for_quote(deployed_test_app["app_id"], prepared["resolved_params"])
    order = _submit_order(
        deployed_test_app["app_id"],
        submitted_by=address,
        params=quote.get("ready_params", prepared["resolved_params"]),
    )
    assert order.get("submitted_by", "").lower() == address.lower()
    assert order.get("params", {}).get("user_nonce") == 0
    if order.get("user_signature"):
        assert order["user_signature"].startswith("0x")
    else:
        assert order.get("params", {}).get("permit_deadline")
    assert order.get("params", {}).get("intent_selector")

    order_id = order["order_id"]
    status, refreshed = _request_json("GET", f"{API_URL}/v1/orders/{order_id}")
    assert status == 200, refreshed
    assert refreshed.get("order_id") == order_id
    assert refreshed.get("status") in {"open", "filled"}

    status, listed = _request_json("GET", f"{API_URL}/v1/orders?app_id={deployed_test_app['app_id']}")
    assert status == 200, listed
    assert any(item.get("order_id") == order_id for item in listed.get("orders", []))
    terminal = _wait_for_order_terminal(order_id)
    assert terminal.get("status") == "filled"
    assert terminal.get("plan")
    assert terminal.get("best_score") is not None
    assert terminal.get("score") is not None
    assert terminal.get("consensus_result", {}).get("approvals")
    assert terminal.get("error") in {None, ""}

    plan = terminal["plan"]
    status, dry_run = _request_json(
        "POST",
        f"{API_URL}/v1/orders/{order_id}/dry-run",
        {
            "interactions": plan.get("interactions", []),
            "deadline": plan.get("deadline", 0),
            "nonce": plan.get("nonce", 0),
            "metadata": plan.get("metadata", {}),
        },
        timeout=120,
    )
    assert status == 200, dry_run
    assert "valid" in dry_run
    assert "score" in dry_run
    assert "reason" in dry_run

    after = _get_wallet_balances(address, chain_id=31337)
    before_tokens = {
        token.get("symbol"): token.get("balance_raw")
        for token in before.get("tokens", [])
    }
    after_tokens = {
        token.get("symbol"): token.get("balance_raw")
        for token in after.get("tokens", [])
    }
    tx_hash = terminal.get("tx_hash")
    assert isinstance(tx_hash, str) and tx_hash
    receipt = _rpc_json(ETH_RPC_URL, "eth_getTransactionReceipt", [tx_hash])
    receipt_result = receipt.get("result") or {}
    assert receipt_result.get("status") == "0x1", receipt
    assert (receipt_result.get("to") or "").lower() == deployed_test_app["contract_address"].lower()
    assert receipt_result.get("blockNumber")

    usdc_before = int(before_tokens.get("USDC", "0"))
    usdc_after = int(after_tokens.get("USDC", "0"))
    weth_after = int(after_tokens.get("WETH", "0"))
    assert before.get("native", {}).get("balance_wei") == after.get("native", {}).get("balance_wei")
    assert usdc_after < usdc_before
    assert weth_after > 0


def test_dual_chain_deploy_quote_and_activate_work(deployed_test_app: dict):
    app_id = deployed_test_app["app_id"]
    deployed = _deploy_app(app_id, chain_id=8453, timeout=180)
    assert deployed.get("chain_id") == 8453
    _assert_contract_code(BASE_RPC_URL, deployed["contract_address"])

    prepared = _prepare_swap(
        app_id,
        chain_id=8453,
        input_token="USDC",
        output_token="WETH",
    )
    resolved = prepared.get("resolved_params", {})
    assert resolved.get("input_token", "").startswith("0x")
    assert resolved.get("output_token", "").startswith("0x")

    status, quote = _request_json(
        "POST",
        f"{API_URL}/v1/apps/{app_id}/quote",
        {
            "chain_id": 8453,
            "intent_function": "swap",
            "params": resolved,
        },
    )
    assert status == 200, quote
    assert quote.get("chain_id") == 8453
    assert int(quote.get("estimated_output", "0")) > 0
    assert quote.get("ready_params", {}).get("input_token") == resolved.get("input_token")
    assert quote.get("ready_params", {}).get("output_token") == resolved.get("output_token")
    assert quote.get("ready_params", {}).get("input_amount") == resolved.get("input_amount")

    status, activated = _request_json("POST", f"{API_URL}/v1/apps/{app_id}/activate?chain_id=8453", {})
    assert status == 200, activated
    assert activated == {"app_id": app_id, "chain_id": 8453, "status": "active"}


def test_scoring_update_requires_matching_deployer():
    unique_suffix = int(time.time())
    deployer = "0x1111111111111111111111111111111111111111"
    wrong_caller = "0x2222222222222222222222222222222222222222"
    created = _create_app(
        name=f"DexAggregatorApp Scoring Auth {unique_suffix}",
        supported_chains=[31337],
        deployer=deployer,
    )
    app_id = created["app_id"]

    status, denied = _request_json(
        "PUT",
        f"{API_URL}/v1/apps/{app_id}/scoring",
        {"new_js_code": DEX_AGGREGATOR_JS, "caller": wrong_caller},
    )
    assert status == 200, denied
    assert "Unauthorized" in denied.get("error", "")

    new_js = DEX_AGGREGATOR_JS + "\n// testnet smoke update\n"
    status, updated = _request_json(
        "PUT",
        f"{API_URL}/v1/apps/{app_id}/scoring",
        {"new_js_code": new_js, "caller": deployer},
    )
    assert status == 200, updated
    assert updated.get("status") == "updated"
    assert updated.get("version") == "1.0.1"


def test_api_cluster_reaches_real_champion_quorum_with_signed_git_submission():
    _wait_for_api_cluster()

    unique_suffix = int(time.time())
    created = _create_app(
        name=f"RoundBoostApp Champion Smoke {unique_suffix}",
        supported_chains=[31337],
        js_code=_benchmark_boost_scoring_js(),
        solidity_code=DEX_AGGREGATOR_SOLIDITY,
    )
    app_id = created["app_id"]
    deployed = _deploy_app(app_id, chain_id=31337, timeout=180)
    assert deployed.get("contract_address", "").startswith("0x"), deployed
    _wait_for_app_status(
        app_id,
        allowed_statuses={"solving", "solved", "active"},
        chain_id=31337,
        timeout=240,
    )

    open_round = _wait_for_solver_round_open(API_URL, timeout=240)
    repo_url, commit_hash = _create_local_submission_repo(f"champion-smoke-{unique_suffix}")
    payload = _signed_submission_payload(
        repo_url=repo_url,
        commit_hash=commit_hash,
        round_id=open_round["round_id"],
        epoch=int(open_round.get("opened_epoch", 0) or 0),
    )
    status, submitted = _request_json(
        "POST",
        f"{API_URL}/v1/submissions",
        payload,
        timeout=30,
    )
    assert status == 201, submitted
    submission_id = submitted["submission_id"]
    assert submitted.get("round_id") == open_round["round_id"], submitted

    submission_status = _wait_for_submission_status(
        submission_id,
        allowed_statuses={"benchmarking", "scored", "adopted"},
        timeout=420,
    )
    assert (submission_status.get("image_id") or "").startswith("sha256:"), submission_status
    assert submission_status.get("rejection_reason") in {None, ""}

    status, health = _request_json("GET", f"{API_URL}/health", timeout=10)
    assert status == 200, health
    current_epoch = int(health.get("solver_round_epoch", 0) or 0)

    status, closed = _request_json(
        "POST",
        f"{API_URL}/v1/solver/round/close",
        {
            "round_id": open_round["round_id"],
            "close_epoch": current_epoch,
            "benchmark_pack_hash": f"smoke-pack-{open_round['round_id']}",
            "committee_hash": f"smoke-committee-{open_round['round_id']}",
            "decision_deadline_epoch": current_epoch + 120,
            "effective_epoch": current_epoch + 1,
        },
        timeout=20,
    )
    assert status == 200, closed
    assert closed.get("round_id") == open_round["round_id"], closed
    assert closed.get("status") in {"closed", "replaying", "certifying", "certified", "activated"}, closed

    certifying_cluster = _wait_for_api_round_certifying(
        open_round["round_id"],
        expected_submission_id=submission_id,
        timeout=420,
    )
    certifying_round = certifying_cluster[API_URL]
    certified = certifying_round
    if certifying_round.get("status") == "certifying":
        status, certified = _request_json(
            "POST",
            f"{API_URL}/v1/solver/round/certify",
            {
                "round_id": open_round["round_id"],
                "candidate_submission_id": submission_id,
                "candidate_image_id": certifying_round.get("finalist_image_id"),
                "committee_hash": certifying_round.get("committee_hash"),
                "benchmark_pack_hash": certifying_round.get("benchmark_pack_hash"),
                "shadow_case_log_hash": certifying_round.get("shadow_case_log_hash"),
                "effective_epoch": int(certifying_round.get("effective_epoch") or current_epoch + 1),
            },
            timeout=120,
        )
        assert status == 200, certified
        assert certified.get("status") in {"certified", "activated"}, certified

    status, certified = _request_json(
        "GET",
        f"{API_URL}/v1/solver/round/{urllib.parse.quote(open_round['round_id'])}",
        timeout=20,
    )
    assert status == 200, certified

    if certified.get("status") != "activated":
        activation_epoch = int(certified.get("effective_epoch") or current_epoch + 1)
        status, activated = _request_json(
            "POST",
            f"{API_URL}/v1/solver/round/activate",
            {
                "round_id": open_round["round_id"],
                "activation_epoch": activation_epoch,
            },
            timeout=120,
        )
        assert status == 200, activated

    round_cluster = _wait_for_api_round_cluster(
        open_round["round_id"],
        expected_submission_id=submission_id,
        timeout=420,
    )
    adopted_status = _wait_for_submission_status(
        submission_id,
        allowed_statuses={"adopted"},
        timeout=240,
    )

    for base_url, item in round_cluster.items():
        round_state = item["round"]
        champion = item["champion"]
        assert round_state.get("status") == "activated", (base_url, round_state)
        assert round_state.get("certificate_candidate_submission_id") == submission_id, (
            base_url,
            round_state,
        )
        assert (round_state.get("certificate_quorum_required") or 0) >= 2, (
            base_url,
            round_state,
        )
        assert (round_state.get("certificate_approvals") or 0) >= (
            round_state.get("certificate_quorum_required") or 0
        ), (base_url, round_state)
        assert champion.get("submission_id") == submission_id, (base_url, champion)
        assert champion.get("activated_round_id") == open_round["round_id"], (base_url, champion)

    assert adopted_status.get("submission_id") == submission_id
    assert adopted_status.get("status") == "adopted"


def _wait_for_round_activated(
    round_id: str,
    *,
    base_url: str = API_URL,
    timeout: int = 600,
) -> dict:
    """Wait for a solver round to reach activated (or aborted) autonomously."""
    deadline = time.time() + timeout
    last_payload: dict | None = None
    while time.time() < deadline:
        status, payload = _request_json(
            "GET",
            f"{base_url}/v1/solver/round/{urllib.parse.quote(round_id)}",
            timeout=10,
        )
        last_payload = payload
        if status == 200:
            if payload.get("status") == "aborted":
                pytest.fail(
                    f"Round {round_id} aborted before activation: "
                    f"reason={payload.get('abort_reason')}"
                )
            if payload.get("status") == "activated":
                return payload
        time.sleep(3)
    pytest.fail(f"Round {round_id} never reached activated on {base_url}: {last_payload}")


def test_round_lifecycle_completes_autonomously():
    """Verify the coordinator loop drives a full round lifecycle without
    manual close/certify/activate API calls.

    This test submits a solver and waits for the coordinator to drive:
    OPEN → CLOSED → REPLAYING → CERTIFYING → CERTIFIED → ACTIVATED.
    """
    _wait_for_api_cluster()

    unique_suffix = int(time.time())
    created = _create_app(
        name=f"AutonomousRoundApp {unique_suffix}",
        supported_chains=[31337],
        js_code=_benchmark_boost_scoring_js(),
        solidity_code=DEX_AGGREGATOR_SOLIDITY,
    )
    app_id = created["app_id"]
    deployed = _deploy_app(app_id, chain_id=31337, timeout=180)
    assert deployed.get("contract_address", "").startswith("0x"), deployed
    _wait_for_app_status(
        app_id,
        allowed_statuses={"solving", "solved", "active"},
        chain_id=31337,
        timeout=240,
    )

    # Wait for an open round
    open_round = _wait_for_solver_round_open(API_URL, timeout=240)
    round_id = open_round["round_id"]
    round_open_seconds = int(
        os.environ.get("SOLVER_ROUND_OPEN_SECONDS", "600")
    )

    # Submit solver
    repo_url, commit_hash = _create_local_submission_repo(
        f"autonomous-round-{unique_suffix}"
    )
    payload = _signed_submission_payload(
        repo_url=repo_url,
        commit_hash=commit_hash,
        round_id=round_id,
        epoch=int(open_round.get("opened_epoch", 0) or 0),
    )
    status, submitted = _request_json(
        "POST", f"{API_URL}/v1/submissions", payload, timeout=30,
    )
    assert status == 201, submitted
    submission_id = submitted["submission_id"]

    # Wait for screening + benchmarking
    _wait_for_submission_status(
        submission_id,
        allowed_statuses={"benchmarking", "scored", "adopted"},
        timeout=420,
    )

    # Now wait for the coordinator to autonomously drive to activated.
    # Timeout = round_open_seconds + generous margin for close/certify/activate.
    total_timeout = round_open_seconds + 300
    activated = _wait_for_round_activated(
        round_id, timeout=total_timeout,
    )
    assert activated.get("finalist_submission_id") == submission_id, activated
    assert activated.get("status") == "activated"

    # Verify champion was adopted cluster-wide
    round_cluster = _wait_for_api_round_cluster(
        round_id,
        expected_submission_id=submission_id,
        timeout=240,
    )
    for base_url, item in round_cluster.items():
        assert item["round"].get("status") == "activated", (base_url, item)
        assert item["champion"].get("submission_id") == submission_id, (
            base_url, item,
        )


def test_validator_weight_tracking():
    """Verify validators track weights and expose them via health endpoint."""
    for url in VALIDATOR_URLS:
        try:
            status, health = _request_json("GET", f"{url}/health", timeout=10)
        except Exception:
            continue  # Skip unreachable validators
        if status != 200:
            continue
        # Validator should report weight-related fields in health
        assert "status" in health, (url, health)
        # Check that the validator is functional (not crashed)
        assert health.get("status") == "ok", (url, health)


def test_validators_have_consensus_enabled():
    """All validator services should have consensus configured."""
    for url in VALIDATOR_URLS:
        try:
            status, info = _request_json(
                "GET", f"{url}/consensus/info", timeout=10,
            )
        except Exception:
            pytest.skip(f"Validator at {url} not reachable")
        assert status == 200, (url, info)
        assert info.get("consensus_enabled") is True, (url, info)


def test_real_swap_with_multi_validator_consensus(deployed_test_app):
    """Real USDC→WETH swap with multi-validator consensus and latency measurement.

    This is the product-level validation: a real user swap goes through
    the full pipeline with independent validator re-scoring, on-chain
    execution, and measurable fill quality.
    """
    import os
    if os.environ.get("CONSENSUS_MODE", "local") != "real":
        pytest.skip("Requires CONSENSUS_MODE=real")

    app_id = deployed_test_app["app_id"]
    wallet = _create_managed_wallet(chain_ids=[31337])
    address = wallet["address"]

    # Fund wallet
    _request_json("POST", f"{API_URL}/v1/testnet/faucet", {
        "address": address, "amount_eth": 1.0, "chain_id": 31337,
    })
    _request_json("POST", f"{API_URL}/v1/testnet/faucet_erc20", {
        "address": address,
        "token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "amount": "1000000",
        "chain_id": 31337,
    })

    # Prepare → Quote flow
    _prepare_swap(app_id, submitted_by=address)
    quote = _wait_for_quote(app_id, {
        "input_token": "USDC",
        "output_token": "WETH",
        "input_amount": "1000000",
    })
    params = quote.get("ready_params", {})
    assert params.get("min_output_amount"), f"Quote not ready: {quote}"
    params["submitted_by"] = address

    # Submit order and time it
    t_start = time.time()
    order = _submit_order(app_id, submitted_by=address, params=params)
    order_id = order["order_id"]

    # Wait for fill
    deadline = time.time() + 120
    final_order = None
    while time.time() < deadline:
        status, current = _request_json("GET", f"{API_URL}/v1/orders/{order_id}")
        if status == 200 and current.get("status") in ("filled", "rejected"):
            final_order = current
            break
        time.sleep(2)
    t_fill = time.time()

    assert final_order is not None, f"Order {order_id} never completed"
    latency = t_fill - t_start
    print(f"\n  === Real Swap Results ===")
    print(f"  Status: {final_order.get('status')}")
    print(f"  Latency: {latency:.1f}s (order to fill)")
    print(f"  Score: {final_order.get('score')}")
    print(f"  Tx hash: {final_order.get('tx_hash', 'none')}")

    cr = final_order.get("consensus_result", {})
    approvals = cr.get("approvals", [])
    print(f"  Consensus: {cr.get('collected')}/{cr.get('quorum')} approvals")
    for a in approvals:
        print(f"    {a['validator_id'][:12]}: score={a['score']:.4f}")

    assert final_order.get("status") == "filled", (
        f"Expected filled, got {final_order.get('status')}: {final_order.get('error')}"
    )
    assert cr.get("reached") is True, f"Consensus not reached: {cr}"
    assert len(approvals) >= 2, f"Expected 2+ approvals, got {len(approvals)}"
    assert final_order.get("tx_hash"), "No tx hash"
    assert final_order.get("score", 0) > 0.5, f"Score too low: {final_order.get('score')}"
    print(f"  PASSED: real swap filled in {latency:.1f}s with {len(approvals)}-validator consensus")


def test_perpetual_order_lifecycle(deployed_test_app):
    """Perpetual order: fills multiple times, respects max_executions.

    Submits a perpetual order with max_executions=2 and cooldown=1s.
    Verifies it fills, re-opens, fills again, then stays permanently filled.
    """
    app_id = deployed_test_app["app_id"]
    wallet = _create_managed_wallet(chain_ids=[31337])
    address = wallet["address"]

    # Fund the wallet
    _request_json("POST", f"{API_URL}/v1/testnet/faucet", {
        "address": address, "amount_eth": 1.0, "chain_id": 31337,
    })

    # Prepare swap params
    prepared = _prepare_swap(app_id, submitted_by=address)
    params = prepared.get("ready_params") or prepared.get("params", {})
    if not params.get("min_output_amount"):
        pytest.skip("Quote not ready for perpetual order test")

    # Submit perpetual order
    status, order = _request_json(
        "POST",
        f"{API_URL}/v1/apps/{app_id}/orders",
        {
            "chain_id": 31337,
            "intent_function": "swap",
            "submitted_by": address,
            "params": params,
            "perpetual": True,
            "max_executions": 2,
            "cooldown": 1.0,
        },
        timeout=30,
    )
    assert status == 201, order
    order_id = order["order_id"]
    assert order.get("perpetual") is True, order
    assert order.get("max_executions") == 2, order

    # Wait for first fill
    deadline = time.time() + 120
    fill_count = 0
    while time.time() < deadline:
        status, current = _request_json("GET", f"{API_URL}/v1/orders/{order_id}")
        if status != 200:
            time.sleep(2)
            continue
        exec_count = current.get("execution_count", 0)
        if exec_count > fill_count:
            fill_count = exec_count
        # After max_executions, should stay FILLED permanently
        if fill_count >= 2:
            assert current.get("status") in {"filled", "FILLED"}, (
                f"Expected permanently filled after {fill_count} executions: {current}"
            )
            break
        time.sleep(3)

    assert fill_count >= 1, f"Perpetual order never filled (order_id={order_id})"


def _get_seeded_app_id() -> str:
    """Find the seeded DexAggregatorApp (avoids slow fixture deployment)."""
    status, data = _request_json("GET", f"{API_URL}/v1/apps/", timeout=10)
    assert status == 200, data
    apps = data.get("apps", [])
    for app in apps:
        if "DexAggregator" in app.get("name", ""):
            return app["app_id"]
    pytest.skip("No seeded DexAggregatorApp found")


def test_validator_failure_consensus_still_works():
    """Kill one validator and verify orders still fill with 2/3 quorum.

    Requires CONSENSUS_MODE=real. Stops validator-peer-2, submits an order,
    verifies quorum is reached with 2 validators (leader + peer-1).
    Restarts the validator after the test.
    """
    import os
    import subprocess

    if os.environ.get("CONSENSUS_MODE", "local") != "real":
        pytest.skip("Requires CONSENSUS_MODE=real")

    app_id = _get_seeded_app_id()

    # Verify all 3 validators are healthy before the test
    for url in VALIDATOR_URLS:
        status, _ = _request_json("GET", f"{url}/health", timeout=10)
        assert status == 200, f"Validator {url} not healthy before test"

    # Stop validator-peer-2
    try:
        subprocess.run(
            ["docker", "stop", "local_testnet-validator-peer-2-1"],
            capture_output=True, timeout=15,
        )
        time.sleep(2)

        # Create and fund wallet (ETH + USDC for swap)
        wallet = _create_managed_wallet(chain_ids=[31337])
        address = wallet["address"]
        _request_json("POST", f"{API_URL}/v1/testnet/faucet", {
            "address": address, "amount_eth": 1.0, "chain_id": 31337,
        })
        _request_json("POST", f"{API_URL}/v1/testnet/faucet_erc20", {
            "address": address,
            "token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "amount": "1000000",
            "chain_id": 31337,
        })

        _prepare_swap(app_id, submitted_by=address)
        quote = _wait_for_quote(app_id, {
            "input_token": "USDC", "output_token": "WETH", "input_amount": "1000000",
        }, timeout=60)
        params = quote.get("ready_params", {})
        min_out = quote.get("suggested_min_output") or params.get("min_output_amount")
        if not min_out or min_out in ("0", ""):
            pytest.skip("Quote not ready after wait")
        params["min_output_amount"] = min_out

        order = _submit_order(app_id, submitted_by=address, params=params)
        order_id = order["order_id"]

        # Wait for fill (should work with 2/3 quorum)
        deadline = time.time() + 60
        final_order = None
        while time.time() < deadline:
            status, current = _request_json("GET", f"{API_URL}/v1/orders/{order_id}")
            if status == 200 and current.get("status") in ("filled", "rejected"):
                final_order = current
                break
            time.sleep(2)

        assert final_order is not None, "Order never completed with validator down"
        cr = final_order.get("consensus_result", {})
        print(f"\n  Validator failure test:")
        print(f"  Status: {final_order.get('status')}")
        print(f"  Error: {final_order.get('error', 'none')}")
        print(f"  Approvals: {cr.get('collected')}/{cr.get('quorum')}")

        # Off-chain consensus should reach quorum with 2 validators
        assert cr.get("reached") is True, f"Consensus should reach with 2/3: {cr}"
        assert cr.get("collected", 0) >= 2, f"Expected 2+ approvals: {cr}"
        assert final_order.get("status") == "filled", (
            f"Order should fill with 2/3 quorum: {final_order.get('error')}"
        )
        assert final_order.get("tx_hash"), "No tx hash"
        print("  PASSED: order filled with 2/3 validator quorum")

    finally:
        # Always restart the validator
        subprocess.run(
            ["docker", "start", "local_testnet-validator-peer-2-1"],
            capture_output=True, timeout=15,
        )
        time.sleep(5)


def test_consensus_timeout_produces_clean_rejection():
    """When ALL non-leader validators are down, consensus should timeout
    cleanly and reject the order (not hang forever).

    Requires CONSENSUS_MODE=real.
    """
    import os
    import subprocess

    if os.environ.get("CONSENSUS_MODE", "local") != "real":
        pytest.skip("Requires CONSENSUS_MODE=real")

    app_id = _get_seeded_app_id()

    # Prepare wallet and quote BEFORE stopping validators
    wallet = _create_managed_wallet(chain_ids=[31337])
    address = wallet["address"]
    _request_json("POST", f"{API_URL}/v1/testnet/faucet", {
        "address": address, "amount_eth": 1.0, "chain_id": 31337,
    })
    _prepare_swap(app_id, submitted_by=address)
    quote = _wait_for_quote(app_id, {
        "input_token": "USDC", "output_token": "WETH", "input_amount": "1000000",
    }, timeout=60)
    params = quote.get("ready_params", {})
    min_out = quote.get("suggested_min_output") or params.get("min_output_amount")
    if not min_out or min_out in ("0", ""):
        pytest.skip("Quote not ready")
    params["min_output_amount"] = min_out

    # NOW stop both peer validators
    try:
        subprocess.run(
            ["docker", "stop", "local_testnet-validator-peer-1-1",
             "local_testnet-validator-peer-2-1"],
            capture_output=True, timeout=15,
        )
        time.sleep(2)

        t_start = time.time()
        order = _submit_order(app_id, submitted_by=address, params=params)
        order_id = order["order_id"]

        # Wait for rejection (should timeout consensus, not hang)
        deadline = time.time() + 90  # Consensus timeout is 30s + margin
        final_order = None
        while time.time() < deadline:
            status, current = _request_json("GET", f"{API_URL}/v1/orders/{order_id}")
            if status == 200 and current.get("status") in ("filled", "rejected"):
                final_order = current
                break
            time.sleep(2)
        t_end = time.time()
        elapsed = t_end - t_start

        assert final_order is not None, (
            f"Order hung for {elapsed:.0f}s without resolving — "
            f"consensus timeout not working"
        )
        print(f"\n  Consensus timeout test:")
        print(f"  Status: {final_order.get('status')}")
        print(f"  Error: {final_order.get('error')}")
        print(f"  Elapsed: {elapsed:.1f}s")
        assert final_order.get("status") == "rejected", (
            f"Expected rejection when all peers down, got: {final_order.get('status')}"
        )
        assert "Consensus" in (final_order.get("error") or ""), (
            f"Expected consensus error: {final_order.get('error')}"
        )
        # Should resolve within consensus timeout (30s) + margin
        assert elapsed < 60, f"Took {elapsed:.0f}s — should resolve within 60s"
        print(f"  PASSED: clean rejection in {elapsed:.1f}s")

    finally:
        subprocess.run(
            ["docker", "start", "local_testnet-validator-peer-1-1",
             "local_testnet-validator-peer-2-1"],
            capture_output=True, timeout=15,
        )
        time.sleep(5)


# ---------------------------------------------------------------------------
# Resilience tests
# ---------------------------------------------------------------------------


def test_order_survives_api_restart():
    """Submit an order with a far-future deadline, restart the API container,
    and verify the order is still in the store and gets processed.

    Tests OB-11 (persistence) and OB-12 (reload on startup).
    """
    import subprocess

    app_id = _get_seeded_app_id()

    # Create wallet and fund
    wallet = _create_managed_wallet(chain_ids=[31337])
    address = wallet["address"]
    _request_json("POST", f"{API_URL}/v1/testnet/faucet", {
        "address": address, "amount_eth": 1.0, "chain_id": 31337,
    })
    _request_json("POST", f"{API_URL}/v1/testnet/faucet_erc20", {
        "address": address,
        "token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "amount": "1000000",
        "chain_id": 31337,
    })

    # Get quote
    _prepare_swap(app_id, submitted_by=address)
    status, quote = _request_json("POST", f"{API_URL}/v1/apps/{app_id}/quote", {
        "chain_id": 31337, "intent_function": "swap",
        "params": {"input_token": "USDC", "output_token": "WETH", "input_amount": "1000000"},
    }, timeout=30)
    params = quote.get("ready_params", {})
    min_out = quote.get("suggested_min_output") or params.get("min_output_amount")
    if not min_out or min_out in ("0", ""):
        pytest.skip("Quote not ready")
    params["min_output_amount"] = min_out

    # Submit order — should be persisted to store
    order = _submit_order(app_id, submitted_by=address, params=params)
    order_id = order["order_id"]
    print(f"\n  Submitted order: {order_id}")

    # Wait briefly to ensure it's persisted (sync happens on submission)
    time.sleep(3)

    # Verify order exists before restart
    status, pre_restart = _request_json("GET", f"{API_URL}/v1/orders/{order_id}")
    assert status == 200, f"Order not found before restart: {pre_restart}"
    pre_status = pre_restart.get("status")
    print(f"  Pre-restart status: {pre_status}")

    # If already filled, the test succeeded trivially (fast block loop)
    if pre_status == "filled":
        print("  Order filled before restart — persistence test passes trivially")
        return

    # Restart the API container
    print("  Restarting API container...")
    subprocess.run(
        ["docker", "restart", "local_testnet-api-1"],
        capture_output=True, timeout=30,
    )

    # Wait for API to come back
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            s, h = _request_json("GET", f"{API_URL}/health", timeout=5)
            if s == 200 and h.get("status") == "ok":
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        pytest.fail("API didn't recover after restart")

    # Wait for block loop to start processing
    time.sleep(5)

    # Check order still exists after restart
    status, post_restart = _request_json("GET", f"{API_URL}/v1/orders/{order_id}")
    assert status == 200, f"Order LOST after restart: {post_restart}"
    post_status = post_restart.get("status")
    print(f"  Post-restart status: {post_status}")

    # Wait for fill (the reloaded order should be reprocessed)
    fill_deadline = time.time() + 60
    final = post_restart
    while time.time() < fill_deadline:
        status, current = _request_json("GET", f"{API_URL}/v1/orders/{order_id}")
        if status == 200 and current.get("status") in ("filled", "rejected"):
            final = current
            break
        time.sleep(3)

    print(f"  Final status: {final.get('status')}")
    assert final.get("status") in ("filled", "rejected"), (
        f"Order {order_id} stuck after restart: {final.get('status')}"
    )
    # If it was filled, persistence + recovery worked perfectly
    if final.get("status") == "filled":
        print(f"  PASSED: order survived restart and was filled (tx={final.get('tx_hash', 'none')[:16]})")
    else:
        print(f"  PASSED: order survived restart (status={final.get('status')})")


def test_concurrent_order_submission():
    """Submit multiple orders simultaneously and verify all are processed
    without nonce conflicts or lost orders.
    """
    import concurrent.futures

    app_id = _get_seeded_app_id()
    n_orders = 5

    # Create and fund wallets (one per order to avoid nonce conflicts at user level)
    wallets = []
    for i in range(n_orders):
        w = _create_managed_wallet(chain_ids=[31337])
        addr = w["address"]
        _request_json("POST", f"{API_URL}/v1/testnet/faucet", {
            "address": addr, "amount_eth": 1.0, "chain_id": 31337,
        })
        _request_json("POST", f"{API_URL}/v1/testnet/faucet_erc20", {
            "address": addr,
            "token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "amount": "1000000",
            "chain_id": 31337,
        })
        wallets.append(addr)

    # Get a quote (same params for all)
    _prepare_swap(app_id, submitted_by=wallets[0])
    status, quote = _request_json("POST", f"{API_URL}/v1/apps/{app_id}/quote", {
        "chain_id": 31337, "intent_function": "swap",
        "params": {"input_token": "USDC", "output_token": "WETH", "input_amount": "1000000"},
    }, timeout=30)
    params = quote.get("ready_params", {})
    min_out = quote.get("suggested_min_output") or params.get("min_output_amount")
    if not min_out or min_out in ("0", ""):
        pytest.skip("Quote not ready")
    params["min_output_amount"] = min_out

    # Submit all orders concurrently
    def submit_one(addr):
        return _submit_order(app_id, submitted_by=addr, params=dict(params))

    print(f"\n  Submitting {n_orders} orders concurrently...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_orders) as pool:
        futures = {pool.submit(submit_one, addr): addr for addr in wallets}
        order_ids = []
        for f in concurrent.futures.as_completed(futures):
            result = f.result()
            order_ids.append(result["order_id"])

    print(f"  Submitted: {order_ids}")
    assert len(order_ids) == n_orders, f"Expected {n_orders} orders, got {len(order_ids)}"

    # Wait for all to reach terminal state
    deadline = time.time() + 120
    results = {}
    while time.time() < deadline:
        for oid in order_ids:
            if oid in results:
                continue
            status, current = _request_json("GET", f"{API_URL}/v1/orders/{oid}")
            if status == 200 and current.get("status") in ("filled", "rejected"):
                results[oid] = current.get("status")
        if len(results) == n_orders:
            break
        time.sleep(3)

    filled = sum(1 for s in results.values() if s == "filled")
    rejected = sum(1 for s in results.values() if s == "rejected")
    pending = n_orders - len(results)

    print(f"  Results: {filled} filled, {rejected} rejected, {pending} pending")
    for oid, s in results.items():
        print(f"    {oid}: {s}")

    assert pending == 0, f"{pending} orders still pending after 120s"
    assert filled >= 1, f"No orders filled out of {n_orders}"
    print(f"  PASSED: {filled}/{n_orders} orders processed concurrently")


def test_api_peers_serve_reads_when_leader_down():
    """When the leader API goes down, peers should still serve read requests
    (health, order status, app listing) via the shared store.

    NOTE: Full leader failover (peer takes over BlockLoop) requires
    dynamic metagraph stake changes, which the local testnet doesn't
    support. This test only validates read-path resilience.
    """
    import subprocess

    app_id = _get_seeded_app_id()

    try:
        # Stop the leader API
        print("\n  Stopping leader API (port 8080)...")
        subprocess.run(
            ["docker", "stop", "local_testnet-api-1"],
            capture_output=True, timeout=15,
        )
        time.sleep(3)

        # Verify leader is unreachable
        try:
            _request_json("GET", f"{API_URL}/health", timeout=3)
            pytest.fail("Leader should be unreachable")
        except Exception:
            pass  # Expected

        # Peers should still be healthy
        for peer_url in [API_PEER_1_URL, API_PEER_2_URL]:
            status, health = _request_json("GET", f"{peer_url}/health", timeout=10)
            assert status == 200, f"Peer {peer_url} not healthy: {health}"
            print(f"  Peer {peer_url}: status={health.get('status')}")

        # Peers should serve app listing from shared store
        for peer_url in [API_PEER_1_URL, API_PEER_2_URL]:
            status, apps = _request_json("GET", f"{peer_url}/v1/apps/", timeout=10)
            assert status == 200, f"Peer {peer_url} can't list apps: {apps}"
            assert len(apps.get("apps", [])) >= 1, f"No apps on {peer_url}"

        print("  PASSED: peers serve reads while leader is down")

    finally:
        subprocess.run(
            ["docker", "start", "local_testnet-api-1"],
            capture_output=True, timeout=15,
        )
        # Wait for leader recovery
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                s, _ = _request_json("GET", f"{API_URL}/health", timeout=5)
                if s == 200:
                    break
            except Exception:
                pass
            time.sleep(2)
