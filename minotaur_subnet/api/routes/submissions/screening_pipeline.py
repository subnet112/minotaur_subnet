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
import re
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

# Hard cap on the NUMBER of tar members. The byte cap alone doesn't stop a repo
# of e.g. 500k zero-byte files (total uncompressed size stays ~0) from exhausting
# host INODES on extract. A legit solver repo has hundreds–low-thousands of
# files; 50k is far above any real tree yet blocks the inode-bomb.
MAX_CLONE_TAR_MEMBERS = 50_000

from minotaur_subnet.harness.actor import snapshot_resolver
from minotaur_subnet.harness.submission_store import (
    OUTCOME_BUILD_BUDGET,
    OUTCOME_COPYCAT_CODE,
    SubmissionStatus,
    offload_write,
)
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


def _max_rounds_per_fingerprint() -> int:
    """Cross-hotkey benched-round cap per NORMALIZED content fingerprint.

    ``SUBMISSIONS_MAX_ROUNDS_PER_FINGERPRINT`` (default 2 — the value the
    leader has run via env; it shipped 0/disabled so the merge was inert, and
    the default now lives in CODE so a leader failover keeps the guard without
    anyone re-arming an env var. 0 disables). Complements the per-(hotkey,
    commit) cap: that one stops naive same-SHA resubmit automation, this one
    stops the two evasions it explicitly cannot — cosmetic hash rotation
    (nonce comments) and sybil spread (one tree, many hotkeys). Leader-local
    intake policy, NOT consensus-relevant: followers mirror the leader's
    post-intake snapshot either way.
    """
    raw = os.environ.get("SUBMISSIONS_MAX_ROUNDS_PER_FINGERPRINT", "2").strip()
    try:
        return int(raw)
    except ValueError:
        return 2


def _reject_cross_actor_copies() -> bool:
    """Cross-ACTOR identical-code reject (``SUBMISSIONS_REJECT_CROSS_ACTOR_FP``,
    default on; 0 disables).

    The EARLIEST submitter of a normalized fingerprint owns it; a submission
    of the same fingerprint whose actor (harness/actor.py — on-chain coldkey)
    determinately differs from the owner's is rejected at stage 1, before it
    can cost a build unit or a slate seat. Unlike the benched-rounds quota
    below this needs no benches to arm, so five UIDs shipping one tree in one
    round lose four copies immediately. The rule only ever acts on POSITIVE
    coldkey attribution: same-actor resubmits pass (the waitlist resubmit
    loop is the designed no-fault path — rejecting it is the false-positive
    trap the 2026-07-22 audit measured at 41.8% of serious-miner
    submissions), and unmapped hotkeys / no coldkey data / the
    SOLVER_ACTOR_KEY=hotkey kill-switch make the check stand down rather than
    guess. Leader-local intake policy, like every cap here.
    """
    return _env_true("SUBMISSIONS_REJECT_CROSS_ACTOR_FP", default=True)


# Statuses the retroactive copy sweep may reject: pre-bench only. A copy that
# already reached the bench (BENCHMARKING/SCORED/ADOPTED) is the slate's
# problem — rejecting it mid-bench would bust slate-width accounting — and
# terminal states are already out of the running.
_SWEEPABLE_STATUSES = frozenset({
    "queued",
    "screening_stage_1",
    "screening_stage_2",
    "screening_stage_3",
    "pending_selection",
})


def evaluate_fingerprint_ownership(
    entries: list[tuple[str, float, str, str]],
    *,
    submission_id: str,
    hotkey: str,
    created_at: float,
    resolver: Any,
) -> tuple[tuple[str, float, str, str] | None, list[str]]:
    """Who owns this fingerprint, and which in-flight copies to sweep.

    ``entries`` are ``(hotkey, created_at, submission_id, status)`` for every
    OTHER submission carrying the fingerprint. Returns ``(owner_prior,
    sweep_ids)``:

    * ``owner_prior`` — the entry proving ANOTHER actor owns the fingerprint
      (the globally-earliest submitter by ``(created_at, submission_id)``),
      meaning THIS submission is a copy and must be rejected. None when this
      submission's actor owns the fingerprint (first submitter, or a
      same-actor sibling of it).
    * ``sweep_ids`` — when (and only when) this submission's actor is
      determinately the owner: ids of OTHER actors' pre-bench copies to
      reject retroactively. This closes the concurrent-copy race: two copies
      screening simultaneously can each miss the other's not-yet-persisted
      fingerprint, but whichever check runs LAST sees the full picture and
      sweeps the escapee — ordering alone cannot, because visibility (not
      creation order) decides which check sees what.

    Actor comparison is STRICT: same hotkey => same actor; otherwise both
    hotkeys must be coldkey-MAPPED to be called different. An unmapped hotkey
    (deregistered original, pre-sync map) is INDETERMINATE — never a reject,
    never a sweep. Degraded attribution must degrade to allowing, not to
    terminally rejecting the rightful owner's resubmit.
    """
    def _same_actor(hk_a: str, hk_b: str) -> bool | None:
        """True/False on positive attribution, None when indeterminate."""
        if (hk_a or "") == (hk_b or ""):
            return True
        ck_a, ck_b = resolver.mapped(hk_a), resolver.mapped(hk_b)
        if ck_a is None or ck_b is None:
            return None
        return ck_a == ck_b

    my_order = (float(created_at or 0.0), submission_id)
    earliest: tuple[str, float, str, str] | None = None
    for entry in entries:
        if earliest is None or (entry[1], entry[2]) < (earliest[1], earliest[2]):
            earliest = entry

    if earliest is None or (earliest[1], earliest[2]) > my_order:
        # I am the fingerprint's first submitter: owner. Sweep other actors'
        # in-flight copies (all of them — an earlier-created copy that raced
        # past its own check is still a copy).
        sweep = [
            sid for hk, _ts, sid, status in entries
            if status in _SWEEPABLE_STATUSES and _same_actor(hotkey, hk) is False
        ]
        return None, sweep

    owner_same = _same_actor(hotkey, earliest[0])
    if owner_same is False:
        return earliest, []
    if owner_same is True:
        # Sibling resubmit of my own actor's code: legitimate. Sweep copies
        # by actors that determinately differ from MINE (== the owner's).
        sweep = [
            sid for hk, _ts, sid, status in entries
            if status in _SWEEPABLE_STATUSES and _same_actor(hotkey, hk) is False
        ]
        return None, sweep
    # Indeterminate owner (unmapped hotkey): stand down entirely.
    return None, []


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
            if len(members) > MAX_CLONE_TAR_MEMBERS:
                logger.warning(
                    "Clone archive member count %d exceeds cap %d (inode-bomb guard)",
                    len(members), MAX_CLONE_TAR_MEMBERS,
                )
                return False
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


