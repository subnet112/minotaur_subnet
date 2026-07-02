"""Unit tests for P4b: follower pull-by-digest in the reactive benchmark."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from minotaur_subnet.harness import image_transport as it
from minotaur_subnet.api.routes.submissions import champion_consensus as cc

REPO = "ghcr.io/subnet112/minotaur-solver-candidates"
HEX = "a" * 64


def _run(coro):
    return asyncio.run(coro)


# ── is_bare_digest: the mode discriminator ──────────────────────────────────

def test_is_bare_digest_only_true_for_bare_64hex():
    assert it.is_bare_digest(HEX) is True
    assert it.is_bare_digest(HEX.upper()) is True            # normalized
    assert it.is_bare_digest(f"sha256:{HEX}") is False       # legacy {{.Id}} -> legacy mode
    assert it.is_bare_digest(f"{REPO}@sha256:{HEX}") is False
    assert it.is_bare_digest("builtin:abc") is False
    assert it.is_bare_digest("a" * 63) is False
    assert it.is_bare_digest(None) is False


# ── the follower's image-resolution branch ──────────────────────────────────
# We stop _reactive_benchmark_candidate right after image resolution by making
# the benchmark machinery (BenchmarkWorker) raise, then assert WHICH docker path
# ran: pull-by-digest (digest mode) vs the local {{.Id}} compare (legacy).

def _candidate(image_tag="solver-x:screening", image_id=f"sha256:{HEX}"):
    return SimpleNamespace(
        submission_id="sub_1", image_tag=image_tag, image_id=image_id,
        commit_hash="c", repo_url="https://github.com/m/s.git",
    )


def test_digest_mode_pulls_by_digest_then_proceeds(monkeypatch):
    # In digest mode the follower pulls <repo>@sha256:D and SKIPS the {{.Id}}
    # compare. We let the pull succeed and the benchmark machinery fail right
    # after (no real Anvil/intents in a unit env) — what matters is that the pull
    # ran with the reconstructed digest ref and the local-id compare did NOT.
    monkeypatch.setenv("CANDIDATE_IMAGE_REPO", REPO)
    pull = AsyncMock(return_value=True)
    local_id = AsyncMock(return_value=f"sha256:{HEX}")
    with patch.object(cc, "_pull_image_by_digest", pull), \
         patch.object(cc, "_resolve_local_image_id", local_id):
        # Returns (False, {}) when the benchmark can't run in a unit env; the
        # resolution branch (the part under test) already executed before that.
        _run(cc._reactive_benchmark_candidate(
            _candidate(), candidate_image_id=HEX,
        ))
    pull.assert_awaited_once()
    assert pull.await_args.args[0] == f"{REPO}@sha256:{HEX}"   # pulled the digest ref
    local_id.assert_not_awaited()                              # {{.Id}} compare skipped


def test_digest_mode_refuses_on_pull_failure(monkeypatch):
    monkeypatch.setenv("CANDIDATE_IMAGE_REPO", REPO)
    with patch.object(cc, "_pull_image_by_digest", AsyncMock(return_value=False)):
        verified, counts = _run(cc._reactive_benchmark_candidate(
            _candidate(), candidate_image_id=HEX,
        ))
    assert verified is False and counts == {}                  # refuse to sign, no benchmark


def test_legacy_mode_keeps_local_id_compare():
    # No digest (candidate_image_id is the legacy sha256:<id>) -> local {{.Id}} path.
    pull = AsyncMock(return_value=True)
    # Mismatched local id -> refuse, proving the legacy compare ran (not a pull).
    local_id = AsyncMock(return_value=f"sha256:{'b' * 64}")
    with patch.object(cc, "_pull_image_by_digest", pull), \
         patch.object(cc, "_resolve_local_image_id", local_id):
        verified, counts = _run(cc._reactive_benchmark_candidate(
            _candidate(), candidate_image_id=f"sha256:{HEX}",
        ))
    assert verified is False and counts == {}
    local_id.assert_awaited_once()       # legacy {{.Id}} compare ran
    pull.assert_not_awaited()            # no pull in legacy mode
