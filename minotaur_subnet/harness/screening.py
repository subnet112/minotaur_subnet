"""Three-stage screening pipeline for IntentSolver submissions.

Filters broken, malformed, or malicious submissions before they reach
the benchmarking phase, saving validator compute resources.

Inspired by SN62 (Ridges AI) progressive screening model.

Stages:
    Stage 1 — Static Checks (~10s): File structure, Dockerfile, repo size
    Stage 2 — Build Check (~2min): Docker build + import + init
    Stage 3 — Smoke Test (~5min): Run 3 synthetic intents, verify valid plans

Usage:
    screener = ScreeningPipeline()
    result = await screener.run_all(repo_path="/tmp/solver-repo", commit="abc123")

    if result.passed:
        print(f"Image: {result.image_tag}")
    else:
        print(f"Failed at stage {result.failed_stage}: {result.rejection_reason}")
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum repo size in bytes (100MB)
MAX_REPO_SIZE = 100 * 1024 * 1024

# Maximum single binary file size (10MB, excluding models/)
MAX_BINARY_SIZE = 10 * 1024 * 1024

# Required files in the repo
REQUIRED_FILES = ["Dockerfile", "solver.py", "README.md"]

# Approved solver-base image origins. FROM lines in a miner's Dockerfile
# must reference one of these — the validator pre-pulls them and
# maintains them, so a miner can't swap to an arbitrary base and smuggle
# in unknown code paths.
#
# - ghcr.io/subnet112/solver-base — canonical base, pinned by
#   @sha256 digest in the upstream solver-repo template.
APPROVED_BASE_IMAGES = [
    "ghcr.io/subnet112/solver-base",
]
BASE_IMAGE_PATTERN = re.compile(
    r"^\s*FROM\s+(?:"
    + "|".join(re.escape(b) for b in APPROVED_BASE_IMAGES)
    + r")(?:@sha256:[0-9a-f]{64}|:[A-Za-z0-9._-]+)?\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Forbidden Dockerfile directives
FORBIDDEN_DIRECTIVES = re.compile(
    r"^\s*(CMD|ENTRYPOINT)\s+",
    re.MULTILINE | re.IGNORECASE,
)

# Binary file extensions that are suspicious (but not model files)
BINARY_EXTENSIONS = {
    ".so", ".dll", ".dylib", ".exe", ".bin", ".dat",
    ".whl", ".tar", ".gz", ".zip", ".bz2",
}

# ── Factorization metric (Phase 0: OBSERVE-ONLY, not gated) ──────────────────
# `max_region_nodes` is the largest AST-node count of any single *named region*
# (module top-level body / function body / class body) across a submission's
# in-tree Python. It is a golf-immune proxy for "worst entanglement": counting
# AST nodes is invariant to formatting, so minifying a god-function changes
# nothing — the only way to lower it is to split a region into named helpers,
# which is exactly the factorization we want to reward.
#
# Phase 0 computes and PERSISTS this integer but does NOT gate on it: we soak
# the live distribution first, then (Phase 1) set MAX_REGION_NODES to a real cap
# and flip run_stage_1 to reject, and (Phase 2) reuse the same integer as the
# saturated-tie dethrone tie-break. `FLOOR_VERSION` stamps the metric semantics
# so a champion clean under vN is never retro-evicted by vN+1.
FLOOR_VERSION = 1
MAX_REGION_NODES: int | None = None  # None ⇒ observe-only; Phase 1 sets an int cap

# Named scopes that START a new region: a nested def/class's *body* leaves its
# parent region (its header still counts in the parent). Lambdas, comprehensions
# and data literals deliberately do NOT appear here — their nodes count into the
# enclosing region so logic can't be relocated into them to dodge the metric.
_NAMED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# Directories excluded from the scan. `.git` is VCS metadata; everything else
# in-tree is treated as miner code (deps arrive via the pinned base image behind
# the FROM allowlist, so a large in-tree *.py is the miner's own). Whether a
# subnet-declared vendor path should be exempted is a Phase-1 decision — see the
# rollout's open decision on in-tree vendored third-party *.py.
_METRIC_EXCLUDE_DIRS = {".git"}


# ═══════════════════════════════════════════════════════════════════════════════
#                          SCREENING RESULT
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class StageResult:
    """Result of a single screening stage."""
    stage: int
    passed: bool
    duration_ms: int = 0
    details: str = ""
    error_code: str | None = None
    # Factorization metric, set by stage 1 only (None on other stages / errors).
    # Computed once here so downstream consumers READ the persisted value and
    # never recompute — a cross-CPython AST difference can't then split consensus.
    max_region_nodes: int | None = None
    # Deadwood metric (Phase 0, OBSERVE-ONLY), set by stage 1 only. Same
    # compute-once-read-forever discipline as max_region_nodes. None on other
    # stages / errors, and None (with version still set) when a non-exempt file
    # failed ast.parse — see harness/deadwood.unproductive_nodes.
    unproductive_nodes: int | None = None
    unproductive_metric_version: int | None = None
    # (path, qualname-or-None, nodes) — what a miner should delete. Max 20,
    # sorted desc by nodes then path; persisted so the report can show it.
    unproductive_top_offenders: list | None = None


@dataclass
class ScreeningResult:
    """Aggregate result of the screening pipeline."""
    passed: bool = False
    stages: list[StageResult] = field(default_factory=list)
    image_tag: str | None = None          # Set if stage 2+ passes (built image)
    solver_name: str | None = None        # From metadata
    solver_version: str | None = None     # From metadata
    failed_stage: int | None = None
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to API-friendly dict."""
        return {
            "passed": self.passed,
            "image_tag": self.image_tag,
            "solver_name": self.solver_name,
            "solver_version": self.solver_version,
            "failed_stage": self.failed_stage,
            "rejection_reason": self.rejection_reason,
            "stages": {
                f"stage_{s.stage}": {
                    "passed": s.passed,
                    "duration_ms": s.duration_ms,
                    "details": s.details,
                    "error_code": s.error_code,
                    "max_region_nodes": s.max_region_nodes,
                    "unproductive_nodes": s.unproductive_nodes,
                }
                for s in self.stages
            },
        }