def _token_basic_auth(repo_url: str, token: str) -> str | None:
    """base64('x-access-token:<PAT>') for a github.com private clone.

    The per-submission GitHub PAT authenticates the clone as HTTP basic auth in
    the ``x-access-token`` form GitHub accepts for tokens. Hard-gated to
    github.com (resolve_pr already restricts the head clone_url to that host) so
    a token can never be leaked to another host via a crafted URL.
    """
    host = (urlparse(repo_url).hostname or "").lower()
    if host != "github.com":
        logger.warning("Refusing token clone for non-github host %r", host)
        return None
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


async def _clone_repo_sandboxed(
    repo_url: str, commit_hash: str, dest: str, *, token: str | None = None,
) -> bool:
    """Fetch a miner repo at ``commit_hash`` inside an ephemeral, hardened
    container and extract the result into ``dest``.

    The container gets network egress (to reach the git host) but is otherwise
    locked down: read-only rootfs with tmpfs scratch, all caps dropped, no new
    privileges, and pid/cpu/memory caps. Only a tar of the checked-out tree is
    streamed back over stdout; nothing from the repo executes here.

    ``token`` (private path) authenticates the clone with the per-submission PAT;
    otherwise the env-configured clone credentials apply (``_resolve_clone_basic_auth``).
    """
    image = os.environ.get("SUBMISSION_CLONE_IMAGE", "").strip() or DEFAULT_CLONE_IMAGE
    network = os.environ.get("SUBMISSION_CLONE_NETWORK", "").strip() or "bridge"
    basic_auth = _token_basic_auth(repo_url, token) if token else _resolve_clone_basic_auth(repo_url)
    if token and not basic_auth:
        return False  # token clone requested but host disallowed — fail closed

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


# Retry knobs for the sandboxed clone. The miner tree is streamed back as a tar
# over the sandbox's stdout, which can arrive TRUNCATED on a transient network
# blip — _safe_extract_tar then fails with "unexpected end of data" and an
# otherwise-valid submission is rejected ("Failed to clone repository"). A fresh
# re-clone almost always succeeds, so retry a few times with a short backoff.
_CLONE_RETRY_BACKOFF_SECONDS = 2.0


def _clone_attempts() -> int:
    """Total sandboxed-clone attempts (1 + retries). Env ``SUBMISSION_CLONE_RETRIES``
    (default 2); 0 disables retries. Retries only absorb transient failures; a
    genuinely bad repo still fails every attempt (a bounded few extra tries)."""
    try:
        retries = int(os.environ.get("SUBMISSION_CLONE_RETRIES", "2"))
    except ValueError:
        retries = 2
    return 1 + max(0, retries)


def _clear_dir(path: str) -> None:
    """Empty a directory in place (keep the dir itself) so a clone retry extracts
    into a clean tree — a failed attempt may have left a partial extraction.
    Best-effort; never raises."""
    try:
        entries = os.listdir(path)
    except OSError:
        return
    for name in entries:
        p = os.path.join(path, name)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.unlink(p)
        except OSError:
            pass


