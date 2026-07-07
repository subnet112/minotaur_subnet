"""Stage-2 screening builds must not stack unbounded.

Live incident 2026-07-07: submission bursts ran one screening pipeline per
submission with no cap; concurrent docker builds (2 CPUs each) saturated the
leader's 4-core host and the api process went silent for 40-60s at a time
(nginx upstream-timeout storms, dead CORS preflights). run_stage_2 is now
bounded by a global semaphore (SCREENING_BUILD_CONCURRENCY, default 1).
"""
from __future__ import annotations

import asyncio

from unittest.mock import patch

import minotaur_subnet.harness.screening as screening


def _reset_semaphore():
    screening._stage2_semaphore = None


def test_default_concurrency_is_one(monkeypatch):
    monkeypatch.delenv("SCREENING_BUILD_CONCURRENCY", raising=False)
    _reset_semaphore()

    async def _check():
        sem = screening._get_stage2_semaphore()
        assert sem._value == 1

    asyncio.run(_check())
    _reset_semaphore()


def test_env_overrides_concurrency(monkeypatch):
    monkeypatch.setenv("SCREENING_BUILD_CONCURRENCY", "3")
    _reset_semaphore()

    async def _check():
        assert screening._get_stage2_semaphore()._value == 3

    asyncio.run(_check())
    _reset_semaphore()


def test_garbage_env_falls_back_to_one(monkeypatch):
    monkeypatch.setenv("SCREENING_BUILD_CONCURRENCY", "0")
    _reset_semaphore()

    async def _check():
        assert screening._get_stage2_semaphore()._value == 1

    asyncio.run(_check())
    _reset_semaphore()


def test_concurrent_stage2_runs_serialize(monkeypatch):
    monkeypatch.delenv("SCREENING_BUILD_CONCURRENCY", raising=False)
    _reset_semaphore()

    in_flight = 0
    max_in_flight = 0

    async def _fake_locked(repo_path, image_tag, build_timeout, init_timeout):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return screening.StageResult(
            stage=2, passed=True, duration_ms=1, details="ok",
        )

    async def _run():
        with patch.object(screening, "_run_stage_2_locked", _fake_locked):
            results = await asyncio.gather(*[
                screening.run_stage_2(f"/tmp/repo{i}", f"img{i}:screening")
                for i in range(4)
            ])
        return results

    results = asyncio.run(_run())
    assert all(r.passed for r in results)
    assert max_in_flight == 1  # never more than one build at a time
    _reset_semaphore()
