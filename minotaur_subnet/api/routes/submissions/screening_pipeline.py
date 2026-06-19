"""Code validation and screening pipeline for solver submissions.

Handles:
- Source code submission handling
- Docker image build triggering
- 3-stage screening (syntax, sandbox, benchmark)
- Git clone and commit verification
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import shutil
import tarfile
import tempfile
from urllib.parse import urlparse

# Ephemeral sandbox used to fetch untrusted miner repos. The clone runs in a
# short-lived, hardened container (read-only rootfs, all caps dropped, no new
# privileges, pid/mem caps) instead of in the long-lived validator process, and
# the result is streamed back as a tar over stdout — so the validator image
# needs no git and never executes repo content during the fetch.
DEFAULT_CLONE_IMAGE = "alpine/git:2.45.2"
# Hard cap on the clone tarball (compressed stream + uncompressed total) to
# bound memory/disk against a hostile repo. 256 MiB is generous for a solver.
MAX_CLONE_TAR_BYTES = 256 * 1024 * 1024

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


def _resolve_clone_basic_auth(repo_url: str) -> str | None:
    """Return base64(user:pass) for a private https clone, honoring the host
    allowlist; None when creds are absent/partial or the host isn't allowed.

    Mirrors the policy in ``_build_git_process_env`` so the sandboxed clone path
    enforces the same private-repo credential scoping.
    """
    username = os.environ.get("SUBMISSION_GIT_CLONE_USERNAME", "").strip()
    password = os.environ.get("SUBMISSION_GIT_CLONE_PASSWORD", "").strip()
    if not username or not password:
        return None
    allowed_hosts = _parse_host_allowlist(
        os.environ.get("SUBMISSION_GIT_CLONE_ALLOWED_HOSTS", "")
    )
    if not allowed_hosts:
        logger.warning(
            "Ignoring private repo clone credentials because "
            "SUBMISSION_GIT_CLONE_ALLOWED_HOSTS is unset"
        )
        return None
    if (urlparse(repo_url).hostname or "").lower() not in allowed_hosts:
        return None
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def _safe_extract_tar(data: bytes, dest: str) -> bool:
    """Extract a clone tarball (from the sandbox's stdout) into ``dest`` with
    traversal/symlink/size guards. Returns True on success."""
    if not data:
        logger.warning("Clone sandbox produced an empty archive")
        return False
    if len(data) > MAX_CLONE_TAR_BYTES:
        logger.warning("Clone archive too large: %d bytes", len(data))
        return False
    os.makedirs(dest, exist_ok=True)
    dest_real = os.path.realpath(dest)
    prefix = dest_real + os.sep
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            members = tf.getmembers()
            for m in members:
                target = os.path.realpath(os.path.join(dest_real, m.name))
                if target != dest_real and not target.startswith(prefix):
                    logger.warning("Clone archive path traversal blocked: %s", m.name)
                    return False
                if m.issym() or m.islnk():
                    link_base = os.path.dirname(target)
                    link_target = os.path.realpath(os.path.join(link_base, m.linkname))
                    if link_target != dest_real and not link_target.startswith(prefix):
                        logger.warning("Clone archive unsafe link blocked: %s", m.name)
                        return False
                total += max(0, m.size)
                if total > MAX_CLONE_TAR_BYTES:
                    logger.warning("Clone archive uncompressed size exceeds cap")
                    return False
            # Validated; extract with the stdlib data filter as defense in depth.
            try:
                tf.extractall(dest_real, filter="data")
            except TypeError:  # Python < 3.12 has no extraction filter kwarg
                tf.extractall(dest_real)
        return True
    except Exception as exc:  # noqa: BLE001 — any tar error => clone failed
        logger.warning("Clone archive extraction failed: %s", exc)
        return False


async def _clone_repo_sandboxed(repo_url: str, commit_hash: str, dest: str) -> bool:
    """Fetch a miner repo at ``commit_hash`` inside an ephemeral, hardened
    container and extract the result into ``dest``.

    The container gets network egress (to reach the git host) but is otherwise
    locked down: read-only rootfs with tmpfs scratch, all caps dropped, no new
    privileges, and pid/cpu/memory caps. Only a tar of the checked-out tree is
    streamed back over stdout; nothing from the repo executes here.
    """
    image = os.environ.get("SUBMISSION_CLONE_IMAGE", "").strip() or DEFAULT_CLONE_IMAGE
    network = os.environ.get("SUBMISSION_CLONE_NETWORK", "").strip() or "bridge"
    basic_auth = _resolve_clone_basic_auth(repo_url)

    # git/tar progress -> stderr so stdout carries only the tarball. Repo URL and
    # commit arrive via env (referenced as "$REPO_URL"/"$COMMIT") so a hostile
    # value can't break out of the argv. Auth (when present) is sent as an
    # http.extraHeader from $GIT_BASIC_AUTH, never on the command line.
    hdr = '-c "http.extraHeader=Authorization: Basic $GIT_BASIC_AUTH" ' if basic_auth else ""
    script = (
        "set -e; export HOME=/tmp; "
        f'git {hdr}clone --no-checkout "$REPO_URL" /clone >&2; '
        f'git {hdr}-C /clone fetch origin "+refs/heads/*:refs/remotes/origin/*" >&2; '
        'git -C /clone checkout "$COMMIT" >&2; '
        "tar -C /clone -cf - ."
    )
    cmd = [
        "docker", "run", "--rm",
        # The alpine/git image's ENTRYPOINT is `git`; override to a shell so the
        # clone+fetch+checkout+tar script runs (and stays image-agnostic).
        "--entrypoint", "sh",
        "--network", network,
        "--read-only",
        "--tmpfs", "/clone:rw,exec,nosuid,size=512m",
        "--tmpfs", "/tmp:rw,exec,nosuid,size=64m",
        "--cap-drop=ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit=256",
        "--memory=2g",
        "--cpus=2",
        "-e", "GIT_TERMINAL_PROMPT=0",
        "-e", "REPO_URL",
        "-e", "COMMIT",
    ]
    if basic_auth:
        cmd += ["-e", "GIT_BASIC_AUTH"]
    cmd += [image, "-c", script]

    run_env = os.environ.copy()
    run_env["REPO_URL"] = repo_url
    run_env["COMMIT"] = commit_hash
    if basic_auth:
        run_env["GIT_BASIC_AUTH"] = basic_auth

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=run_env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
    except asyncio.TimeoutError:
        logger.warning("Clone sandbox timed out for %s", repo_url)
        return False
    except FileNotFoundError:
        logger.error("docker CLI not found; cannot run clone sandbox")
        return False

    if proc.returncode != 0:
        logger.warning(
            "Clone sandbox failed (rc=%s): %s",
            proc.returncode,
            stderr.decode("utf-8", errors="replace")[:300],
        )
        return False
    return _safe_extract_tar(stdout, dest)


async def _clone_repo(repo_url: str, commit_hash: str, dest: str) -> bool:
    """Clone a git repo at a specific commit.

    http(s) repos (the production path) are fetched in an ephemeral hardened
    container (``_clone_repo_sandboxed``) so the validator process never runs
    git on untrusted input. ``file://`` repos — only used by the bind-mounted
    local-testnet stack — keep the in-process clone, which understands the
    host-foreign-ownership trust dance.

    Returns True on success, False on failure.
    """
    scheme = (urlparse(repo_url).scheme or "").lower()
    if scheme in ("http", "https"):
        return await _clone_repo_sandboxed(repo_url, commit_hash, dest)
    return await _clone_repo_in_process(repo_url, commit_hash, dest)


async def _clone_repo_in_process(repo_url: str, commit_hash: str, dest: str) -> bool:
    """Clone directly in the validator process (used for local-testnet
    ``file://`` repos). Requires git on PATH."""
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


async def _push_candidate_image(image_tag: str, pr_number: int) -> str | None:
    """Retag the locally-built screening image to the candidate repo, push it
    (single-arch — the local build is already single-arch), and return the
    resolved ``<repo>@sha256:<digest>`` manifest ref.

    This is the content-addressed transport step: it distributes the leader's
    sandbox-built image so every follower can pull byte-identical bytes by digest
    (no per-host rebuild → no ``{{.Id}}`` divergence). Best-effort: returns
    ``None`` on any docker/registry failure (the leader can still benchmark its
    local build; only fleet distribution is affected). Only the leader runs this,
    gated by ``leader_pushes_digests()`` at the call site, so it is inert until a
    ``CANDIDATE_IMAGE_REPO`` is configured.
    """
    from minotaur_subnet.harness.image_transport import candidate_repo, is_digest_ref

    repo = candidate_repo()
    ref = f"{repo}:pr-{pr_number}"

    async def _docker(*args: str, timeout: float) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return (proc.returncode or 0), out.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            return 124, "timed out"
        except FileNotFoundError:
            return 127, "docker not found"

    rc, msg = await _docker("tag", image_tag, ref, timeout=30)
    if rc != 0:
        logger.warning("Candidate retag %s -> %s failed: %s", image_tag, ref, msg[:200])
        return None
    rc, msg = await _docker("push", ref, timeout=600)
    if rc != 0:
        logger.warning("Candidate push %s failed: %s", ref, msg[:300])
        return None
    # RepoDigests is populated by the registry after a successful push.
    rc, msg = await _docker(
        "image", "inspect", ref, "--format", "{{index .RepoDigests 0}}", timeout=30,
    )
    digest_ref = msg.strip()
    if rc != 0 or not is_digest_ref(digest_ref):
        logger.warning("Could not resolve RepoDigest for %s (rc=%s): %s", ref, rc, msg[:200])
        return None
    return digest_ref


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

        # Content-addressed transport (leader-only, inert until CANDIDATE_IMAGE_REPO
        # is set): push the built image to the candidate repo and persist its GHCR
        # manifest digest so followers pull byte-identical bytes by digest. The
        # local build above is what the leader benchmarks; this just distributes it.
        from minotaur_subnet.harness.image_transport import leader_pushes_digests
        if leader_pushes_digests() and sub.pr_number:
            digest_ref = await _push_candidate_image(image_tag, sub.pr_number)
            if digest_ref:
                store.set_image_digest(submission_id, digest_ref)
                logger.info("Candidate image pushed for %s: %s", submission_id, digest_ref)
            else:
                logger.warning(
                    "Candidate push failed for %s; image_digest unset (followers "
                    "cannot pull-by-digest until this succeeds).", submission_id,
                )

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