async def _clone_repo(
    repo_url: str, commit_hash: str, dest: str, *, token: str | None = None,
) -> bool:
    """Clone a git repo at a specific commit.

    http(s) repos (the production path) are fetched in an ephemeral hardened
    container (``_clone_repo_sandboxed``) so the validator process never runs
    git on untrusted input, and are RETRIED on a transient failure (a truncated
    tarball / network blip) — see ``_clone_attempts``. ``file://`` repos — only
    used by the bind-mounted local-testnet stack — keep the in-process clone,
    which understands the host-foreign-ownership trust dance.

    ``token`` (private path) is the per-submission GitHub PAT used to authenticate
    the https clone of the miner's private repo.

    Returns True on success, False on failure.
    """
    scheme = (urlparse(repo_url).scheme or "").lower()
    if scheme not in ("http", "https"):
        return await _clone_repo_in_process(repo_url, commit_hash, dest)

    attempts = _clone_attempts()
    for i in range(1, attempts + 1):
        if i > 1:
            _clear_dir(dest)  # discard any partial extraction from the prior try
        if await _clone_repo_sandboxed(repo_url, commit_hash, dest, token=token):
            if i > 1:
                logger.info(
                    "Clone succeeded for %s on attempt %d/%d", repo_url, i, attempts,
                )
            return True
        if i < attempts:
            delay = _CLONE_RETRY_BACKOFF_SECONDS * i
            logger.warning(
                "Clone attempt %d/%d failed for %s; retrying in %.0fs",
                i, attempts, repo_url, delay,
            )
            await asyncio.sleep(delay)
    return False


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


# Post-push manifest-retrievability verification. A ``docker push`` can return
# rc=0 with the manifest not yet durably retrievable from the registry — on the
# containerd image store ``RepoDigests`` is read from the LOCAL store, not the
# registry, so the digest looks good locally while a follower 404s on pull
# (observed live on GHCR: rc=0, blobs uploaded, manifest 404 for minutes until a
# re-push). These bound the verify+re-push loop in ``_verify_digest_retrievable``.
_PUSH_VERIFY_ATTEMPTS = 5
_PUSH_VERIFY_BACKOFF_SECONDS = 3.0


async def _verify_digest_retrievable(
    ref: str,
    digest_ref: str,
    docker_fn,
    *,
    attempts: int = _PUSH_VERIFY_ATTEMPTS,
    backoff_seconds: float = _PUSH_VERIFY_BACKOFF_SECONDS,
    sleep_fn=asyncio.sleep,
) -> str | None:
    """Confirm ``<repo>@sha256:D`` is actually RETRIEVABLE from the registry.

    The leader must never propose a digest the fleet can't pull: a follower that
    404s on the candidate image returns REJECT-on-pull, which reads as solver
    dissent and silently breaks champion consensus. ``docker manifest inspect``
    queries the REGISTRY (unlike ``RepoDigests``, which the containerd image
    store populates from the LOCAL store), so it detects a push that returned
    rc=0 but didn't durably land the manifest. Between failed attempts it
    re-pushes — the empirically confirmed remedy — with linear backoff to also
    ride out registry propagation, and returns ``None`` if the digest never
    becomes retrievable so the caller fails closed (``image_digest`` unset).

    ``docker_fn`` is the same ``async (*args, timeout) -> (rc, msg)`` runner the
    push uses; injectable so the verify+re-push loop is unit-testable without a
    registry.
    """
    for attempt in range(attempts):
        rc, _msg = await docker_fn("manifest", "inspect", digest_ref, timeout=30)
        if rc == 0:
            if attempt:
                logger.info(
                    "Candidate digest %s retrievable after %d re-push attempt(s)",
                    digest_ref, attempt,
                )
            return digest_ref
        if attempt < attempts - 1:
            logger.warning(
                "Candidate digest %s not yet registry-retrievable "
                "(attempt %d/%d); re-pushing %s",
                digest_ref, attempt + 1, attempts, ref,
            )
            await docker_fn("push", ref, timeout=600)
            await sleep_fn(backoff_seconds * (attempt + 1))
    logger.warning(
        "Candidate digest %s NOT registry-retrievable after %d attempts; failing "
        "closed (image_digest unset — leader will not propose an unpullable digest)",
        digest_ref, attempts,
    )
    return None


def _safe_image_tag(value: str) -> str:
    """Sanitize an arbitrary id into a valid Docker/OCI tag component
    ([A-Za-z0-9_.-], ≤128 chars, not leading with ``.``/``-``)."""
    t = re.sub(r"[^A-Za-z0-9_.-]", "-", value or "")[:128].lstrip(".-")
    return t or "unknown"


