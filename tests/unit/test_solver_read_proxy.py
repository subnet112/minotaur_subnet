"""Unit tests for the orchestrator-side block-pin proxy client wiring."""
from __future__ import annotations

from minotaur_subnet.harness import solver_read_proxy as srp


def test_read_proxy_config_disabled(monkeypatch):
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    assert srp.read_proxy_config() is None


def test_read_proxy_config_defaults(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645/")  # trailing slash
    monkeypatch.delenv("SOLVER_READ_PROXY_CONTROL", raising=False)
    monkeypatch.delenv("SOLVER_READ_PROXY_TOKEN", raising=False)
    monkeypatch.delenv("SOLVER_READ_PROXY_CHAINS", raising=False)
    cfg = srp.read_proxy_config()
    assert cfg.url == "http://p:8645"  # stripped
    assert cfg.control_url == "http://p:8645"  # defaults to the data url
    assert cfg.token == ""
    assert cfg.chain_ids == (8453,)  # Base anchor default


def test_read_proxy_config_control_url_split(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://172.30.0.5:8645")  # solver/data (sandbox)
    monkeypatch.setenv("SOLVER_READ_PROXY_CONTROL", "http://rpc-pin-proxy:8645/")  # api/control
    cfg = srp.read_proxy_config()
    assert cfg.url == "http://172.30.0.5:8645"  # solver dials this
    assert cfg.control_url == "http://rpc-pin-proxy:8645"  # api dials this (stripped)


def test_read_proxy_config_explicit(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_TOKEN", "sekret")
    monkeypatch.setenv("SOLVER_READ_PROXY_CHAINS", "8453,1")
    cfg = srp.read_proxy_config()
    assert cfg.token == "sekret"
    assert cfg.chain_ids == (8453, 1)


def test_build_pin_blocks_only_routed_chains_present_in_map():
    cfg = srp.ReadProxyConfig(url="http://p", control_url="http://p", token="", chain_ids=(8453,))
    rpc_map = {8453: "u_base", 1: "u_eth"}
    # only routed (8453) chains in rpc_map are pinned, at fork_block; eth (not
    # routed) is left out
    assert srp.build_pin_blocks(cfg, rpc_map, 12345) == {"base": 12345}


def test_build_pin_blocks_empty_when_routed_chain_absent():
    cfg = srp.ReadProxyConfig(url="http://p", control_url="http://p", token="", chain_ids=(8453,))
    assert srp.build_pin_blocks(cfg, {1: "u_eth"}, 12345) == {}


def test_proxy_rpc_url_uses_chain_name():
    cfg = srp.ReadProxyConfig(url="http://p:8645", control_url="http://p:8645", token="", chain_ids=(8453,))
    assert srp.proxy_rpc_url(cfg, "s1", 8453) == "http://p:8645/rpc/s1/base"
    assert srp.proxy_rpc_url(cfg, "s1", 1) == "http://p:8645/rpc/s1/eth"
    assert srp.proxy_rpc_url(cfg, "s1", 964) == "http://p:8645/rpc/s1/btevm"


def test_pack_hash_block_rewrite_gating(monkeypatch):
    from minotaur_subnet.harness.rpc_budget_proxy.rewrite_table import rewrite_table_record
    # NOTE: round_anchored_pin_enabled() is DEFAULT ON (unset == pinned); only
    # {0,false,no,off} disable it.
    # no proxy -> None regardless of pin (byte-identical to a non-proxy fleet)
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    assert srp.pack_hash_block_rewrite() is None
    # proxy + pin (default-on) -> the rewrite record (reads route through proxy)
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    assert srp.pack_hash_block_rewrite() == rewrite_table_record()
    # proxy but pin EXPLICITLY off -> None (run_benchmark won't route w/o fork_block)
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")
    assert srp.pack_hash_block_rewrite() is None
    # pin back on but no proxy -> None
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    assert srp.pack_hash_block_rewrite() is None