# ═══════════════════════════════════════════════════════════════════════════════
#                       FACTORIZATION METRIC (max_region_nodes)
# ═══════════════════════════════════════════════════════════════════════════════


def _module_max_region(tree: ast.Module) -> int:
    """Largest region node-count within one parsed module.

    A *region* is the body of a named scope: the module top level, or the body
    of a FunctionDef / AsyncFunctionDef / ClassDef. Counting a region walks every
    descendant AST node EXCEPT it does not descend into the body of a nested
    named scope — that body forms its own region (the nested def's header still
    counts in the parent, but its body "leaves"). So extracting a block into a
    named helper strictly lowers the enclosing region, while hiding logic in a
    lambda / comprehension / data literal does not (those don't start a region).
    """
    max_count = 0
    regions: list[list[ast.stmt]] = [list(tree.body)]
    while regions:
        body = regions.pop()
        count = 0
        stack: list[ast.AST] = list(body)
        while stack:
            node = stack.pop()
            count += 1
            if isinstance(node, _NAMED_SCOPES):
                # Body spins off its own region; header children (args,
                # decorators, bases, returns) still count in THIS region.
                regions.append(list(node.body))
                for child in ast.iter_child_nodes(node):
                    if any(child is stmt for stmt in node.body):
                        continue
                    stack.append(child)
            else:
                stack.extend(ast.iter_child_nodes(node))
        if count > max_count:
            max_count = count
    return max_count


def max_region_nodes(repo_path: str) -> int:
    """Largest AST region across all in-tree Python — the factorization metric.

    Golf-immune (counts AST nodes, so formatting/minification can't move it) and
    a pure function of the candidate tree (no baseline diff, no champion source).
    Returns 0 when the repo has no parseable Python. Unparseable files are
    skipped in observe mode — stage 2's import check is the backstop for code
    that cannot even be parsed.
    """
    root = Path(repo_path)
    max_count = 0
    for py in root.rglob("*.py"):
        if _METRIC_EXCLUDE_DIRS.intersection(py.parts):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        m = _module_max_region(tree)
        if m > max_count:
            max_count = m
    return max_count


# ═══════════════════════════════════════════════════════════════════════════════
#                          STAGE 1: STATIC CHECKS
# ═══════════════════════════════════════════════════════════════════════════════