async def _push_candidate_image(
    image_tag: str, pr_number: int, submission_id: str = "",
) -> str | None:
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

    The image is pushed under TWO tags:
      * ``pr-<pr_number>`` — human-friendly, but REUSED across every candidate
        from the same PR (many champions come from one long-lived private PR), so
        each new push moves this tag off the prior digest.
      * ``sub-<submission_id>`` — a per-submission tag that is NEVER reused, so
        every pushed digest keeps at least one tag for the life of the package.
    The second tag fixes a real outage: GHCR retention DELETES untagged package
    versions, so once ``pr-<N>`` moved to a newer candidate the prior (possibly
    still-adopted CHAMPION) digest became untagged and was pruned — its incumbent
    re-benchmark then failed "image not found", aborting every round. A stable
    per-submission tag keeps the champion's image retention-safe.
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

    # Also push a per-submission tag so this digest is NEVER left untagged (and
    # thus retention-prunable) when a later candidate from the same PR moves the
    # ``pr-<N>`` tag off it. Best-effort: a failure here doesn't fail the push
    # (the pr-<N> tag + returned digest still work short-term), but it re-opens
    # the retention-GC window, so warn loudly.
    if submission_id:
        stable_ref = f"{repo}:sub-{_safe_image_tag(submission_id)}"
        rc_s, msg_s = await _docker("tag", image_tag, stable_ref, timeout=30)
        if rc_s == 0:
            rc_s, msg_s = await _docker("push", stable_ref, timeout=600)
        if rc_s != 0:
            logger.warning(
                "Stable per-submission tag push %s failed (digest is now "
                "retention-prunable once pr-%s moves): %s",
                stable_ref, pr_number, msg_s[:200],
            )

    # RepoDigests is populated by the registry after a successful push. An image
    # built FROM a base carries MULTIPLE RepoDigests (the base repo + ours), so we
    # must pick the entry for the repo we just pushed to — NOT index 0, which can
    # be the base/source repo's digest (caught on a real registry: index 0 was a
    # stale source repo, giving the wrong digest entirely).
    import json as _json
    rc, msg = await _docker(
        "image", "inspect", ref, "--format", "{{json .RepoDigests}}", timeout=30,
    )
    if rc != 0:
        logger.warning("Could not inspect RepoDigests for %s (rc=%s): %s", ref, rc, msg[:200])
        return None
    try:
        repo_digests = _json.loads(msg.strip() or "[]")
    except ValueError:
        logger.warning("Malformed RepoDigests for %s: %s", ref, msg[:200])
        return None
    prefix = f"{repo}@sha256:"
    digest_ref = next((d for d in repo_digests if isinstance(d, str) and d.startswith(prefix)), None)
    if not digest_ref or not is_digest_ref(digest_ref):
        logger.warning(
            "No RepoDigest matching %s after push (got %s)", prefix, repo_digests,
        )
        return None
    # ``docker push`` rc=0 + the (local-store) RepoDigests are NOT proof the
    # fleet can pull this digest — verify it's registry-retrievable (re-pushing
    # if not) before trusting it, else fail closed.
    return await _verify_digest_retrievable(ref, digest_ref, _docker)


# Statuses a live pipeline task walks through — a submission parked in one of
# these with NO running task is stranded (its task died with the process).
_RESUMABLE_STATUSES = (
    SubmissionStatus.QUEUED,
    SubmissionStatus.SCREENING_STAGE_1,
    SubmissionStatus.SCREENING_STAGE_2,
    SubmissionStatus.SCREENING_STAGE_3,
)
# Don't resurrect ancient strandings (their round is long gone; rebuilding the
# image would be wasted work) — bound the boot-time re-kick to recent ones.
_RESUME_MAX_AGE_SECONDS = 24 * 3600.0


async def resume_stranded_screenings() -> int:
    """Re-kick screening for submissions stranded mid-pipeline by a restart.

    The pipeline runs as a background task spawned once at submission time; a
    process restart kills it and nothing re-starts it, leaving the submission
    parked in QUEUED/SCREENING_* forever (observed live 2026-07-02: an update
    restart stranded ALL 5 of a round's submissions in screening — the round
    then busy-spun in ``replaying`` with benchmarked=0 and miners saw
    "scoring…" indefinitely). Screening is idempotent-from-scratch (fresh
    clone, stages re-run, statuses re-walk), so a boot-time re-kick is safe.

    Private submissions lose their in-memory repo PAT on restart (by design —
    the token is never persisted); those are rejected with an actionable
    reason instead of the misleading generic clone failure they'd hit anyway.

    Returns the number of pipelines re-spawned. Call once at api startup.
    """
    import time as _time

    store = get_store()
    stranded = [
        sub for status in _RESUMABLE_STATUSES
        for sub in store.list_by_status(status)
    ]
    resumed = 0
    for sub in stranded:
        age = _time.time() - (sub.updated_at or 0)
        if age > _RESUME_MAX_AGE_SECONDS:
            logger.warning(
                "[screening] leaving stranded submission %s alone (status=%s, "
                "stale for %.0fh — its round is long gone)",
                sub.submission_id, sub.status.value, age / 3600,
            )
            continue
        if sub.is_private and store.get_repo_token(sub.submission_id) is None:
            await offload_write(store.reject,
                sub.submission_id,
                "screening was interrupted by a validator restart and the "
                "private-repo token is not retained across restarts — please "
                "re-submit",
            )
            logger.warning(
                "[screening] rejected private submission %s stranded by a "
                "restart (in-memory repo token lost)", sub.submission_id,
            )
            continue
        # Rebuild the build-budget gate's charged set from the PRISTINE
        # boot-time statuses BEFORE this pipeline re-runs (its stage-1 re-walk
        # resets the status back to SCREENING_STAGE_1, erasing the evidence a
        # build already started) — so a restart re-dispatch passes the gate
        # without consuming a second budget unit. See _ensure_budget_round.
        _ensure_budget_round(store, getattr(sub, "round_id", "") or "")
        asyncio.get_running_loop().create_task(
            _run_screening_pipeline(sub.submission_id)
        )
        resumed += 1
        logger.info(
            "[screening] resuming submission %s stranded in %s by a restart",
            sub.submission_id, sub.status.value,
        )
    if stranded:
        logger.info(
            "[screening] boot resume: %d stranded, %d re-spawned", len(stranded), resumed,
        )
    return resumed


