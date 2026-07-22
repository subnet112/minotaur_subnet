"""Unit tests for the api-launched block-pin proxy manager (read_proxy_manager)."""
import asyncio
import os

from minotaur_subnet.api import read_proxy_manager as rpm


class FakeDocker:
    """Async stand-in for rpm._docker: records calls, returns scripted (rc, out, err)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, *args, timeout=30.0):
        self.calls.append(args)
        return self.responses.pop(0) if self.responses else (0, "", "")


def _clear_env(monkeypatch):
    for k in (
        "DISABLE_READ_PROXY", "SOLVER_READ_PROXY", "SOLVER_READ_PROXY_CONTROL",
        "SOLVER_READ_PROXY_TOKEN", "BENCHMARK_DOCKER_NETWORK",
    ):
        monkeypatch.delenv(k, raising=False)


def test_disabled_skips_docker(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DISABLE_READ_PROXY", "1")
    fake = FakeDocker([])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False
    assert fake.calls == []  # never touched docker
    assert os.environ.get("SOLVER_READ_PROXY") is None


def test_proxy_running_current_image_left_alone(monkeypatch):
    # inspect works: proxy running on the api's CURRENT image -> leave it, env wired.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    scheduled = []
    monkeypatch.setattr(rpm, "_schedule_ensure_retry", lambda: scheduled.append(1))
    fake = FakeDocker([
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),   # _resolve_self -> api image
        (0, 'true|sha256:img|["PATH=/usr/bin"]', ""),        # _proxy_state -> running, SAME image
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert not any(c and c[0] == "run" for c in fake.calls)        # no relaunch
    assert not scheduled                                           # fully compared -> no retry
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL
    assert os.environ["SOLVER_READ_PROXY_TOKEN"] == "tok"


def test_proxy_rpc_env_drift_recreated(monkeypatch):
    # inspect works: proxy on the CURRENT image but its RPC_PROXY_* env differs from
    # the api's (operator flipped the cache kill switch) -> recreate to apply it.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    monkeypatch.setenv("RPC_PROXY_RESPONSE_CACHE", "0")  # api wants caches OFF
    fake = FakeDocker([
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),   # api image
        (0, 'true|sha256:img|["PATH=/usr/bin"]', ""),        # proxy running, NO cache var
        (0, "", ""),                                         # rm -f
        (0, "[{}]", ""),                                     # network inspect -> exists
        (0, "cid", ""),                                      # run
        (0, "", ""),                                         # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    run_call = next(c for c in fake.calls if c and c[0] == "run")
    assert "RPC_PROXY_RESPONSE_CACHE=0" in run_call  # switch forwarded into the container


def test_proxy_stale_image_recreated(monkeypatch):
    # inspect works: proxy running on a STALE image -> recreate so it tracks the api.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "sha256:NEW|minotaur benchmark-sandbox ", ""),  # api image NEW
        (0, "true|sha256:OLD", ""),                         # proxy running OLD image
        (0, "", ""),                                        # rm -f
        (0, "[{}]", ""),                                     # network inspect -> net EXISTS (no create)
        (0, "cid", ""),                                     # run
        (0, "", ""),                                        # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert any(c[:2] == ("rm", "-f") for c in fake.calls)   # removed stale
    assert any(c and c[0] == "run" for c in fake.calls)     # recreated on the new image
    assert not any(c[:2] == ("network", "create") for c in fake.calls)  # net present -> no create


def test_env_wired_inspect_403_running_proxy_left(monkeypatch):
    # inspect 403s for BOTH self + proxy -> ps fallbacks (api image via ps; proxy running,
    # image UNcompared) -> leave it. env wired throughout (the #301 robustness path).
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (1, "", "403 Forbidden"),                                          # inspect self -> 403
        (0, "ghcr.io/x:latest|production_minotaur,benchmark-sandbox", ""),  # self ps fallback
        (1, "", "403 Forbidden"),                                          # inspect proxy -> 403
        (0, "minotaur-rpc-pin-proxy", ""),                                 # proxy ps -> running
    ])
    scheduled = []
    monkeypatch.setattr(rpm, "_schedule_ensure_retry", lambda: scheduled.append(1))
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert not any(c and c[0] == "run" for c in fake.calls)       # left alone (uncompared)
    assert scheduled                                              # degraded -> retry scheduled
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL


def test_env_wired_even_when_all_docker_fails(monkeypatch):
    # ROOT-CAUSE FIX: every docker call 403s + proxy not running -> can't launch (False),
    # BUT the env is STILL wired -> the api never silently reads the raw anvil.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (1, "", "403 Forbidden"),   # inspect self -> 403
        (1, "", "403 Forbidden"),   # self ps fallback -> 403  (api image = "")
        (1, "", "403 Forbidden"),   # inspect proxy -> 403
        (1, "", "403 Forbidden"),   # proxy ps -> 403  (running=False)
    ])
    scheduled = []
    monkeypatch.setattr(rpm, "_schedule_ensure_retry", lambda: scheduled.append(1))
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL  # env wired (THE FIX)
    assert scheduled                                              # degraded -> retry scheduled


def test_degraded_boot_background_retry_recovers(monkeypatch):
    # THE 2026-07-02 INCIDENT: a watchtower rolling update makes every docker call
    # fail at api boot -> ensure is degraded (no proxy launched). The background
    # retry must re-run ensure once docker heals and launch the proxy — previously
    # the one-shot ensure stranded the proxy unmanaged for the api's lifetime.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    monkeypatch.setattr(rpm, "_ENSURE_RETRY_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(rpm, "_ensure_retry_task", None)
    fake = FakeDocker([
        # boot pass: socket-proxy churn — everything fails
        (1, "", "403 Forbidden"),   # inspect self
        (1, "", "403 Forbidden"),   # self ps fallback
        (1, "", "403 Forbidden"),   # inspect proxy
        (1, "", "403 Forbidden"),   # proxy ps
        # retry pass: docker healed — resolve, proxy absent, launch
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),  # self inspect OK
        (1, "", "No such object"),                          # proxy inspect -> absent
        (0, "", ""),                                        # proxy ps -> not running
        (0, "", ""),                                        # rm -f
        (0, "[{}]", ""),                                    # network inspect -> exists
        (0, "cid", ""),                                     # run
        (0, "", ""),                                        # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)

    async def main():
        assert await rpm.ensure_read_proxy_container() is False  # boot: degraded
        assert rpm._ensure_retry_task is not None
        await asyncio.wait_for(rpm._ensure_retry_task, timeout=5)

    asyncio.run(main())
    assert any(c and c[0] == "run" for c in fake.calls)  # retry launched the proxy


def test_launch_when_absent(monkeypatch):
    # proxy not running, inspect works -> launch from the api image.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok123")
    monkeypatch.setenv("BASE_RPC_URL", "https://base.example")
    fake = FakeDocker([
        (0, "sha256:apiimg|minotaur benchmark-sandbox ", ""),  # api image
        (1, "", "No such object"),                             # proxy inspect -> absent
        (0, "", ""),                                           # proxy ps -> empty (not running)
        (0, "", ""),                                           # rm -f
        (0, "[{}]", ""),                                       # network inspect -> exists
        (0, "cid", ""),                                        # run
        (0, "", ""),                                           # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    run = next(c for c in fake.calls if c and c[0] == "run")
    assert rpm.PROXY_CONTAINER_NAME in run and rpm.PROXY_STATIC_IP in run
    assert "sha256:apiimg" in run and rpm._PROXY_MODULE in run
    blob = " ".join(x for c in fake.calls for x in c)
    assert "CONTROL_TOKEN=tok123" in blob and "base=https://base.example" in blob
    assert any(c[:2] == ("network", "connect") and "minotaur" in c for c in fake.calls)
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL


def test_launch_via_ps_fallback_when_inspect_403(monkeypatch):
    # self-inspect 403s -> ps fallback resolves the api image + net; proxy not running -> launch.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (1, "", "403 Forbidden"),                                          # inspect self -> 403
        (0, "ghcr.io/x/img:latest|production_minotaur,benchmark-sandbox", ""),  # self ps fallback
        (1, "", "No such object"),                                         # proxy inspect -> absent
        (0, "", ""),                                                       # proxy ps -> not running
        (0, "", ""),                                                       # rm -f
        (0, "[{}]", ""),                                                   # network inspect -> exists
        (0, "cid", ""),                                                    # run
        (0, "", ""),                                                       # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    run = next(c for c in fake.calls if c and c[0] == "run")
    assert "ghcr.io/x/img:latest" in run              # image from the ps fallback
    assert any(c[:2] == ("network", "connect") and "production_minotaur" in c for c in fake.calls)
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL


def test_create_failure_still_wires_env(monkeypatch):
    # launch FAILS -> env STILL wired (prior proxy used / benchmarks fail loud, never anvil).
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),   # api image
        (1, "", "No such object"),                           # proxy inspect -> absent
        (0, "", ""),                                          # proxy ps -> not running
        (0, "", ""),                                          # rm -f
        (0, "[{}]", ""),                                      # network inspect -> exists (no create)
        (1, "", "Address already in use"),                   # run FAILS
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL  # wired despite launch fail


# ── benchmark-sandbox network self-heal (the unused-top-level-net gap) ────────

def test_ensure_benchmark_network_noop_when_present(monkeypatch):
    _clear_env(monkeypatch)
    fake = FakeDocker([(0, "[{}]", "")])  # network inspect -> exists
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm._ensure_benchmark_network("benchmark-sandbox")) is True
    assert not any(c[:2] == ("network", "create") for c in fake.calls)  # never creates


def test_ensure_benchmark_network_creates_when_missing(monkeypatch):
    _clear_env(monkeypatch)
    fake = FakeDocker([
        (1, "", "network benchmark-sandbox not found"),  # inspect -> MISSING
        (0, "netid", ""),                                # create -> ok
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm._ensure_benchmark_network("benchmark-sandbox")) is True
    creates = [c for c in fake.calls if c[:2] == ("network", "create")]
    assert creates, "should have created the missing network"
    args = creates[0]
    assert "--internal" in args and "--subnet" in args
    assert "172.30.0.0/24" in args and args[-1] == "benchmark-sandbox"


def test_ensure_benchmark_network_subnet_env_override(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BENCHMARK_DOCKER_NETWORK_SUBNET", "10.9.0.0/24")
    fake = FakeDocker([(1, "", "missing"), (0, "netid", "")])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm._ensure_benchmark_network("bench")) is True
    creates = [c for c in fake.calls if c[:2] == ("network", "create")]
    assert "10.9.0.0/24" in creates[0]


def test_ensure_benchmark_network_create_race_then_present(monkeypatch):
    # create loses a race (already exists) -> re-inspect finds it -> success, not failure.
    _clear_env(monkeypatch)
    fake = FakeDocker([
        (1, "", "missing"),          # inspect -> missing
        (1, "", "already exists"),   # create -> fails (concurrent create)
        (0, "netid", ""),            # re-inspect -> now present
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm._ensure_benchmark_network("benchmark-sandbox")) is True


def test_ensure_benchmark_network_hard_fail_returns_false(monkeypatch):
    # create fails AND still missing -> False (logged, best-effort), never raises.
    _clear_env(monkeypatch)
    fake = FakeDocker([
        (1, "", "missing"),  # inspect -> missing
        (1, "", "boom"),     # create -> hard fail
        (1, "", "missing"),  # re-inspect -> still missing
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm._ensure_benchmark_network("benchmark-sandbox")) is False


def test_launch_path_self_heals_missing_network(monkeypatch):
    # The proxy-launch path creates the missing net BEFORE the run (the prod fix).
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),  # api image
        (1, "", "no such container"),                       # _proxy_state inspect -> not running
        (1, "", "no such container"),                       # _proxy_state ps fallback -> not running
        (0, "", ""),                                        # rm -f
        (1, "", "network not found"),                       # network inspect -> MISSING
        (0, "netid", ""),                                   # network create -> ok
        (0, "cid", ""),                                     # run
        (0, "", ""),                                        # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert any(c[:2] == ("network", "create") for c in fake.calls)   # self-healed
    # ordering: the create must precede the proxy run
    order = [c for c in fake.calls if c[:2] == ("network", "create") or (c and c[0] == "run")]
    assert order[0][:2] == ("network", "create") and order[1][0] == "run"


def test_self_container_id_prefers_mountinfo_over_stale_hostname(monkeypatch, tmp_path):
    # Watchtower clones bake the OLD container id in as the hostname; the real
    # id must come from the kernel (mountinfo), never gethostname().
    real_id = "a" * 64
    mi = tmp_path / "mountinfo"
    mi.write_text(
        f"1510 1373 8:2 /var/lib/docker/containers/{real_id}/hostname "
        "/etc/hostname rw,relatime - ext4 /dev/sda2 rw\n"
    )
    monkeypatch.setattr(rpm, "_MOUNTINFO_PATH", str(mi))
    monkeypatch.setattr(rpm, "_CGROUP_PATH", str(tmp_path / "absent"))
    monkeypatch.setattr(rpm.socket, "gethostname", lambda: "562d8cace782")  # stale
    assert rpm._self_container_id() == real_id


def test_self_container_id_falls_back_to_hostname(monkeypatch, tmp_path):
    # Non-container dev runs: no docker paths anywhere -> hostname fallback.
    plain = tmp_path / "mountinfo"
    plain.write_text("29 1 8:2 / / rw,relatime - ext4 /dev/sda2 rw\n")
    monkeypatch.setattr(rpm, "_MOUNTINFO_PATH", str(plain))
    monkeypatch.setattr(rpm, "_CGROUP_PATH", str(tmp_path / "absent"))
    monkeypatch.setattr(rpm.socket, "gethostname", lambda: "devbox")
    assert rpm._self_container_id() == "devbox"


def test_resolve_self_uses_real_container_id(monkeypatch, tmp_path):
    # The docker calls must be keyed by the mountinfo id, not the stale hostname.
    real_id = "b" * 64
    mi = tmp_path / "mountinfo"
    mi.write_text(f"1510 1373 8:2 /x/docker/containers/{real_id}/hostname /etc/hostname rw\n")
    monkeypatch.setattr(rpm, "_MOUNTINFO_PATH", str(mi))
    monkeypatch.setattr(rpm, "_CGROUP_PATH", str(tmp_path / "absent"))
    fake = FakeDocker([(0, "sha256:img|production_minotaur ", "")])
    monkeypatch.setattr(rpm, "_docker", fake)
    image, net = asyncio.run(rpm._resolve_self_image_and_net())
    assert image == "sha256:img" and net == "production_minotaur"
    assert fake.calls[0][1] == real_id  # inspect <real id>, not gethostname()


# ── live-solver net: derived proxy IP + retry-path attach ───────────────────


def test_live_proxy_ip_derived_from_subnet(monkeypatch):
    """The proxy's live-net IP is subnet base+5, so overriding ONLY the subnet
    keeps the pair consistent; SOLVER_LIVE_RPC_PROXY_IP pins it explicitly."""
    monkeypatch.delenv("SOLVER_LIVE_RPC_PROXY_IP", raising=False)
    assert rpm._live_proxy_ip("172.30.1.0/24") == "172.30.1.5"
    assert rpm._live_proxy_ip("10.99.7.0/24") == "10.99.7.5"
    assert rpm._live_proxy_ip("not-a-subnet") == "172.30.1.5"  # fail-safe default
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY_IP", "10.0.0.9")
    assert rpm._live_proxy_ip("172.30.1.0/24") == "10.0.0.9"


def test_default_live_subnet_avoids_aws_default_vpc():
    """172.31.0.0/16 is the AWS default-VPC CIDR (VPC DNS resolver at
    172.31.0.2) — an explicitly-subnetted docker bridge overlapping it
    blackholes host DNS / intra-VPC traffic on default-VPC EC2 hosts."""
    import ipaddress
    assert not ipaddress.ip_network(rpm.LIVE_SOLVER_NETWORK_SUBNET).overlaps(
        ipaddress.ip_network("172.31.0.0/16")
    )


def test_maybe_attach_live_net_gated_on_flag(monkeypatch):
    called = []

    async def _fake_attach():
        called.append(1)

    monkeypatch.setattr(rpm, "_attach_proxy_to_live_net", _fake_attach)
    monkeypatch.delenv("LIVE_SOLVER_RPC_VIA_PROXY", raising=False)
    asyncio.run(rpm._maybe_attach_live_net())
    assert called == []  # feature off => never touches docker
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    asyncio.run(rpm._maybe_attach_live_net())
    assert called == [1]


def test_ensure_retry_recovery_attaches_live_net(monkeypatch):
    """A degraded first ensure must not strand the live-net attach until the
    next restart: the background retry loop attaches on recovery too."""
    attached = []

    async def _fake_ensure():
        return True, False  # recovered on the first retry

    async def _fake_attach():
        attached.append(1)

    monkeypatch.setattr(rpm, "_ENSURE_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(rpm, "_ensure_impl", _fake_ensure)
    monkeypatch.setattr(rpm, "_maybe_attach_live_net", _fake_attach)

    async def _run():
        rpm._ensure_retry_task = None
        rpm._schedule_ensure_retry()
        await rpm._ensure_retry_task

    asyncio.run(_run())
    assert attached == [1]