def run_stage_1(repo_path: str) -> StageResult:
    """Static checks on repo structure and Dockerfile.

    Checks:
    - Required files exist (Dockerfile, solver.py, README.md)
    - Dockerfile uses official base image
    - No CMD/ENTRYPOINT in Dockerfile
    - Repo size ≤ 100MB
    - No suspicious binaries > 10MB (outside models/)

    Args:
        repo_path: Path to the cloned repo.

    Returns:
        StageResult with pass/fail and details.
    """
    start = time.monotonic()
    path = Path(repo_path)

    if not path.exists() or not path.is_dir():
        return StageResult(
            stage=1, passed=False,
            duration_ms=_elapsed(start),
            details=f"Repo path not found: {repo_path}",
            error_code="repo_not_found",
        )

    # Check required files
    for required in REQUIRED_FILES:
        if not (path / required).exists():
            return StageResult(
                stage=1, passed=False,
                duration_ms=_elapsed(start),
                details=f"Missing required file: {required}",
                error_code=f"missing_{required.lower().replace('.', '_')}",
            )

    # Validate Dockerfile
    dockerfile_content = (path / "Dockerfile").read_text()

    if not BASE_IMAGE_PATTERN.search(dockerfile_content):
        return StageResult(
            stage=1, passed=False,
            duration_ms=_elapsed(start),
            details=(
                "Dockerfile FROM must reference an approved base: "
                + ", ".join(APPROVED_BASE_IMAGES)
                + " (optionally :tag or @sha256:digest)"
            ),
            error_code="invalid_base_image",
        )

    if FORBIDDEN_DIRECTIVES.search(dockerfile_content):
        return StageResult(
            stage=1, passed=False,
            duration_ms=_elapsed(start),
            details="Dockerfile must not contain CMD or ENTRYPOINT (harness manages entry point)",
            error_code="custom_entrypoint",
        )

    # Check repo size
    total_size = _dir_size(path)
    if total_size > MAX_REPO_SIZE:
        return StageResult(
            stage=1, passed=False,
            duration_ms=_elapsed(start),
            details=f"Repo size {total_size / 1024 / 1024:.1f}MB exceeds limit of {MAX_REPO_SIZE / 1024 / 1024:.0f}MB",
            error_code="repo_too_large",
        )

    # Check for suspicious binaries (outside models/)
    for file_path in path.rglob("*"):
        if not file_path.is_file():
            continue
        # Skip .git directory
        if ".git" in file_path.parts:
            continue
        # Allow model files in models/ directories
        if "models" in file_path.parts:
            continue
        if file_path.suffix.lower() in BINARY_EXTENSIONS:
            size = file_path.stat().st_size
            if size > MAX_BINARY_SIZE:
                return StageResult(
                    stage=1, passed=False,
                    duration_ms=_elapsed(start),
                    details=f"Suspicious binary: {file_path.relative_to(path)} ({size / 1024 / 1024:.1f}MB)",
                    error_code="suspicious_binary",
                )

    # Factorization metric — Phase 0 OBSERVE-ONLY: compute + persist + log, but
    # do NOT gate (MAX_REGION_NODES is None until Phase 1 calibrates the cap).
    factor_nodes = max_region_nodes(repo_path)
    logger.info(
        "[factorization] max_region_nodes=%d floor_version=%d (observe-only, not gated) repo=%s",
        factor_nodes, FLOOR_VERSION, repo_path,
    )

    # Deadwood metric — Phase 0 OBSERVE-ONLY: compute + persist + log in the
    # same pass, do NOT gate (deadwood.UNPRODUCTIVE_NODES_MAX is None until a
    # later, separately-reviewed PR arms the floor). An unparseable non-exempt
    # file yields unproductive_nodes=None (logged inside the analyzer) — the
    # value is persisted as None and every consumer skips it; stage 2's import
    # check remains the backstop for code that cannot even be parsed.
    from minotaur_subnet.harness import deadwood

    dw = deadwood.unproductive_nodes(repo_path)
    if not dw.unparseable:
        logger.info(
            "[deadwood] unproductive_nodes=%d version=%d (observe-only) repo=%s",
            dw.unproductive_nodes, dw.version, repo_path,
        )

    return StageResult(
        stage=1, passed=True,
        duration_ms=_elapsed(start),
        details="All static checks passed",
        max_region_nodes=factor_nodes,
        unproductive_nodes=dw.unproductive_nodes,
        unproductive_metric_version=dw.version,
        unproductive_top_offenders=[list(t) for t in dw.top_offenders],
    )


# ═══════════════════════════════════════════════════════════════════════════════
#                          STAGE 2: BUILD CHECK
# ═══════════════════════════════════════════════════════════════════════════════


