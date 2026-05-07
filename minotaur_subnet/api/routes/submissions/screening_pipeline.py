"""Code validation and screening pipeline for solver submissions.

Handles:
- Source code submission handling
- Docker image build triggering
- 3-stage screening (syntax, sandbox, benchmark)
- Git clone and commit verification
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from urllib.parse import urlparse

from minotaur_subnet.harness.submission_store import SubmissionStatus
from minotaur_subnet.harness.provenance import create_signed_provenance

from .state import get_store

logger = logging.getLogger(__name__)


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_host_allowlist(raw: str) -> set[str]:
    """Parse a comma-separated host allowlist from operator config."""
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _build_git_process_env(repo_url: str) -> tuple[dict[str, str], str | None]:
    """Build a non-interactive git env and optional scoped helper file."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    parsed = urlparse(repo_url)

    if parsed.scheme == "file":
        # Local testnet `file://` repos are bind-mounted from the host, so git
        # inside the container sees them as foreign-owned and refuses to clone
        # unless the source is marked safe in protected config. Keep the trust
        # scoped to this subprocess via a temporary global config file.
        fd, git_config_path = tempfile.mkstemp(
            prefix="minotaur-git-config-",
            suffix=".gitconfig",
            text=True,
        )
        with os.fdopen(fd, "w") as handle:
            handle.write("[safe]\n")
            handle.write("\tdirectory = *\n")
            handle.write('[protocol "file"]\n')
            handle.write("\tallow = always\n")
        env["GIT_CONFIG_GLOBAL"] = git_config_path
        return env, git_config_path

    username = os.environ.get("SUBMISSION_GIT_CLONE_USERNAME", "").strip()
    password = os.environ.get("SUBMISSION_GIT_CLONE_PASSWORD", "").strip()
    if not username and not password:
        return env, None
    if not username or not password:
        logger.warning(
            "Ignoring partial private repo clone credentials; both "
            "SUBMISSION_GIT_CLONE_USERNAME and SUBMISSION_GIT_CLONE_PASSWORD "
            "must be set"
        )
        return env, None

    allowed_hosts = _parse_host_allowlist(
        os.environ.get("SUBMISSION_GIT_CLONE_ALLOWED_HOSTS", "")
    )
    if not allowed_hosts:
        logger.warning(
            "Ignoring private repo clone credentials because "
            "SUBMISSION_GIT_CLONE_ALLOWED_HOSTS is unset"
        )
        return env, None

    repo_host = (urlparse(repo_url).hostname or "").lower()
    if repo_host not in allowed_hosts:
        return env, None

    fd, askpass_path = tempfile.mkstemp(
        prefix="minotaur-git-askpass-",
        suffix=".sh",
        text=True,
    )
    with os.fdopen(fd, "w") as handle:
        handle.write("#!/bin/sh\n")
        handle.write('case "$1" in\n')
        handle.write('  *Username*) printf "%s\\n" "$MINOTAUR_GIT_CLONE_USERNAME" ;;\n')
        handle.write('  *Password*) printf "%s\\n" "$MINOTAUR_GIT_CLONE_PASSWORD" ;;\n')
        handle.write('  *) printf "\\n" ;;\n')
        handle.write("esac\n")
    os.chmod(askpass_path, 0o700)

    env["GIT_ASKPASS"] = askpass_path
    env["MINOTAUR_GIT_CLONE_USERNAME"] = username
    env["MINOTAUR_GIT_CLONE_PASSWORD"] = password
    return env, askpass_path


def _cleanup_temp_file(path: str | None) -> None:
    """Best-effort cleanup for temporary helper files."""
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Failed to remove temporary helper file: %s", path)


async def _clone_repo(repo_url: str, commit_hash: str, dest: str) -> bool:
    """Clone a git repo at a specific commit.

    Returns True on success, False on failure.
    """
    git_env, askpass_path = _build_git_process_env(repo_url)
    try:
        # Clone the repo. Miners push to branches (miner/{id}), so the
        # commit may not be on the default branch. Strategy:
        # 1. Clone with --no-checkout (fast, no branch assumption)
        # 2. Fetch all branches so the miner's commit is available
        # 3. Checkout the specific commit
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--no-checkout", repo_url, dest,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=git_env,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            logger.warning(
                "Git clone failed: %s",
                stderr.decode("utf-8", errors="replace")[:300],
            )
            return False

        # Fetch all branches (miner commits live on miner/* branches)
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", dest, "fetch", "origin",
            "+refs/heads/*:refs/remotes/origin/*",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=git_env,
        )
        await asyncio.wait_for(proc.communicate(), timeout=120)

        # Checkout specific commit
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", dest, "checkout", commit_hash,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=git_env,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            logger.warning(
                "Git checkout %s failed: %s",
                commit_hash[:8],
                stderr.decode("utf-8", errors="replace")[:300],
            )
            return False

        return True

    except asyncio.TimeoutError:
        logger.warning("Git clone timed out for %s", repo_url)
        return False
    except FileNotFoundError:
        logger.error("git not found. Is git installed?")
        return False
    finally:
        _cleanup_temp_file(askpass_path)