# Statuses proving a stage-2 BUILD already started for this submission (in
# this or a previous process life). Used to rebuild the build-budget gate's
# charged set after a restart and to re-dispatch resumed pipelines without
# consuming a second unit: resume_stranded_screenings re-runs the pipeline
# from scratch, so without this a restart would re-charge (or re-flood) the
# round's budget for work that already happened.
_BUILD_ATTEMPT_STATUSES = (
    SubmissionStatus.SCREENING_STAGE_2,
    SubmissionStatus.SCREENING_STAGE_3,
    SubmissionStatus.BENCHMARKING,
    SubmissionStatus.SCORED,
    SubmissionStatus.ADOPTED,
)


def _has_prior_build_attempt(sub: Any) -> bool:
    """Did a stage-2 build already start for this submission?

    True when the CURRENT status is at/past the build, or a stage-2 screening
    result was recorded (covers terminal states that already paid for a build:
    build_failed rejects, window-elapsed waitlists of built submissions, …).
    """
    if sub is None:
        return False
    if getattr(sub, "status", None) in _BUILD_ATTEMPT_STATUSES:
        return True
    stage2 = (getattr(sub, "screening", None) or {}).get("stage_2") or {}
    return stage2.get("passed") is not None


def _round_open_window_seconds() -> float:
    """The round OPEN window length — same env + default as the round
    coordinator's close check (api/startup.py). Read here only to time the
    build budget's newcomer→proven spill delay."""
    try:
        return float(os.environ.get("SOLVER_ROUND_OPEN_SECONDS", "300").strip() or "300")
    except ValueError:
        return 300.0


def _ensure_budget_round(store: Any, round_id: str) -> None:
    """Bootstrap the build-budget gate's state for a round (idempotent).

    Scans the round's submissions for prior build attempts so the gate's
    charged set survives a restart (each counted exactly once — the gate never
    double-charges or re-floods a round whose builds already ran). MUST run
    before the resumed pipelines re-walk their statuses: the pipeline resets a
    stranded submission back to SCREENING_STAGE_1 at its stage-1 re-run, which
    would erase the "build already started" evidence this scan reads — so
    resume_stranded_screenings calls this FIRST, on the pristine boot-time
    statuses. Best-effort (test doubles may lack the store surface).
    """
    from minotaur_subnet.harness.build_budget import get_build_budget_gate

    from .state import get_round_store

    gate = get_build_budget_gate()
    if not round_id or not gate.needs_round(round_id):
        return
    try:
        opened_at = 0.0
        try:
            round_state = get_round_store().get_round(round_id)
            opened_at = float(getattr(round_state, "created_at", 0.0) or 0.0)
        except Exception:
            logger.warning(
                "[build-budget] no round state for %s (newcomer-spill delay "
                "disabled for the round)", round_id, exc_info=True,
            )
        prior = [
            (s.submission_id, s.hotkey or "")
            for s in store.list_by_round(round_id)
            if _has_prior_build_attempt(s)
        ]
        gate.ensure_round(
            round_id,
            opened_at=opened_at,
            open_seconds=_round_open_window_seconds(),
            prior_attempts=prior,
        )
    except Exception:
        logger.warning(
            "[build-budget] bootstrap for %s failed (gate will bootstrap "
            "lazily without restart history)", round_id, exc_info=True,
        )


async def _acquire_build_grant(store: Any, sub: Any):
    """Wire the pipeline into the per-round build-budget gate (may WAIT).

    Gathers the leader-local context the gate needs — the round's open window
    (for the newcomer-spill delay), a liveness probe (so a waiter never
    outlives a round closed without a rotation flush), and the restart-rebuild
    input (prior build attempts, charged exactly once) — then asks for a
    unit. See harness/build_budget.py for the allocation rules and the
    2026-07-16 build-flood rationale.
    """
    from minotaur_subnet.harness.build_budget import get_build_budget_gate

    from .state import get_round_store

    gate = get_build_budget_gate()
    round_id = sub.round_id or ""

    def _round_is_open() -> bool:
        try:
            current = get_round_store().get_round(round_id)
        except Exception:
            return False
        if current is None:
            return False
        status = getattr(current.status, "value", current.status)
        return str(status) == "open"

    _ensure_budget_round(store, round_id)

    fresh = store.get(sub.submission_id) or sub
    return await gate.acquire(
        submission_id=sub.submission_id,
        hotkey=sub.hotkey or "",
        round_id=round_id,
        prior_attempt=_has_prior_build_attempt(fresh),
        round_is_open=_round_is_open,
    )