def _solver_build_command(image_tag: str, repo_path: str) -> list[str]:
    """``docker build`` argv for an UNTRUSTED, miner-submitted Dockerfile.

    The build runs on the validator's SHARED host docker daemon — there is no
    rootless / isolated builder (see issue #472; BuildKit can't run behind the
    docker-socket-proxy, so buildx-only isolation is unavailable). These flags
    bound what a malicious Dockerfile's ``RUN`` steps can consume on the host;
    all are legacy-builder-compatible (plain ``POST /build`` params the proxy
    allows). They do NOT contain a daemon/kernel escape — that needs the
    isolated builder tracked in #472.

    Tunable via env (conservative defaults):
      ``SCREENING_BUILD_MEMORY``      RSS cap                 (default ``4g``)
      ``SCREENING_BUILD_CPU_PERIOD``  CFS period, µs          (default ``100000``)
      ``SCREENING_BUILD_CPU_QUOTA``   CFS quota, µs           (default ``200000`` = 2 CPUs)
      ``SCREENING_BUILD_NOFILE``      open-fd ulimit (soft:hard) (default ``4096``)
    """
    memory = (os.environ.get("SCREENING_BUILD_MEMORY") or "4g").strip() or "4g"
    cpu_period = (os.environ.get("SCREENING_BUILD_CPU_PERIOD") or "100000").strip() or "100000"
    cpu_quota = (os.environ.get("SCREENING_BUILD_CPU_QUOTA") or "200000").strip() or "200000"
    nofile = (os.environ.get("SCREENING_BUILD_NOFILE") or "4096").strip() or "4096"
    return [
        "docker", "build",
        # RUN has no network — pip can't reach PyPI (anti-exfil / reproducibility);
        # the base image carries the deps. The primary abuse guard.
        "--network=none",
        # The legacy layer cache hard-fails once a referenced base layer is GC'd
        # (the #449 incident); skipping it is robust and cheap — only the solver's
        # small layers rebuild — and avoids cross-submission cache poisoning.
        "--no-cache",
        # RSS cap on the untrusted build.
        f"--memory={memory}",
        # Cap total memory INCLUDING swap (== --memory disables the swap escape;
        # without this, swap defaults to ~2x --memory).
        f"--memory-swap={memory}",
        # Bound build CPU so a crypto-mining / CPU-DoS RUN can't peg the validator
        # host for the whole build_timeout window. quota/period = N CPUs.
        f"--cpu-period={cpu_period}",
        f"--cpu-quota={cpu_quota}",
        # Cap open file descriptors per build process (cheap fd-exhaustion guard).
        # NOTE: legacy `docker build` has no `--pids-limit`, so a fork bomb is only
        # bounded by --cpu/--memory + build_timeout, not a hard pid cap — another
        # reason for the isolated builder in #472.
        "--ulimit", f"nofile={nofile}:{nofile}",
        "-t", image_tag,
        repo_path,
    ]


