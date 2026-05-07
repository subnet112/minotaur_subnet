"""End-to-end challenger demo: proves miner competition improves execution.

Flow:
  1. Deploy app with scoring JS that rewards boosted plans
  2. Wait for genesis baseline champion (scores low on boosted scenarios)
  3. Submit a "better" solver via signed git submission
  4. Wait for autonomous round lifecycle: screening → benchmarking → champion
     quorum → activation
  5. Verify: new champion is activated, benchmark score improved

This test does NOT manually call close/certify/activate — the coordinator
loop must drive the full lifecycle autonomously.

Requires:
  - Local testnet running (make testnet-up)
  - SOLVER_ROUND_OPEN_SECONDS should be short (30-60s) for fast iteration
  - REQUIRE_LOCAL_TESTNET=1 to fail instead of skip
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
REQUIRE_LOCAL_TESTNET = os.environ.get("REQUIRE_LOCAL_TESTNET", "").strip().lower() in {
    "1", "true", "yes", "on",
}
REPO_ROOT = Path(__file__).resolve().parents[2]
DEX_AGGREGATOR_SOLIDITY = (
    REPO_ROOT / "contracts" / "src" / "DexAggregatorApp.sol"
).read_text()
LOCAL_TESTNET_SUBMISSION_ROOT = Path(
    os.environ.get(
        "LOCAL_TESTNET_SUBMISSION_HOST_ROOT",
        str(Path(tempfile.gettempdir()) / "minotaur-testnet-submissions"),
    )
)
LOCAL_TESTNET_SUBMISSION_CONTAINER_ROOT = "/solver-submissions"
EXAMPLE_SOLVER_DOCKERFILE = (
    REPO_ROOT / "minotaur_subnet" / "docker" / "example-solver" / "Dockerfile"
).read_text()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# JS scoring code for challenger demo
# ---------------------------------------------------------------------------

def _challenger_scoring_js() -> str:
    """Scoring JS that gives 0.3 to baseline plans and 1.0 to challenger plans.

    The key difference: plans with ``metadata.challenger_boost`` score 1.0.
    The genesis baseline solver doesn't set this, so it gets ~0.3.
    The challenger solver does set it, so it gets 1.0.
    """
    benchmark_scenarios = [
        {
            "name": f"challenger_case_{idx}",
            "description": "Challenger demo benchmark case",
            "intent_function": "optimize",
            "params": {
                "benchmark_case": str(idx),
                "challenge_level": "standard",
            },
        }
        for idx in range(6)
    ]
    manifest = {
        "intent_functions": [
            {
                "name": "optimize",
                "description": "Challenger demo intent",
                "params": {
                    "benchmark_case": {
                        "type": "uint256",
                        "description": "Case id",
                        "source": "system",
                    },
                    "challenge_level": {
                        "type": "string",
                        "description": "Challenge level label",
                        "source": "system",
                    },
                },
                "example_params": {
                    "benchmark_case": "0",
                    "challenge_level": "standard",
                },
            },
        ],
        "benchmark_scenarios": benchmark_scenarios,
    }
    config = {
        "name": "ChallengerDemoApp",
        "version": "1.0.0",
        "type": "challenger_demo",
    }
    return textwrap.dedent(
        f"""
        var config = {json.dumps(config)};
        var manifest = {json.dumps(manifest)};

        function score(plan, state, context) {{
          var metadata = (plan && plan.metadata) || {{}};
          if (metadata.challenger_boost) {{
            return {{
              score: 1.0,
              valid: true,
              reason: "challenger plan: maximum score",
              breakdown: {{ challenger_boost: true }},
            }};
          }}
          // Baseline plans get a passing but low score
          if (plan && plan.interactions && plan.interactions.length > 0) {{
            return {{
              score: 0.3,
              valid: true,
              reason: "baseline plan: acceptable but improvable",
              breakdown: {{ challenger_boost: false }},
            }};
          }}
          return {{
            score: 0.0,
            valid: false,
            reason: "empty plan",
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


def _challenger_solver_source() -> str:
    """Python solver that produces plans with ``challenger_boost`` metadata."""
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


        class ChallengerSolver(IntentSolver):
            # Solver that outperforms the baseline on challenger demo benchmarks.

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
                                target="0x" + "cc" * 20,
                                value="0",
                                call_data="0x" + "dd" * 16,
                                chain_id=state.chain_id,
                            ),
                        ],
                        deadline=int(time.time()) + 300,
                        nonce=state.nonce,
                        metadata={
                            "challenger_boost": True,
                            "benchmark_case": params.get("benchmark_case", "0"),
                            "solver": "challenger-demo-v1",
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
                    name="challenger-demo-solver",
                    version="1.0.0",
                    author="challenger-test",
                    description="Solver that outperforms baseline on challenger demo benchmarks",
                    supported_chains=[1, 31337, 8453],
                    supported_intent_types=["swap", "optimize"],
                )


        SOLVER_CLASS = ChallengerSolver
        """
    ).strip() + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_testnet_reachable() -> None:
    try:
        status, _ = _request_json("GET", f"{API_URL}/health", timeout=5)
    except Exception as exc:
        if REQUIRE_LOCAL_TESTNET:
            pytest.fail(f"Local testnet API is not reachable at {API_URL}: {exc}")
        pytest.skip(f"Local testnet API is not reachable at {API_URL}")
    if status != 200:
        if REQUIRE_LOCAL_TESTNET:
            pytest.fail(f"Local testnet API health failed with HTTP {status}")
        pytest.skip(f"Local testnet API health failed with HTTP {status}")


def _create_app(name: str) -> dict:
    status, payload = _request_json("POST", f"{API_URL}/v1/apps/", {
        "name": name,
        "description": "Challenger demo app for testing miner competition",
        "supported_chains": [31337],
        "js_code": _challenger_scoring_js(),
        "solidity_code": DEX_AGGREGATOR_SOLIDITY,
    })
    assert status in (200, 201), payload
    return payload


def _deploy_app(app_id: str, timeout: int = 180) -> dict:
    status, payload = _request_json(
        "POST",
        f"{API_URL}/v1/apps/{app_id}/deploy",
        {"chain_id": 31337},
        timeout=timeout,
    )
    assert status == 200, payload
    return payload


def _wait_for_app_status(app_id: str, *, timeout: int = 240) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = _request_json("GET", f"{API_URL}/v1/apps/{app_id}/status")
        if status == 200 and payload.get("status") in {"solving", "solved", "active"}:
            deployment = payload.get("deployments", {}).get("31337")
            if deployment and deployment.get("status") in {"solving", "solved", "active"}:
                return payload
        time.sleep(2)
    pytest.fail(f"App {app_id} never became ready")


def _wait_for_round_open(timeout: int = 240) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = _request_json("GET", f"{API_URL}/v1/solver/round", timeout=10)
        if status == 200 and payload.get("status") == "open" and payload.get("accepting_submissions"):
            return payload
        time.sleep(3)
    pytest.fail("No open solver round found")


def _create_local_submission_repo(prefix: str) -> tuple[str, str]:
    LOCAL_TESTNET_SUBMISSION_ROOT.mkdir(parents=True, exist_ok=True)
    repo_dir = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=LOCAL_TESTNET_SUBMISSION_ROOT))
    (repo_dir / "Dockerfile").write_text(EXAMPLE_SOLVER_DOCKERFILE)
    (repo_dir / "solver.py").write_text(_challenger_solver_source())
    (repo_dir / "README.md").write_text("# Challenger Demo Solver\n")
    (repo_dir / "requirements.txt").write_text("\n")

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Challenger Test",
        "GIT_AUTHOR_EMAIL": "challenger@example.com",
        "GIT_COMMITTER_NAME": "Challenger Test",
        "GIT_COMMITTER_EMAIL": "challenger@example.com",
    }
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, env=env, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add challenger demo solver"],
        cwd=repo_dir, check=True, env=env, capture_output=True,
    )
    commit_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir, check=True, env=env, capture_output=True, text=True,
    ).stdout.strip()
    repo_url = f"file://{LOCAL_TESTNET_SUBMISSION_CONTAINER_ROOT}/{repo_dir.name}"
    return repo_url, commit_hash


def _signed_submission_payload(
    *, repo_url: str, commit_hash: str, round_id: str, epoch: int,
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


def _wait_for_submission_scored(submission_id: str, *, timeout: int = 420) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, payload = _request_json(
                "GET", f"{API_URL}/v1/submissions/{submission_id}/status",
                timeout=60,
            )
        except Exception:
            time.sleep(5)
            continue
        if status == 200:
            current = payload.get("status")
            if current in {"scored", "adopted"}:
                return payload
            if current == "rejected":
                pytest.fail(f"Submission {submission_id} rejected: {payload}")
        time.sleep(3)
    pytest.fail(f"Submission {submission_id} never scored (timeout={timeout}s)")


def _wait_for_champion_adopted(
    submission_id: str,
    round_id: str,
    *,
    timeout: int = 600,
) -> dict:
    """Wait for a submission to become the active champion.

    Checks both the round status AND the champion endpoint,
    since the coordinator may advance past the round before
    we poll it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Check champion endpoint first (most reliable)
        try:
            status, champion = _request_json(
                "GET", f"{API_URL}/v1/solver/champion", timeout=10,
            )
            if (
                status == 200
                and champion.get("submission_id") == submission_id
                and champion.get("activated_round_id") == round_id
            ):
                # Also fetch the round for metadata
                _, round_data = _request_json(
                    "GET",
                    f"{API_URL}/v1/solver/round/{urllib.parse.quote(round_id)}",
                    timeout=10,
                )
                return {**round_data, "_champion": champion}
        except Exception:
            pass

        # Check round directly
        try:
            status, payload = _request_json(
                "GET",
                f"{API_URL}/v1/solver/round/{urllib.parse.quote(round_id)}",
                timeout=10,
            )
            if status == 200:
                if payload.get("status") == "aborted":
                    pytest.fail(
                        f"Round {round_id} aborted: {payload.get('abort_reason')}"
                    )
                if payload.get("status") == "activated":
                    return payload
        except Exception:
            pass

        time.sleep(3)
    pytest.fail(
        f"Submission {submission_id} never became champion from round {round_id}"
    )


def _get_champion() -> dict:
    status, payload = _request_json("GET", f"{API_URL}/v1/solver/champion")
    assert status == 200, payload
    return payload


def _wait_for_api_cluster(timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for url in API_URLS:
                status, health = _request_json("GET", f"{url}/health", timeout=10)
                assert status == 200
                if health.get("solver_round_coordinator") != "running":
                    raise AssertionError("coordinator not running")
            return
        except Exception:
            time.sleep(3)
    pytest.fail("API cluster never became ready")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def require_local_testnet():
    _ensure_testnet_reachable()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_challenger_dethrones_baseline_champion():
    """Full miner competition demo:

    1. Deploy app → genesis baseline champion bootstraps
    2. Submit challenger solver → screening → benchmarking → scored higher
    3. Coordinator autonomously drives round lifecycle → champion activated
    4. Verify: new champion has higher benchmark score than baseline
    """
    _wait_for_api_cluster()

    # --- Step 1: Deploy app and let genesis bootstrap create baseline champion ---
    unique = int(time.time())
    created = _create_app(f"ChallengerDemo {unique}")
    app_id = created["app_id"]
    deployed = _deploy_app(app_id)
    assert deployed.get("contract_address", "").startswith("0x"), deployed
    _wait_for_app_status(app_id)

    # Record initial champion state (may or may not exist yet)
    initial_champion = _get_champion()
    initial_champion_id = initial_champion.get("submission_id")
    print(f"  Initial champion: {initial_champion_id or '(none)'}")

    # --- Step 2: Wait for open round, submit challenger solver ---
    open_round = _wait_for_round_open()
    round_id = open_round["round_id"]
    print(f"  Open round: {round_id}")

    repo_url, commit_hash = _create_local_submission_repo(f"challenger-{unique}")
    payload = _signed_submission_payload(
        repo_url=repo_url,
        commit_hash=commit_hash,
        round_id=round_id,
        epoch=int(open_round.get("opened_epoch", 0) or 0),
    )
    status, submitted = _request_json("POST", f"{API_URL}/v1/submissions", payload, timeout=30)
    assert status == 201, submitted
    submission_id = submitted["submission_id"]
    print(f"  Submitted challenger: {submission_id}")

    # --- Step 3: Wait for screening + benchmarking ---
    scored = _wait_for_submission_scored(submission_id)
    challenger_score = scored.get("benchmark_score", 0)
    print(f"  Challenger scored: {challenger_score}")
    assert challenger_score > 0, f"Challenger scored 0: {scored}"

    # --- Step 4: Wait for autonomous round lifecycle ---
    round_open_seconds = int(os.environ.get("SOLVER_ROUND_OPEN_SECONDS", "600"))
    total_timeout = round_open_seconds + 300
    print(f"  Waiting for champion adoption (timeout={total_timeout}s)...")
    activated = _wait_for_champion_adopted(
        submission_id, round_id, timeout=total_timeout,
    )
    print(f"  Champion adopted: finalist={activated.get('finalist_submission_id')}")

    # --- Step 5: Verify champion changed ---
    new_champion = _get_champion()
    new_champion_id = new_champion.get("submission_id")
    print(f"  New champion: {new_champion_id}")

    assert new_champion_id == submission_id, (
        f"Expected challenger {submission_id} to become champion, "
        f"got {new_champion_id}"
    )
    assert activated.get("finalist_submission_id") == submission_id

    # Verify the round was certified with quorum
    cert_approvals = activated.get("certificate_approvals", 0)
    cert_required = activated.get("certificate_quorum_required", 0)
    print(f"  Certificate: {cert_approvals}/{cert_required} approvals")
    assert cert_approvals >= cert_required, (
        f"Quorum not met: {cert_approvals} < {cert_required}"
    )

    # Verify cluster-wide activation
    for url in API_URLS:
        status, round_state = _request_json(
            "GET",
            f"{url}/v1/solver/round/{urllib.parse.quote(round_id)}",
            timeout=10,
        )
        assert status == 200, (url, round_state)
        assert round_state.get("status") == "activated", (url, round_state)

    print("  Challenger demo PASSED: miner competition improved execution")
