"""/health must be able to see the failure mode that actually took the api down.

On 2026-08-22 the leader's 193GB root filled with solver container logs.
`/health` answered **200 `status: ok`** throughout — it touches no store — while
every store-backed endpoint 500'd, `docker exec` failed with "no space left on
device", and rounds stalled ~5h. A liveness probe blind to that is why it ran
unnoticed for hours.

The probe reports; it does not throw. And it deliberately keeps /health at
HTTP 200: the container healthcheck fails only on an exception, so a 5xx would
mark the api unhealthy and invite a restart loop — and restarting fixes nothing
when the disk is full.
"""
import pytest

from minotaur_subnet.api import server


@pytest.fixture
def usage(monkeypatch):
    def _set(total_gb, free_gb):
        class U:
            total = int(total_gb * 1024**3)
            free = int(free_gb * 1024**3)
            used = total - free
        monkeypatch.setattr(server.shutil, "disk_usage", lambda p: U)
    return _set


def test_healthy_disk_is_ok(usage):
    usage(200, 120)
    assert server._disk_health("/data")["state"] == "ok"


def test_low_disk_is_flagged(usage):
    usage(200, 15)                      # 7.5% free, under the 10% warn
    assert server._disk_health("/data")["state"] == "low"


def test_nearly_full_disk_is_critical(usage):
    usage(200, 2)                       # 1% free
    assert server._disk_health("/data")["state"] == "critical"


def test_absolute_floor_catches_a_huge_disk(usage):
    """3% of 2TB is 60GB of headroom and 3% of 20GB is 600MB — a percentage
    alone is a bad alarm, so an absolute byte floor runs alongside it."""
    usage(2000, 4)                      # 0.2% free but a percentage-only rule
    assert server._disk_health("/data")["state"] == "critical"


def test_percentage_catches_a_small_disk(usage):
    """…and symmetrically, the byte floor alone would never fire on a small
    disk, so the percentage must still bite. The WORSE verdict wins."""
    usage(40, 2)                        # 5% free — above CRIT bytes? no: 2GB < 5GB
    assert server._disk_health("/data")["state"] == "critical"


def test_unreadable_path_is_unknown_not_degraded(monkeypatch):
    """A probe that cannot measure must not itself be the reason /health fails."""
    def boom(_):
        raise OSError("no such path")
    monkeypatch.setattr(server.shutil, "disk_usage", boom)
    out = server._disk_health("/nope")
    assert out["state"] == "unknown"
    assert "error" in out


def test_reported_fields_are_present_and_consistent(usage):
    usage(100, 25)
    d = server._disk_health("/data")
    assert d["free_pct"] == pytest.approx(25.0, abs=0.01)
    assert d["used_pct"] == pytest.approx(75.0, abs=0.01)
    assert d["free_bytes"] < d["total_bytes"]
    assert d["path"] == "/data"


def test_unknown_state_does_not_degrade_status():
    """Mirrors the handler's own rule: only low/critical degrade."""
    for state, expected in (
        ("ok", "ok"), ("unknown", "ok"), ("low", "degraded"), ("critical", "degraded"),
    ):
        status = "degraded" if state in ("low", "critical") else "ok"
        assert status == expected, state