async def _resolve_image_id(image_tag: str) -> str | None:
    """Resolve immutable local image ID (sha256:...) for a built tag."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", image_tag, "--format", "{{.Id}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        logger.warning("docker image inspect timed out for %s", image_tag)
        return None
    except FileNotFoundError:
        logger.warning("docker not found while resolving image ID for %s", image_tag)
        return None

    if proc.returncode != 0:
        logger.warning(
            "docker image inspect failed for %s: %s",
            image_tag,
            stderr.decode("utf-8", errors="replace")[:300],
        )
        return None

    image_id = stdout.decode("utf-8", errors="replace").strip()
    if not image_id.startswith("sha256:"):
        logger.warning("Unexpected image ID format for %s: %s", image_tag, image_id)
        return None
    return image_id


async def _run_screening_pipeline(submission_id: str) -> None:
    """Clone repo and run the 3-stage screening pipeline.

    Runs as a background task after submission creation.
    Updates the store with results as each stage completes.
    """
    store = get_store()
    sub = store.get(submission_id)
    if sub is None:
        logger.error("Submission %s not found for screening", submission_id)
        return

    repo_dir = None
    try:
        # Clone the repo
        repo_dir = tempfile.mkdtemp(prefix=f"solver-{sub.commit_hash[:8]}-")
        clone_ok = await _clone_repo(sub.repo_url, sub.commit_hash, repo_dir)
        if not clone_ok:
            store.reject(submission_id, "Failed to clone repository")
            return

        # Stage 1: Static checks
        store.update_status(submission_id, SubmissionStatus.SCREENING_STAGE_1)

        from minotaur_subnet.harness.screening import run_stage_1
        s1 = run_stage_1(repo_dir)
        store.set_screening_result(
            submission_id, stage=1,
            passed=s1.passed,
            duration_ms=s1.duration_ms,
            details=s1.details,
            error_code=s1.error_code,
        )
        if not s1.passed:
            return  # set_screening_result already rejected

        # Stage 2: Build check
        store.update_status(submission_id, SubmissionStatus.SCREENING_STAGE_2)

        image_tag = f"solver-{sub.commit_hash[:12]}:screening"
        from minotaur_subnet.harness.screening import run_stage_2
        s2 = await run_stage_2(repo_dir, image_tag)
        store.set_screening_result(
            submission_id, stage=2,
            passed=s2.passed,
            duration_ms=s2.duration_ms,
            details=s2.details,
            error_code=s2.error_code,
        )
        if not s2.passed:
            return

        store.set_image_tag(submission_id, image_tag)
        image_id = await _resolve_image_id(image_tag)
        if not image_id:
            store.reject(
                submission_id,
                (
                    "Stage 2 policy: failed to resolve immutable image ID "
                    f"for built image {image_tag}"
                ),
            )
            return
        store.set_image_id(submission_id, image_id)
        require_signed_provenance = _env_true("REQUIRE_SIGNED_PROVENANCE", default=False)
        require_asymmetric_provenance = _env_true("REQUIRE_ASYMMETRIC_PROVENANCE", default=False)
        signing_key = os.environ.get("SUBMISSION_PROVENANCE_HMAC_KEY", "").strip()
        signing_private_key = os.environ.get(
            "SUBMISSION_PROVENANCE_SIGNING_PRIVATE_KEY", "",
        ).strip()
        signing_address = os.environ.get("SUBMISSION_PROVENANCE_SIGNING_ADDRESS", "").strip()
        if require_asymmetric_provenance and not signing_private_key:
            store.reject(
                submission_id,
                (
                    "Stage 2 policy: REQUIRE_ASYMMETRIC_PROVENANCE=1 but "
                    "SUBMISSION_PROVENANCE_SIGNING_PRIVATE_KEY is unset"
                ),
            )
            return
        if require_signed_provenance and not signing_private_key and not signing_key:
            store.reject(
                submission_id,
                (
                    "Stage 2 policy: REQUIRE_SIGNED_PROVENANCE=1 but no provenance "
                    "signing key is configured"
                ),
            )
            return
        if signing_private_key or signing_key:
            try:
                provenance = create_signed_provenance(
                    submission_id=sub.submission_id,
                    repo_url=sub.repo_url,
                    commit_hash=sub.commit_hash,
                    image_id=image_id,
                    image_tag=image_tag,
                    signing_key=signing_key,
                    signing_private_key=signing_private_key,
                    signer_address=signing_address,
                )
            except Exception as exc:
                store.reject(
                    submission_id,
                    f"Stage 2 policy: failed to sign provenance ({exc})",
                )
                return
            store.set_provenance(submission_id, provenance)

        # Extract solver info from stage 2 details
        if ":" in s2.details:
            try:
                info_part = s2.details.split(": ", 1)[1]
                name_ver = info_part.split(" v")
                store.set_solver_info(
                    submission_id,
                    name=name_ver[0],
                    version=name_ver[1] if len(name_ver) > 1 else None,
                )
            except (IndexError, ValueError):
                pass

        # Stage 3: Smoke test
        store.update_status(submission_id, SubmissionStatus.SCREENING_STAGE_3)

        from minotaur_subnet.harness.screening import run_stage_3
        s3 = await run_stage_3(image_tag)
        store.set_screening_result(
            submission_id, stage=3,
            passed=s3.passed,
            duration_ms=s3.duration_ms,
            details=s3.details,
            error_code=s3.error_code,
        )
        if not s3.passed:
            return

        # All screening passed -- move to benchmarking queue
        store.update_status(submission_id, SubmissionStatus.BENCHMARKING)
        logger.info(
            "Submission %s passed screening, queued for benchmarking",
            submission_id,
        )

    except Exception as exc:
        logger.exception("Screening pipeline error for %s", submission_id)
        store.reject(submission_id, f"Screening error: {exc}")

    finally:
        if repo_dir and os.path.exists(repo_dir):
            shutil.rmtree(repo_dir, ignore_errors=True)