def _terminal_during_screening(store: Any, submission_id: str) -> str | None:
    """Return the terminal reason if this submission reached a terminal state
    while its screening ran, else None.

    Close-time rotation (apply_rotation_slate) parks the round's overflow to
    hold the benched slate at SOLVER_ROUND_MAX_SUBMISSIONS. If it fires while a
    skipped submission is still screening — or a restart resumes an already
    -skipped one — the pipeline must NOT re-queue it for benchmark (that
    overwrites the terminal state and busts the slate cap). Returns the reason
    (possibly empty str) so the caller can log it; None means still eligible.

    Checks the SHARED rotation terminal rule, not just REJECTED: rotation has
    parked overflow as WAITLISTED (no-fault) since #620, and this guard's old
    REJECTED-only check let a late-finishing screening overwrite that terminal
    waitlist back to BENCHMARKING — the live slate-cap leak this fixes. The
    build-budget flush parks its waiters as WAITLISTED too, so the same rule
    covers both.
    """
    from minotaur_subnet.harness.rotation import is_terminal_status

    current = store.get(submission_id)
    if current is not None and is_terminal_status(current):
        return current.rejection_reason or current.status.value
    return None


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

    # Private submissions clone the miner's private repo with the per-submission
    # PAT (in-memory only). None for the public path.
    repo_token = store.get_repo_token(submission_id) if sub.is_private else None

    repo_dir = None
    try:
        # Clone the repo
        repo_dir = tempfile.mkdtemp(prefix=f"solver-{sub.commit_hash[:8]}-")
        clone_ok = await _clone_repo(
            sub.repo_url, sub.commit_hash, repo_dir, token=repo_token,
        )
        if not clone_ok:
            await offload_write(store.reject,
                submission_id, "Failed to clone repository",
                outcome_code="clone_failed",
            )
            return

        # Stage 1: Static checks
        await offload_write(store.update_status,submission_id, SubmissionStatus.SCREENING_STAGE_1)

        from minotaur_subnet.harness.screening import run_stage_1
        # Off the event loop: the AST walk (factorization/deadwood metrics)
        # is ~1s of pure CPU per repo — run on-loop it stalls every in-flight
        # request for the duration, and submission bursts run several
        # back-to-back.
        s1 = await asyncio.to_thread(run_stage_1, repo_dir)
        # Factorization metric: persist BEFORE the pass-check so a rejected
        # submission (incl. an armed-floor `too_entangled`) still records the
        # value it was rejected at — miners see the number they must get under.
        # Runs for BOTH the public and private clone paths (single confluence).
        if s1.max_region_nodes is not None:
            await offload_write(store.set_max_region_nodes,submission_id, s1.max_region_nodes)
        await offload_write(store.set_screening_result,
            submission_id, stage=1,
            passed=s1.passed,
            duration_ms=s1.duration_ms,
            details=s1.details,
            error_code=s1.error_code,
        )
        # Phase-0 deadwood metric (observe-only, not gated). Persisted BEFORE
        # the pass-check so any stage-1 result that carried a measurement is
        # recorded even when the submission is rejected. unproductive_nodes may
        # be None (unparseable non-exempt file) — persisted as None on purpose.
        if s1.unproductive_metric_version is not None:
            await offload_write(store.set_deadwood_metric,
                submission_id,
                s1.unproductive_nodes,
                s1.unproductive_metric_version,
                s1.unproductive_top_offenders,
            )
        # Normalized content fingerprint — persisted BEFORE the pass-check
        # (persist-on-reject, like the metrics above) so rejected submissions
        # still record the identity they were rejected under.
        if s1.content_fingerprint:
            await offload_write(store.set_content_fingerprint,submission_id, s1.content_fingerprint)

        if not s1.passed:
            return  # set_screening_result already rejected

        # Fingerprint checks, ONE store scan for both: (a) the cross-ACTOR
        # copy reject — the earliest submitter owns the fingerprint, copies by
        # other actors reject pre-build, and in-flight copies that raced past
        # their own check get swept retroactively; (b) the benched-rounds
        # quota. Both leader-local admission control.
        fp_cap = _max_rounds_per_fingerprint()
        reject_copies = _reject_cross_actor_copies()
        benched = 0
        if s1.content_fingerprint and (fp_cap > 0 or reject_copies):
            submitters, benched = store.fingerprint_usage(
                s1.content_fingerprint, exclude_submission_id=submission_id,
            )
            resolver = snapshot_resolver() if reject_copies else None
            if reject_copies and resolver is None:
                logger.debug(
                    "cross-actor copy reject standing down for %s: no coldkey "
                    "attribution available (kill-switch or map not loaded)",
                    submission_id,
                )
            if resolver is not None:
                owner_prior, sweep_ids = evaluate_fingerprint_ownership(
                    submitters,
                    submission_id=submission_id,
                    hotkey=sub.hotkey or "",
                    created_at=float(sub.created_at or 0.0),
                    resolver=resolver,
                )
                if owner_prior is not None:
                    await offload_write(store.reject,
                        submission_id,
                        (
                            f"identical code (normalized fingerprint "
                            f"{s1.content_fingerprint[:12]}…) was first submitted "
                            f"by another miner — a copy adds nothing to the "
                            f"corpus and is not eligible for benchmarking. "
                            f"Comment, whitespace, docstring and rename edits do "
                            f"not make code yours; submit your own solver logic "
                            f"to participate."
                        ),
                        outcome_code=OUTCOME_COPYCAT_CODE,
                    )
                    logger.info(
                        "Submission %s rejected as cross-actor copy: fp %s first "
                        "submitted by %s (hotkey %s, map=%s)",
                        submission_id, s1.content_fingerprint[:12],
                        owner_prior[2], (owner_prior[0] or "")[:12],
                        resolver.source,
                    )
                    return
                for late_sid in sweep_ids:
                    try:
                        await offload_write(store.reject,
                            late_sid,
                            (
                                f"identical code (normalized fingerprint "
                                f"{s1.content_fingerprint[:12]}…) was first "
                                f"submitted by another miner — a copy adds "
                                f"nothing to the corpus and is not eligible "
                                f"for benchmarking."
                            ),
                            outcome_code=OUTCOME_COPYCAT_CODE,
                        )
                        logger.info(
                            "Swept in-flight cross-actor copy %s of fp %s "
                            "(owner submission %s)",
                            late_sid, s1.content_fingerprint[:12], submission_id,
                        )
                    except Exception:
                        logger.warning(
                            "Sweeping cross-actor copy %s failed (ignored)",
                            late_sid, exc_info=True,
                        )

        # Cross-hotkey resubmit quota on the NORMALIZED identity. The
        # per-(hotkey, commit) cap keys on the git SHA — refreshed for free by
        # a nonce comment — and gives each sybil hotkey its own allowance for
        # the same bytes. This cap keys on what the code MEANS and counts
        # benched rounds ACROSS hotkeys, so identical trees share ONE quota
        # bucket however they're wrapped or distributed. Enforced pre-build:
        # a capped resubmit never costs a Docker build or a bench slot.
        # Operator-local admission control (leader gateway), like the other
        # submission caps — not fleet-consensus.
        if fp_cap > 0 and s1.content_fingerprint:
            if benched >= fp_cap:
                await offload_write(store.reject,
                    submission_id,
                    (
                        f"identical code (normalized fingerprint "
                        f"{s1.content_fingerprint[:12]}…) was already benchmarked "
                        f"in {benched} round(s) — the cap is {fp_cap}, counted "
                        f"across ALL hotkeys. Comment, whitespace, docstring and "
                        f"nonce edits do not make code new; change the logic or "
                        f"data to participate again."
                    ),
                    outcome_code="fingerprint_repeat",
                )
                return

        # Terminal check BEFORE asking for a build unit: close-time rotation can
        # have parked this submission (waitlist/reject) while stage 1 was still
        # running — building it would waste a budget unit on a submission that
        # can no longer be benched this round.
        pre_gate_terminal = _terminal_during_screening(store, submission_id)
        if pre_gate_terminal is not None:
            logger.info(
                "Submission %s reached a terminal state during stage 1 (%s) — "
                "not requesting a build", submission_id, pre_gate_terminal,
            )
            return

        # Stage 2 gate: the docker build is the resource the 2026-07-16 flood
        # weaponized (63 builds/hour from sybil intake), so builds are dispensed
        # from a per-round budget (SOLVER_ROUND_INTAKE_MAX, default 8, 0 =
        # unlimited) by ROTATION SENIORITY — proven miners LRU-first with a
        # reserved newcomer lottery share — instead of arrival order. This
        # acquire may WAIT (until a unit frees, or the close-time flush parks
        # us); budget-winners proceed immediately, so their near-immediate
        # feedback is preserved. See harness/build_budget.py.
        grant = await _acquire_build_grant(store, sub)
        if not grant.granted:
            # No-fault denial: never a REJECT for flow control. The close-time
            # flush usually parked us already (grant.parked); otherwise park
            # here — unless a terminal state landed meanwhile (rotation).
            if not grant.parked and _terminal_during_screening(store, submission_id) is None:
                await offload_write(
                    store.waitlist,
                    submission_id,
                    grant.reason or (
                        "this round's build budget was spent before your "
                        "seniority reached the front of the queue — waitlisted, "
                        "seniority retained; resubmit next round"
                    ),
                    outcome_code=OUTCOME_BUILD_BUDGET,
                )
            logger.info(
                "Submission %s denied a build unit (%s) — waitlisted no-fault",
                submission_id, grant.reason,
            )
            return

        # Stage 2: Build check. The gate slot is released as soon as the build
        # finishes (pass OR fail) so the next-priority waiter dispatches
        # immediately — NOT held through stage 3 / the GHCR push (up to ~10 min),
        # which would stall the whole dispatch queue behind one slow push.
        from minotaur_subnet.harness.build_budget import get_build_budget_gate

        image_tag = f"solver-{sub.commit_hash[:12]}:screening"
        from minotaur_subnet.harness.screening import run_stage_2
        try:
            await offload_write(store.update_status,submission_id, SubmissionStatus.SCREENING_STAGE_2)
            s2 = await run_stage_2(repo_dir, image_tag)
        finally:
            get_build_budget_gate().release(sub.round_id or "", submission_id)
        await offload_write(store.set_screening_result,
            submission_id, stage=2,
            passed=s2.passed,
            duration_ms=s2.duration_ms,
            details=s2.details,
            error_code=s2.error_code,
        )
        if not s2.passed:
            return

        await offload_write(store.set_image_tag,submission_id, image_tag)
        image_id = await _resolve_image_id(image_tag)
        if not image_id:
            await offload_write(store.reject,
                submission_id,
                (
                    "Stage 2 policy: failed to resolve immutable image ID "
                    f"for built image {image_tag}"
                ),
            )
            return
        await offload_write(store.set_image_id,submission_id, image_id)

        # Content-addressed transport (leader-only, inert until CANDIDATE_IMAGE_REPO
        # is set): push the built image to the candidate repo and persist its GHCR
        # manifest digest so followers pull byte-identical bytes by digest. The
        # local build above is what the leader benchmarks; this just distributes it.
        from minotaur_subnet.harness.image_transport import leader_pushes_digests
        if leader_pushes_digests() and sub.pr_number:
            digest_ref = await _push_candidate_image(
                image_tag, sub.pr_number, submission_id,
            )
            if digest_ref:
                await offload_write(store.set_image_digest,submission_id, digest_ref)
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
            await offload_write(store.reject,
                submission_id,
                (
                    "Stage 2 policy: REQUIRE_ASYMMETRIC_PROVENANCE=1 but "
                    "SUBMISSION_PROVENANCE_SIGNING_PRIVATE_KEY is unset"
                ),
            )
            return
        if require_signed_provenance and not signing_private_key and not signing_key:
            await offload_write(store.reject,
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
                await offload_write(store.reject,
                    submission_id,
                    f"Stage 2 policy: failed to sign provenance ({exc})",
                )
                return
            await offload_write(store.set_provenance,submission_id, provenance)

        # Extract solver info from stage 2 details
        if ":" in s2.details:
            try:
                info_part = s2.details.split(": ", 1)[1]
                name_ver = info_part.split(" v")
                await offload_write(store.set_solver_info,
                    submission_id,
                    name=name_ver[0],
                    version=name_ver[1] if len(name_ver) > 1 else None,
                )
            except (IndexError, ValueError):
                pass

        # Stage 3: Smoke test
        await offload_write(store.update_status,submission_id, SubmissionStatus.SCREENING_STAGE_3)

        from minotaur_subnet.harness.screening import run_stage_3
        s3 = await run_stage_3(image_tag)
        await offload_write(store.set_screening_result,
            submission_id, stage=3,
            passed=s3.passed,
            duration_ms=s3.duration_ms,
            details=s3.details,
            error_code=s3.error_code,
        )
        if not s3.passed:
            return

        # All screening passed -- move to benchmarking queue. But re-read the
        # status first: close-time rotation (apply_rotation_slate) can PARK
        # this submission while its screening was still in flight (and a restart
        # can resume an already-skipped one). Without this guard the async
        # pipeline overwrites that terminal state back to BENCHMARKING, so the
        # round benches MORE than its SOLVER_ROUND_MAX_SUBMISSIONS slate — the
        # leak that showed 9-11 "scored" in a 3-slot round on restart-heavy
        # days. Must use the SHARED terminal rule: rotation parks overflow as
        # WAITLISTED (not REJECTED) since #620, and the old REJECTED-only check
        # let a late-finishing screening resurrect a terminal waitlist.
        terminal = _terminal_during_screening(store, submission_id)
        if terminal is not None:
            logger.info(
                "Submission %s passed screening but is already terminal "
                "(%s) — not queuing for benchmark (rotation slate full)",
                submission_id, terminal or "terminal",
            )
            return
        # All screening passed -- move to benchmarking queue
        await offload_write(store.update_status,submission_id, SubmissionStatus.BENCHMARKING)
        logger.info(
            "Submission %s passed screening, queued for benchmarking",
            submission_id,
        )

    except Exception as exc:
        logger.exception("Screening pipeline error for %s", submission_id)
        await offload_write(store.reject,submission_id, f"Screening error: {exc}", outcome_code="screening_error")

    finally:
        if repo_dir and os.path.exists(repo_dir):
            shutil.rmtree(repo_dir, ignore_errors=True)
        # PR-fold feedback: if screening REJECTED a PR-based submission, mirror it
        # onto the miner's PR (comment the reason + close + GC the candidate image).
        # Pure feedback — usable while adoption is frozen, no chain writes. Best-
        # effort + leader-only (no-op without SOLVER_REPO_URL).
        try:
            final = store.get(submission_id)
            if (
                final is not None
                and final.status == SubmissionStatus.REJECTED
                and getattr(final, "pr_number", None)
            ):
                from minotaur_subnet.relayer.solver_repo import on_champion_rejected_pr
                reason = final.rejection_reason or "Screening rejected"
                # repo_token (captured above) survives the store's purge-on-reject
                # so a private rejection can still comment on the private PR.
                on_champion_rejected_pr(
                    final, f"### ❌ Screening rejected\n\n{reason}",
                    repo_token=repo_token,
                )
        except Exception as exc:  # never let feedback break screening
            logger.warning("Screening reject-feedback failed for %s: %s", submission_id, exc)