async def run_stage_2(
    repo_path: str,
    image_tag: str,
    build_timeout: float = 120.0,
    init_timeout: float = 60.0,
) -> StageResult:
    """Build the Docker image and verify solver can be imported and initialized.

    Steps:
    1. Build Docker image from repo's Dockerfile
    2. Run import check: from solver import SOLVER_CLASS
    3. Run init check: SOLVER_CLASS().initialize({})
    4. Get metadata

    Args:
        repo_path: Path to the cloned repo.
        image_tag: Tag for the built image.
        build_timeout: Timeout for docker build (seconds).
        init_timeout: Timeout for import + init check (seconds).

    Returns:
        StageResult with pass/fail and details.
    """
    start = time.monotonic()

    # Step 1: Build the untrusted miner image on the LEGACY builder (see build_env
    # below for why BuildKit can't run in this container). The resource caps that
    # bound what a malicious Dockerfile's RUN steps can consume on the shared host
    # daemon live in _solver_build_command; issue #472 tracks the residual
    # host-daemon (root-equivalent) risk and the rootless/isolated-builder follow-up.
    build_cmd = _solver_build_command(image_tag, repo_path)

    # Force the LEGACY builder. BuildKit/buildx CANNOT run in this api container: it needs
    # docker-API session-upgrade calls the docker-socket-proxy forbids (HTTP 403) and writes
    # builder state into the read-only ~/.docker mount. The legacy builder only needs
    # POST /build (allowed by the proxy) and loads straight into the image store, so
    # `docker inspect {{.Id}}` and the entrypoint checks below keep working. This reverts
    # #449's BuildKit switch, whose failures were silently blocking every fresh build.
    build_env = {**os.environ, "DOCKER_BUILDKIT": "0"}

    try:
        proc = await asyncio.create_subprocess_exec(
            *build_cmd,
            env=build_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=build_timeout,
        )
    except asyncio.TimeoutError:
        return StageResult(
            stage=2, passed=False,
            duration_ms=_elapsed(start),
            details=f"Docker build timed out after {build_timeout}s",
            error_code="build_timeout",
        )
    except FileNotFoundError:
        return StageResult(
            stage=2, passed=False,
            duration_ms=_elapsed(start),
            details="Docker not found. Is Docker installed and running?",
            error_code="docker_not_found",
        )

    if proc.returncode != 0:
        error_text = stderr.decode("utf-8", errors="replace")[:500]
        return StageResult(
            stage=2, passed=False,
            duration_ms=_elapsed(start),
            details=f"Docker build failed: {error_text}",
            error_code="build_failed",
        )

    # Step 1b: Runtime entrypoint verification. The text-based Stage 1 check
    # catches top-level CMD/ENTRYPOINT lines but can't see overrides done via
    # multi-line tricks, heredocs, or base-image inheritance. After the build,
    # we compare the built image's Config.Entrypoint/Cmd to the base image's.
    # Any divergence means the Dockerfile changed the entrypoint in a way
    # static inspection missed.
    entrypoint_err = await _verify_entrypoint_unchanged(repo_path, image_tag)
    if entrypoint_err is not None:
        return StageResult(
            stage=2, passed=False,
            duration_ms=_elapsed(start),
            details=entrypoint_err,
            error_code="entrypoint_overridden",
        )

    # Step 2: Import check
    # Override entrypoint since the base image sets it to the harness runner
    import_cmd = [
        "docker", "run", "--rm",
        "--network=none", "--read-only",
        "--tmpfs=/tmp:size=64m",
        "--memory=2g", "--cpus=1.0",
        "--entrypoint", "python",
        image_tag,
        "-c",
        "from solver import SOLVER_CLASS; print(f'OK: {SOLVER_CLASS.__name__}')",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *import_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=30,
        )
    except asyncio.TimeoutError:
        return StageResult(
            stage=2, passed=False,
            duration_ms=_elapsed(start),
            details="Import check timed out",
            error_code="import_timeout",
        )

    if proc.returncode != 0:
        error_text = stderr.decode("utf-8", errors="replace")[:500]
        return StageResult(
            stage=2, passed=False,
            duration_ms=_elapsed(start),
            details=f"Import failed: {error_text}",
            error_code="import_failed",
        )

    # Step 3: Init + metadata check
    init_script = (
        "from solver import SOLVER_CLASS; "
        "from minotaur_subnet.sdk.intent_solver import IntentSolver; "
        "assert issubclass(SOLVER_CLASS, IntentSolver), 'Not an IntentSolver subclass'; "
        "s = SOLVER_CLASS(); "
        "s.initialize({'chain_ids': [1], 'timeout_per_plan_ms': 30000}); "
        "m = s.metadata(); "
        "assert m.name, 'metadata().name is empty'; "
        "assert m.version, 'metadata().version is empty'; "
        "import json; "
        "print(json.dumps({'name': m.name, 'version': m.version, 'types': m.supported_intent_types}))"
    )

    init_cmd = [
        "docker", "run", "--rm",
        "--network=none", "--read-only",
        "--tmpfs=/tmp:size=64m",
        "--memory=2g", "--cpus=1.0",
        "--entrypoint", "python",
        image_tag,
        "-c", init_script,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *init_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=init_timeout,
        )
    except asyncio.TimeoutError:
        return StageResult(
            stage=2, passed=False,
            duration_ms=_elapsed(start),
            details=f"Init check timed out after {init_timeout}s",
            error_code="init_timeout",
        )

    if proc.returncode != 0:
        error_text = stderr.decode("utf-8", errors="replace")[:500]
        # Distinguish between IntentSolver subclass check and other failures
        if "Not an IntentSolver subclass" in error_text:
            error_code = "invalid_solver_class"
        elif "metadata" in error_text.lower():
            error_code = "invalid_metadata"
        else:
            error_code = "init_failed"
        return StageResult(
            stage=2, passed=False,
            duration_ms=_elapsed(start),
            details=f"Init failed: {error_text}",
            error_code=error_code,
        )

    # Parse metadata from stdout
    try:
        meta_data = json.loads(stdout.decode("utf-8").strip())
        details = f"Image built, init OK: {meta_data['name']} v{meta_data['version']}"
    except (json.JSONDecodeError, KeyError):
        details = "Image built, init OK"

    return StageResult(
        stage=2, passed=True,
        duration_ms=_elapsed(start),
        details=details,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#                          STAGE 3: SMOKE TEST
# ═══════════════════════════════════════════════════════════════════════════════


async def run_stage_3(
    image_tag: str,
    plan_timeout: float = 30.0,
    total_timeout: float = 300.0,
) -> StageResult:
    """Smoke test: run 3 synthetic intents and verify valid plans.

    Uses the orchestrator subprocess mode (inside Docker container) to
    run the solver against synthetic intents. Validates structural
    correctness of returned plans (NOT scoring).

    Args:
        image_tag: Docker image tag to test.
        plan_timeout: Timeout per plan generation (seconds).
        total_timeout: Total timeout for all smoke tests (seconds).

    Returns:
        StageResult with pass/fail and details.
    """
    from minotaur_subnet.harness.snapshot import build_synthetic_intents
    from minotaur_subnet.harness.orchestrator import (
        SolverOrchestrator,
        SolverSession,
        SolverTimeoutError,
        SolverCrashedError,
    )
    from minotaur_subnet.harness.protocol import TIMEOUTS
    from minotaur_subnet.shared.types import TriggerType

    start = time.monotonic()
    synthetic = build_synthetic_intents()
    passed_count = 0
    total_count = len(synthetic)
    trigger_checked = False

    orch = SolverOrchestrator()

    try:
        session = await orch.start_docker(image_tag)
    except Exception as exc:
        return StageResult(
            stage=3, passed=False,
            duration_ms=_elapsed(start),
            details=f"Failed to start container: {exc}",
            error_code="container_start_failed",
        )

    try:
        # Initialize. Pass rpc_urls (resolved from the benchmark RPC env, the same
        # way the benchmark worker does) so a solver that reads its chain RPC from
        # the init params — not just the env — can build a Web3 for chain 1 and
        # actually generate a plan. Without this, Stage 3 fails such solvers with
        # "no Web3 available for chain 1" even though the build + init succeeded.
        from minotaur_subnet.harness.orchestrator import build_rpc_url_map
        _init_config: dict[str, Any] = {
            "chain_ids": [1],
            "timeout_per_plan_ms": int(plan_timeout * 1000),
        }
        _rpc_map = build_rpc_url_map([1])
        if _rpc_map:
            _init_config["rpc_urls"] = {str(k): v for k, v in _rpc_map.items()}
        await asyncio.wait_for(
            session.initialize(_init_config),
            timeout=60,
        )

        for intent, state, snapshot in synthetic:
            if time.monotonic() - start > total_timeout:
                return StageResult(
                    stage=3, passed=False,
                    duration_ms=_elapsed(start),
                    details=f"Total timeout exceeded ({total_timeout}s), {passed_count}/{total_count} passed",
                    error_code="total_timeout",
                )

            # Generate plan
            try:
                plan = await asyncio.wait_for(
                    session.generate_plan(intent, state, snapshot),
                    timeout=plan_timeout,
                )
            except asyncio.TimeoutError:
                return StageResult(
                    stage=3, passed=False,
                    duration_ms=_elapsed(start),
                    details=f"Plan generation timed out for {intent.app_id}",
                    error_code="plan_timeout",
                )
            except SolverCrashedError as exc:
                return StageResult(
                    stage=3, passed=False,
                    duration_ms=_elapsed(start),
                    details=f"Solver crashed during {intent.app_id}: {exc}",
                    error_code="plan_generation_failed",
                )

            if plan is None:
                return StageResult(
                    stage=3, passed=False,
                    duration_ms=_elapsed(start),
                    details=f"Solver returned null plan for {intent.app_id}",
                    error_code="plan_generation_failed",
                )

            # Validate plan structure
            error = _validate_plan_structure(plan, intent, snapshot)
            if error:
                return StageResult(
                    stage=3, passed=False,
                    duration_ms=_elapsed(start),
                    details=f"Invalid plan for {intent.app_id}: {error}",
                    error_code="invalid_plan_structure",
                )

            # For auto-triggered intents, test check_trigger
            if intent.config.trigger_type == TriggerType.AUTO_TRIGGERED and not trigger_checked:
                try:
                    trigger_result = await asyncio.wait_for(
                        session.check_trigger(intent, state, snapshot),
                        timeout=10,
                    )
                    if not isinstance(trigger_result, bool):
                        return StageResult(
                            stage=3, passed=False,
                            duration_ms=_elapsed(start),
                            details="check_trigger must return bool",
                            error_code="invalid_trigger_response",
                        )
                    trigger_checked = True
                except asyncio.TimeoutError:
                    return StageResult(
                        stage=3, passed=False,
                        duration_ms=_elapsed(start),
                        details="check_trigger timed out",
                        error_code="trigger_timeout",
                    )

            passed_count += 1

    except SolverTimeoutError as exc:
        return StageResult(
            stage=3, passed=False,
            duration_ms=_elapsed(start),
            details=f"Solver timed out: {exc}",
            error_code="plan_timeout",
        )
    except SolverCrashedError as exc:
        return StageResult(
            stage=3, passed=False,
            duration_ms=_elapsed(start),
            details=f"Solver crashed: {exc}",
            error_code="plan_generation_failed",
        )
    except Exception as exc:
        return StageResult(
            stage=3, passed=False,
            duration_ms=_elapsed(start),
            details=f"Unexpected error: {exc}",
            error_code="plan_generation_failed",
        )
    finally:
        await session.shutdown()

    trigger_msg = ", trigger check OK" if trigger_checked else ""
    return StageResult(
        stage=3, passed=True,
        duration_ms=_elapsed(start),
        details=f"{passed_count}/{total_count} plans valid{trigger_msg}",
    )


def _validate_plan_structure(plan: Any, intent: Any, snapshot: Any) -> str | None:
    """Validate structural correctness of a plan. Returns error string or None."""
    if not hasattr(plan, "intent_id"):
        return "plan missing intent_id"

    if plan.intent_id != intent.app_id:
        return f"intent_id mismatch: got {plan.intent_id}, expected {intent.app_id}"

    if not hasattr(plan, "interactions") or len(plan.interactions) == 0:
        return "plan has no interactions"

    snapshot_ts = snapshot.timestamp if snapshot is not None else 0
    if snapshot_ts > 0 and plan.deadline <= snapshot_ts:
        return f"deadline {plan.deadline} is not after snapshot timestamp {snapshot_ts}"

    for i, ix in enumerate(plan.interactions):
        if not ix.target.startswith("0x"):
            return f"interaction[{i}].target must start with 0x: {ix.target}"
        if len(ix.target) != 42:
            return f"interaction[{i}].target must be 42 chars: {ix.target} ({len(ix.target)})"
        if not ix.call_data.startswith("0x"):
            return f"interaction[{i}].call_data must start with 0x: {ix.call_data}"

    return None


# ═══════════════════════════════════════════════════════════════════════════════
#                          PIPELINE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════


class ScreeningPipeline:
    """Runs the full 3-stage screening pipeline on a submission."""

    async def run_all(
        self,
        repo_path: str,
        commit_hash: str = "unknown",
    ) -> ScreeningResult:
        """Run all screening stages sequentially.

        Stops at the first failing stage — later stages are not attempted.

        Args:
            repo_path: Path to the cloned repo.
            commit_hash: Git commit hash (for image tagging).

        Returns:
            ScreeningResult with aggregate pass/fail and per-stage details.
        """
        result = ScreeningResult()
        image_tag = f"solver-{commit_hash[:12]}:screening"

        # Stage 1: Static checks
        logger.info("Screening Stage 1: Static checks for %s", repo_path)
        s1 = run_stage_1(repo_path)
        result.stages.append(s1)

        if not s1.passed:
            result.failed_stage = 1
            result.rejection_reason = f"Stage 1: {s1.error_code} — {s1.details}"
            logger.warning("Stage 1 FAILED: %s", s1.details)
            return result

        logger.info("Stage 1 passed: %s", s1.details)

        # Stage 2: Build check
        logger.info("Screening Stage 2: Build check for %s", repo_path)
        s2 = await run_stage_2(repo_path, image_tag)
        result.stages.append(s2)
        result.image_tag = image_tag if s2.passed else None

        if not s2.passed:
            result.failed_stage = 2
            result.rejection_reason = f"Stage 2: {s2.error_code} — {s2.details}"
            logger.warning("Stage 2 FAILED: %s", s2.details)
            # Clean up failed image
            await _cleanup_image(image_tag)
            return result

        # Extract solver info from stage 2 details
        if ":" in s2.details:
            try:
                info_part = s2.details.split(": ", 1)[1]
                name_ver = info_part.split(" v")
                result.solver_name = name_ver[0]
                if len(name_ver) > 1:
                    result.solver_version = name_ver[1]
            except (IndexError, ValueError):
                pass

        logger.info("Stage 2 passed: %s", s2.details)

        # Stage 3: Smoke test
        logger.info("Screening Stage 3: Smoke test for %s", image_tag)
        s3 = await run_stage_3(image_tag)
        result.stages.append(s3)

        if not s3.passed:
            result.failed_stage = 3
            result.rejection_reason = f"Stage 3: {s3.error_code} — {s3.details}"
            logger.warning("Stage 3 FAILED: %s", s3.details)
            await _cleanup_image(image_tag)
            return result

        logger.info("Stage 3 passed: %s", s3.details)

        # All stages passed
        result.passed = True
        logger.info(
            "Screening PASSED for %s (solver: %s v%s)",
            commit_hash[:12],
            result.solver_name or "unknown",
            result.solver_version or "unknown",
        )
        return result

    async def run_stage_1_only(self, repo_path: str) -> StageResult:
        """Run only stage 1 (static checks). No Docker required."""
        return run_stage_1(repo_path)


_FROM_LINE = re.compile(
    r"^\s*FROM\s+([^\s]+)",
    re.MULTILINE | re.IGNORECASE,
)


async def _docker_inspect_entrypoint_and_cmd(
    image_ref: str,
) -> tuple[list[str] | None, list[str] | None] | None:
    """Return (Entrypoint, Cmd) from `docker image inspect`, or None on error.

    Tries `docker image inspect` first (works on local image); falls back to
    `docker pull` then inspect for base images not yet cached locally.
    """
    proc = await asyncio.create_subprocess_exec(
        "docker", "image", "inspect",
        "--format", "{{json .Config.Entrypoint}}|{{json .Config.Cmd}}",
        image_ref,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        # Try pulling once in case it's a remote base image.
        pull = await asyncio.create_subprocess_exec(
            "docker", "pull", "--quiet", image_ref,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await pull.wait()
        if pull.returncode != 0:
            return None
        proc2 = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect",
            "--format", "{{json .Config.Entrypoint}}|{{json .Config.Cmd}}",
            image_ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc2.communicate()
        if proc2.returncode != 0:
            return None
    raw = stdout.decode("utf-8", errors="replace").strip()
    entrypoint_json, _, cmd_json = raw.partition("|")
    try:
        entrypoint = json.loads(entrypoint_json) if entrypoint_json else None
        cmd = json.loads(cmd_json) if cmd_json else None
    except json.JSONDecodeError:
        return None
    return entrypoint, cmd


async def _verify_entrypoint_unchanged(
    repo_path: str,
    built_image_tag: str,
) -> str | None:
    """Ensure the built image's Entrypoint/Cmd match the base image's exactly.

    Returns an error string to surface in the Stage 2 result, or None on pass.
    """
    try:
        dockerfile_text = Path(repo_path, "Dockerfile").read_text()
    except Exception as exc:
        return f"Could not read Dockerfile to extract base image: {exc}"

    match = _FROM_LINE.search(dockerfile_text)
    if not match:
        return "Could not find FROM line in Dockerfile (runtime entrypoint check aborted)"
    base_ref = match.group(1).strip()

    base_insp = await _docker_inspect_entrypoint_and_cmd(base_ref)
    built_insp = await _docker_inspect_entrypoint_and_cmd(built_image_tag)

    if base_insp is None:
        return (
            f"Could not inspect base image {base_ref!r} to verify entrypoint. "
            "If this is a transient registry issue, retry; otherwise the "
            "base image ref is malformed."
        )
    if built_insp is None:
        return f"Could not inspect built image {built_image_tag!r} after build"

    base_ep, base_cmd = base_insp
    built_ep, built_cmd = built_insp

    if built_ep != base_ep:
        return (
            f"Dockerfile overrode ENTRYPOINT: base={base_ep!r} built={built_ep!r}. "
            "Submissions must inherit the harness entrypoint from the "
            "approved solver-base image unchanged."
        )
    if built_cmd != base_cmd:
        return (
            f"Dockerfile overrode CMD: base={base_cmd!r} built={built_cmd!r}. "
            "Submissions must inherit the harness CMD unchanged."
        )
    return None


async def _cleanup_image(image_tag: str) -> None:
    """Remove a Docker image, ignoring errors."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rmi", "-f", image_tag,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#                          HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _elapsed(start: float) -> int:
    """Return elapsed milliseconds since start."""
    return int((time.monotonic() - start) * 1000)


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory, excluding .git."""
    total = 0
    for f in path.rglob("*"):
        if ".git" in f.parts:
            continue
        if f.is_file():
            total += f.stat().st_size
    return total
