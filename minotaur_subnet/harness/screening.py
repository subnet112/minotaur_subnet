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
                }
                for s in self.stages
            },
        }


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

    return StageResult(
        stage=1, passed=True,
        duration_ms=_elapsed(start),
        details="All static checks passed",
    )


# ═══════════════════════════════════════════════════════════════════════════════
#                          STAGE 2: BUILD CHECK
# ═══════════════════════════════════════════════════════════════════════════════


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

    # Step 1: Build Docker image
    build_cmd = [
        "docker", "build",
        "--network=none",
        "--memory=4g",
        "-t", image_tag,
        repo_path,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *build_cmd,
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
        # Initialize
        await asyncio.wait_for(
            session.initialize({"chain_ids": [1], "timeout_per_plan_ms": int(plan_timeout * 1000)}),
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
