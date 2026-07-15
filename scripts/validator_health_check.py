#!/usr/bin/env python3
"""Validator health check + status report for subnet 112.

Produces two outputs on every run:

  ``summary.md``    — markdown status table covering every EVM in the
                      configured on-chain ValidatorRegistries. Pinned into
                      a tracking issue by the workflow so the latest
                      state is always one click away.
  ``findings.json`` — list of incident objects (stale weights, low Yuma
                      trust) the workflow opens GitHub issues for.

Exits 0 even when findings exist — the workflow inspects ``findings.json``
to decide which alerts to file. Exits non-zero only on a wiring error
(no registries configured, no subtensor reachable, etc.) so a broken
deploy surfaces as a red CI run instead of silent green.

Config (env vars):

  NETUID                       Subnet to inspect. Default 112.
  SUBTENSOR_NETWORK            Network arg to ``bt.Subtensor``. Default
                               "finney".
  STALE_THRESHOLD_SECONDS      Alert when last weight set is older than
                               this. Default 3600 (1 hour).
  LOW_TRUST_THRESHOLD          Alert when metagraph.trust < this.
                               Default 0.5.
  PROBE_TIMEOUT_SECONDS        Per-axon /identity HTTP timeout.
                               Default 5.
  REGISTRIES                   ``name|chain_id|rpc_url|registry_addr``
                               entries separated by commas. Order matters
                               only for the summary column order; chain
                               registration set is the union across all.

Example:

    REGISTRIES="Base|8453|https://mainnet.base.org|0x88a08d…,BT EVM|964|https://lite.chain.opentensor.ai|0x…"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp


# ── Config ───────────────────────────────────────────────────────────────


NETUID = int(os.environ.get("NETUID", "112"))
SUBTENSOR_NETWORK = os.environ.get("SUBTENSOR_NETWORK", "finney")
STALE_THRESHOLD_SECONDS = int(os.environ.get("STALE_THRESHOLD_SECONDS", "3600"))
LOW_TRUST_THRESHOLD = float(os.environ.get("LOW_TRUST_THRESHOLD", "0.5"))
PROBE_TIMEOUT_SECONDS = float(os.environ.get("PROBE_TIMEOUT_SECONDS", "5"))
# A champion-consensus node re-reads the on-chain registry every ~60s. If its
# last SUCCESSFUL read is older than this, the cached validator count/set it's
# computing quorum + peer discovery from is frozen → raise stale_registry_view.
REGISTRY_REFRESH_STALE_SECONDS = int(
    os.environ.get("REGISTRY_REFRESH_STALE_SECONDS", "600")
)
BLOCK_TIME_SECONDS = 12


REGISTRY_ABI = [
    {
        "name": "getValidators",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
]


# Hardcoded canonical EVM ↔ hotkey ↔ display-name map for subnet-112's
# active validator set. Two distinct fallback paths use it:
#
#   1. evm → uid: when the live /identity probe to an axon fails (probe
#      timeout, "No bittensor hotkey configured", connection refused —
#      all observed against current third-party operators), we look up
#      the hotkey here and use ``metagraph.hotkeys.index(hk)`` to recover
#      the uid. Without this, the row appears as "registry-only" even
#      though we have a known-good identity for the EVM on file.
#
#   2. display name: when ``metagraph.identities[uid]`` is empty (the
#      operator hasn't run subtensor's ``set_identity`` extrinsic — eg.
#      our own validator at uid=0), we fall back to the name listed
#      here.
#
# On-chain ``metagraph.identities[uid]`` always wins over this map when
# present — operators control their own display name on subtensor and
# we don't want to override that.
#
# Maintenance: whenever ``ValidatorRegistry.getValidators()`` on Base or
# BT EVM gains a new EVM, add an entry here. Worst case if missed: that
# operator's row falls back to "registry-only" when their axon is
# unreachable — same behaviour the script had before this map existed.
KNOWN_VALIDATORS: dict[str, dict[str, str | None]] = {
    "0x3f1649704bacf67eeed4b373f761dfadd9df504d": {
        "hotkey": "5E1ohAszHfhyQUEtz6mvCCkW4pYHsinPjxXS938fAZ2jFvCt",
        "name": "Minotaur Leader",
        "url": "https://minotaursubnet.com",
    },
    "0x19235203853dd4a8dbc7c717ec669c9391e16aa1": {
        "hotkey": "5FdtBrmYC1WHKfqs34ZDpQeQqZgQjY5D32EcYChswhiWs112",
        "name": "Rizzo (Insured)",
        "url": None,
    },
    "0x8f0bac1081661e193c21028dd1dd1002cd962d9a": {
        "hotkey": "5HBMtn1FvqANpG8d9comQpF1gTZWr1fu9aTd8qKQMUdbbAyo",
        "name": "TAO.com",
        "url": None,
    },
    "0x8d5aba035d54128ad4d5380866af8bf33bfb6bd7": {
        "hotkey": "5F27SMbBezy8YGdAn7zTKKNfyiHrkGP8ZdiLoJc5Prpdvsj6",
        "name": "Kraken",
        "url": None,
    },
    "0x7ef6fafcd590ad9f60fda6de093dbd238f3845b7": {
        "hotkey": "5C7N4wGWX2QhRtyHqknp2agx4wzu3q8zP1kNvqdwkCJ7HGHa",
        "name": "Yuma, a DCG Company",
        "url": None,
    },
}


@dataclass
class Registry:
    name: str
    chain_id: int
    rpc_url: str
    address: str


@dataclass
class ValidatorStatus:
    """One row of the summary table.

    ``hotkey``, ``uid``, ``stake``, ``trust``, ``last_update_seconds_ago``
    are populated only when the EVM has been mapped to a metagraph entry
    via ``/identity`` cross-attestation. Registry-only entries (EVMs
    on-chain in the ValidatorRegistry but with no discoverable metagraph
    axon — typically operators mid-deploy or with an unreachable daemon)
    leave these fields as None and the summary renders them as ``—``.
    """

    evm_address: str
    chain_registrations: dict[str, bool] = field(default_factory=dict)
    hotkey: str | None = None
    uid: int | None = None
    stake: float | None = None
    axon_url: str = ""
    axon_published: bool = False
    identity_reachable: bool = False
    last_update_seconds_ago: int | None = None
    trust: float | None = None
    # On-chain validator identity (set via subtensor's set_identity
    # extrinsic — coldkey-scoped). None when the validator hasn't
    # registered a display name. Pulled from ``metagraph.identities[uid]``
    # without an extra RPC since the default ``metagraph()`` load
    # already includes it in bittensor 10.x.
    display_name: str | None = None
    identity_url: str | None = None
    # /health-derived fields. Populated when the /health probe succeeded
    # on a daemon at PR #78 or later. ``health_reachable`` is the truthy
    # gate — when False, the rest are None / unknown.
    #
    # ``weight_source`` classifies this validator's weight-setting health:
    #   "self"     daemon reported a SUCCESSFUL emit within the staleness
    #              window (``last_successful_emit``). This is the real
    #              health question — "is it emitting often enough to stay
    #              stable?" — and transient/rate-limited failures (which
    #              overwrite ``last_emit``) don't mask it.
    #   "external" daemon hasn't succeeded within the window, yet the chain
    #              still shows recent weights — something else is keeping
    #              them alive (a standalone burn script, btcli, etc.). NOTE
    #              this cannot prove a second setter; it means "fresh weights
    #              not explained by a recent self-success."
    #   "no-emitter" weights_emitter_configured=false — daemon can't sign
    #              chain TXs at all (wallet didn't load).
    #   "stale"    neither a recent self-success nor recent chain weights.
    #   "unknown"  /health probe failed; can't classify.
    health_reachable: bool = False
    weights_emitter_configured: bool | None = None
    last_emit: dict | None = None
    # last_successful_emit: only advances on a successful set_weights (daemon
    # PR — separate from last_emit, the latest attempt). The PRIMARY signal
    # for weight_source. None when the daemon predates the field or /health
    # was unreachable.
    last_successful_emit: dict | None = None
    weight_source: str | None = None
    # ── Tier 1 additions ──
    # Raw fields lifted from /health when the probe succeeded. None when
    # the daemon predates the field (older image) OR /health was unreachable.
    image_sha: str | None = None
    owner_hotkey_resolved: bool | None = None
    # bt_init: the daemon's Bittensor bring-up state (dict with keys
    # configured/ok/attempts/error/error_at/retrying). Present on images
    # that carry the init-retry fix; None before that. When ok=false the
    # ``error`` field holds the ACTUAL exception that broke the bring-up —
    # alert texts prefer it over guessing the cause from downstream
    # symptoms (weights_emitter_configured / owner_hotkey_resolved).
    bt_init: dict | None = None
    loaded_intents: int | None = None
    uptime_seconds: float | None = None
    my_uid_reported: int | None = None
    my_last_update_block_cached: int | None = None
    block_loop_running: bool | None = None
    # ── Tier 2 derived signals ──
    # phantom_leader: daemon reports block_loop_running=True even though
    # it isn't the metagraph-elected leader (highest validator-permit
    # stake). On pre-init-retry images this indicated the fail-open
    # ``_is_leader=True`` branch after a startup sync error; on images
    # with the fix leadership fails closed, so a phantom implies
    # FORCE_LEADER=1 or an election bug. Cosmetic on prod since real
    # chain interactions need a gas-wallet, but worth surfacing.
    phantom_leader: bool = False
    # metagraph_sync_stale: their daemon's cached ``my_last_update_block``
    # diverges from the chain's current view by more than one tempo
    # (~360 blocks / 72 min). Means their MetagraphSync background loop
    # is wedged — they're voting on stale snapshots.
    metagraph_sync_stale: bool = False
    # axon_dns_drift: /identity.axon_url's DNS resolves to a different IP
    # than what the metagraph has on chain. Could be ELB rotation between
    # serve_axon ticks (benign) OR a serious config drift (alarming).
    # Surfaced for human review, no auto-alert.
    axon_dns_drift: bool = False
    # probe_latency_ms: wall-clock time the /identity request took. Useful
    # to spot operators on degrading networks.
    probe_latency_ms: int | None = None
    # image_drift_versus_stable: count of merges between operator's image
    # SHA and current :stable's SHA (commits behind). None when we can't
    # resolve (no GHCR access, no /health image_sha, etc.).
    image_commits_behind_stable: int | None = None
    # live_solver_running: api process's view of whether its live champion
    # solver session is currently usable. False ⇒ the Docker session crashed
    # and respawn is pending (or no genesis solver is configured). True ⇒
    # solver is up. None ⇒ field not present (older image) or api /health
    # unreachable / not the leader. Sampled from the api /health (port 8080)
    # not the validator's /health (port 9100).
    live_solver_running: bool | None = None
    # live_solver_respawn_count: how many times the api has respawned the
    # live solver since its current uptime started. A fast-rising count
    # means the solver is crash-looping (eg solver bug, persistent slow
    # RPC, or unreachable Anvil sim). None when /health field absent.
    live_solver_respawn_count: int | None = None
    # live_solver_last_crash_error: truncated string of the most recent
    # crash reason ("quote: Command quote timed out after 5.0s", etc).
    # Used to render an "Active alerts" finding when live_solver_running
    # is False and a recent crash is recorded.
    live_solver_last_crash_error: str | None = None
    # orderbook_stats: the validator /health ``orderbook`` block — a
    # ``status_value → count`` map (open/executed/failed/…). None when the
    # daemon predates the field or /health was unreachable; ``{}`` when the
    # daemon is up but tracking no orders. Surfaced in the transposed dump:
    # a leader with open orders vs. followers at zero is the steady state.
    orderbook_stats: dict | None = None
    # api_health: the raw api-process /health (port 8080) payload, kept for
    # the transposed dump so the leader's extra surface (benchmark worker,
    # solver-round coordinator, champion consensus, current round) renders
    # without a dataclass field per key. None for validator-only nodes
    # (third parties) and any host whose api didn't answer. The scalar
    # live_solver_* fields above are still lifted from this for alerting.
    api_health: dict | None = None


def parse_registries() -> list[Registry]:
    raw = os.environ.get("REGISTRIES", "").strip()
    if not raw:
        return []
    out: list[Registry] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        if len(parts) != 4:
            print(
                f"[warn] skipping malformed REGISTRIES entry: {entry!r} "
                f"(expected name|chain_id|rpc_url|registry_addr)",
                file=sys.stderr,
            )
            continue
        name, chain_id, rpc_url, address = (p.strip() for p in parts)
        try:
            out.append(Registry(name=name, chain_id=int(chain_id),
                                rpc_url=rpc_url, address=address))
        except ValueError as exc:
            print(f"[warn] bad chain_id in {entry!r}: {exc}", file=sys.stderr)
    return out


# ── Registry reads ───────────────────────────────────────────────────────


def fetch_registered_evms(
    registries: list[Registry],
) -> dict[str, dict[str, bool]]:
    """Read getValidators() across all configured registries.

    Returns ``{evm_lower: {chain_name: registered_bool}}``. Every chain
    in ``registries`` appears as a key in each inner dict — False when
    the registry read succeeded but the EVM wasn't in the list, True
    when it was. A failed registry read drops that chain entirely from
    the inner dict (so the summary shows the column blank — operator can
    tell "failed to read" vs "explicitly absent").
    """
    from web3 import Web3

    union: dict[str, dict[str, bool]] = {}
    chain_names: list[str] = []
    chains_with_successful_read: set[str] = set()

    for reg in registries:
        chain_names.append(reg.name)
        try:
            w3 = Web3(Web3.HTTPProvider(
                reg.rpc_url,
                request_kwargs={"timeout": 10},
            ))
            c = w3.eth.contract(
                address=Web3.to_checksum_address(reg.address),
                abi=REGISTRY_ABI,
            )
            evms = [v.lower() for v in c.functions.getValidators().call()]
            chains_with_successful_read.add(reg.name)
        except Exception as exc:
            print(
                f"[warn] failed to read registry {reg.name} at {reg.address}: {exc}",
                file=sys.stderr,
            )
            continue
        for evm in evms:
            union.setdefault(evm, {})[reg.name] = True

    # Fill in explicit False for chains that did get read but didn't list this EVM
    for evm in union:
        for chain in chains_with_successful_read:
            union[evm].setdefault(chain, False)
    return union


# ── Metagraph + identity discovery ───────────────────────────────────────


async def probe_identity(
    session: aiohttp.ClientSession,
    axon_url: str,
) -> tuple[dict | None, str | None]:
    """Fetch /identity at the axon, with one retry on timeout.

    Returns ``(data, error)`` — exactly one of them is non-None. ``data``
    is the parsed JSON identity payload on success; ``error`` is a
    short human-readable string on failure (HTTP status, timeout,
    connection refused, etc.). The error string is propagated up so the
    summary can show the operator WHY a probe didn't return — silent
    Nones were the original mistake (issue #59 v1 saw "identity-mapped
    1 of 5" from a GitHub runner and had no way to debug from the logs).

    Retry policy:
      - asyncio.TimeoutError → retry once. Cross-continent paths
        sometimes need >5s and a single retry is cheap.
      - HTTP 4xx/5xx, connection refused, DNS error → no retry. These
        are definitive: the daemon is either responding with an error or
        not listening. Retrying would only delay the (correct) failure.
    """
    url = axon_url.rstrip("/") + "/identity"
    last_err: str | None = None
    for attempt in range(2):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS),
            ) as r:
                if r.status != 200:
                    return None, f"HTTP {r.status}"
                return await r.json(), None
        except asyncio.TimeoutError:
            last_err = f"timeout after {PROBE_TIMEOUT_SECONDS}s"
            if attempt == 0:
                continue
        except aiohttp.ClientConnectorError as exc:
            return None, f"connect failed: {exc}"
        except aiohttp.ClientError as exc:
            return None, f"client error: {type(exc).__name__}: {exc}"
        except Exception as exc:  # JSON parse, etc.
            return None, f"unexpected: {type(exc).__name__}: {exc}"
    return None, last_err or "unknown"


async def probe_health(
    session: aiohttp.ClientSession,
    axon_url: str,
) -> tuple[dict | None, str | None]:
    """Fetch /health at the axon. Same retry policy as ``probe_identity``.

    Used to read PR #78's ``weights_emitter_configured`` /
    ``my_last_update_block`` / ``last_emit`` fields so the workflow can
    distinguish "weights set by our daemon" vs "weights set by some other
    process running against the same hotkey". Daemons predating #78
    answer 200 with the older subset of fields — callers must treat any
    missing field as None / unknown.
    """
    url = axon_url.rstrip("/") + "/health"
    last_err: str | None = None
    for attempt in range(2):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS),
            ) as r:
                if r.status != 200:
                    return None, f"HTTP {r.status}"
                return await r.json(), None
        except asyncio.TimeoutError:
            last_err = f"timeout after {PROBE_TIMEOUT_SECONDS}s"
            if attempt == 0:
                continue
        except aiohttp.ClientConnectorError as exc:
            return None, f"connect failed: {exc}"
        except aiohttp.ClientError as exc:
            return None, f"client error: {type(exc).__name__}: {exc}"
        except Exception as exc:
            return None, f"unexpected: {type(exc).__name__}: {exc}"
    return None, last_err or "unknown"


async def probe_api_health(
    session: aiohttp.ClientSession,
    axon_url: str,
) -> tuple[dict | None, str | None]:
    """Fetch the api process's /health (port 8080) on the same host as the axon.

    The validator daemon's /health (port 9100) and the api's /health
    (port 8080) live in separate containers but on the same Docker host
    per the canonical compose. The api exposes solver-runtime state that
    the daemon doesn't know about — most importantly ``live_solver_running``,
    which tells us whether the api can currently serve quote/order
    requests.

    Best-effort: third-party operators whose api isn't exposed on 8080
    (custom nginx, different port mapping, no api container at all) will
    silently produce a ``(None, err)`` here. The workflow degrades to
    "field unknown" rather than false-alarming.
    """
    try:
        from urllib.parse import urlparse as _urlparse
        parsed = _urlparse(axon_url)
        host = parsed.hostname
    except Exception as exc:
        return None, f"axon_url parse failed: {exc}"
    if not host:
        return None, "no host in axon_url"
    url = f"http://{host}:8080/health"
    last_err: str | None = None
    for attempt in range(2):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS),
            ) as r:
                if r.status != 200:
                    return None, f"HTTP {r.status}"
                return await r.json(), None
        except asyncio.TimeoutError:
            last_err = f"timeout after {PROBE_TIMEOUT_SECONDS}s"
            if attempt == 0:
                continue
        except aiohttp.ClientConnectorError as exc:
            return None, f"connect failed: {exc}"
        except aiohttp.ClientError as exc:
            return None, f"client error: {type(exc).__name__}: {exc}"
        except Exception as exc:
            return None, f"unexpected: {type(exc).__name__}: {exc}"
    return None, last_err or "unknown"


async def discover_identity_map(
    metagraph,
) -> tuple[dict[str, dict], list[dict], dict[int, dict], dict[int, dict]]:
    """Probe every axon-serving UID for /identity AND /health.

    Returns ``(identity_map, probe_outcomes, health_by_uid)``:
      - ``identity_map``: ``{evm_lower: {hotkey, axon_url, uid}}`` for
        UIDs whose /identity returned a valid binding.
      - ``probe_outcomes``: per-axon ``{uid, hotkey, axon_url, status,
        error, evm}`` — captures both successes and failures so the
        summary can render "Probed 5 axons: 4 ok / 1 timeout" with the
        per-axon reason. Crucial for diagnosing missing rows in the
        report (e.g. when a GitHub runner can't reach an operator's
        load-balancer in 5s but locally it's fine).
      - ``health_by_uid``: ``{uid: health_json}`` for UIDs whose /health
        probe succeeded. The /health and /identity probes are
        independent — one can succeed while the other fails. We key by
        uid rather than evm because /health on a pre-#78 daemon doesn't
        carry an evm_address.
    """
    candidates: list[tuple[int, str, str]] = []
    for uid, ax in enumerate(metagraph.axons):
        if ax.ip != "0.0.0.0" and ax.port != 0:
            candidates.append((uid, metagraph.hotkeys[uid], f"http://{ax.ip}:{ax.port}"))

    if not candidates:
        return {}, [], {}, {}

    async with aiohttp.ClientSession() as session:
        # Run all three probes in parallel per axon — single session
        # reuses connections. ``return_exceptions=False`` would surface
        # a probe exception as a task failure, but our probes already
        # trap and return ``(None, err)`` so this is safe.
        #
        # The third probe (api_health on port 8080) hits the api process
        # on the same host. Best-effort: operators whose api isn't
        # exposed on 8080 produce a ``(None, err)`` and the workflow
        # leaves those fields unknown. Currently only used to surface
        # ``live_solver_running`` for the swap-pipeline health view.
        identity_results, health_results, api_health_results = await asyncio.gather(
            asyncio.gather(*(probe_identity(session, url) for _, _, url in candidates)),
            asyncio.gather(*(probe_health(session, url) for _, _, url in candidates)),
            asyncio.gather(*(probe_api_health(session, url) for _, _, url in candidates)),
        )

    identity_map: dict[str, dict] = {}
    outcomes: list[dict] = []
    health_by_uid: dict[int, dict] = {}
    api_health_by_uid: dict[int, dict] = {}
    for (uid, hk, url), (data, err), (hdata, herr), (ahdata, aherr) in zip(
        candidates, identity_results, health_results, api_health_results,
    ):
        if hdata is not None:
            health_by_uid[uid] = hdata
        if ahdata is not None:
            api_health_by_uid[uid] = ahdata

        if data is not None:
            evm = (data.get("evm_address") or "").lower()
            if evm:
                identity_map[evm] = {
                    "hotkey": hk, "axon_url": url, "uid": uid,
                    "source": "probe",
                }
                outcomes.append({
                    "uid": uid, "hotkey": hk, "axon_url": url,
                    "status": "ok", "error": None, "evm": evm,
                    "health_ok": hdata is not None,
                    "health_error": herr,
                })
                print(
                    f"[probe] uid={uid:>3} {url} → ok evm={evm} "
                    f"health={'ok' if hdata is not None else f'FAIL({herr})'}",
                    file=sys.stderr,
                )
                continue
            err = "200 OK but payload missing evm_address"
        outcomes.append({
            "uid": uid, "hotkey": hk, "axon_url": url,
            "status": "fail", "error": err, "evm": None,
            "health_ok": hdata is not None,
            "health_error": herr,
        })
        print(
            f"[probe] uid={uid:>3} {url} → FAIL identity: {err} | "
            f"health={'ok' if hdata is not None else f'FAIL({herr})'}",
            file=sys.stderr,
        )

    # Fallback: for any KNOWN_VALIDATORS entry the live /identity probe
    # didn't bind, recover the uid by looking up the hardcoded hotkey in
    # ``metagraph.hotkeys``. Two failure modes this rescues, both seen on
    # current operators:
    #   - axon unreachable from the runner (port closed / connect refused)
    #   - daemon answers /identity with "No bittensor hotkey configured"
    #     (the operator hasn't wired a hotkey into VALIDATOR_PRIVATE_KEY)
    # Without this, the row falls through to "registry-only" even though
    # we already know who they are. Probe still has to fail for the
    # fallback to kick in — a successful probe whose EVM disagrees with
    # the hardcoded map would be a real conflict, not something to paper
    # over.
    hotkey_to_uid = {hk: i for i, hk in enumerate(metagraph.hotkeys)}
    outcomes_by_uid = {o["uid"]: o for o in outcomes}
    for evm_lower, known in KNOWN_VALIDATORS.items():
        if evm_lower in identity_map:
            continue
        hk = known.get("hotkey")
        if not hk:
            continue
        recovered_uid = hotkey_to_uid.get(hk)
        if recovered_uid is None:
            continue
        ax = metagraph.axons[recovered_uid]
        axon_url = (
            f"http://{ax.ip}:{ax.port}"
            if ax.ip != "0.0.0.0" and ax.port != 0 else ""
        )
        identity_map[evm_lower] = {
            "hotkey": hk, "axon_url": axon_url, "uid": recovered_uid,
            "source": "known-validators",
        }
        # Mark the corresponding probe outcome (if there was one — the
        # uid might not have been in the axon-serving candidates list at
        # all) so the diagnostics table can render "❌ HTTP 503 →
        # recovered" instead of a bare red X that suggests the row is
        # missing from the table.
        out = outcomes_by_uid.get(recovered_uid)
        if out is not None:
            out["recovered"] = True
            out["evm"] = evm_lower
        print(
            f"[probe] uid={recovered_uid:>3} {axon_url or '(no axon)'} → "
            f"recovered via KNOWN_VALIDATORS evm={evm_lower}",
            file=sys.stderr,
        )
    return identity_map, outcomes, health_by_uid, api_health_by_uid


def _classify_weight_source(
    s: ValidatorStatus,
    health: dict | None,
    *,
    now: float,
    stale_threshold_seconds: int,
    alignment_tolerance_seconds: int = 120,
) -> str | None:
    """Classify this validator's weight-setting health.

    See ``ValidatorStatus.weight_source`` for the codes. The question we
    actually care about is "is this validator's daemon setting weights
    successfully often enough to stay stable?" — answered by the LAST
    SUCCESSFUL emit, not the latest attempt. A transient/rate-limited
    failure overwrites ``last_emit`` with an error but leaves
    ``last_successful_emit`` untouched, so a validator that set weights
    minutes ago no longer false-flags as "external".

    Logic (primary path, daemon reports ``last_successful_emit``):

    * ``health is None`` or ``last_emit`` field absent     → "unknown"
    * ``weights_emitter_configured == false``              → "no-emitter"
    * ``last_successful_emit`` within stale window         → "self"
    * else chain ``last_update`` fresh                     → "external"
    * else                                                 → "stale"

    Legacy fallback: daemons that predate ``last_successful_emit`` are
    classified by the old ``last_emit`` timestamp-alignment heuristic so
    they don't all collapse to "unknown" during the rollout window.
    """
    if health is None:
        return "unknown"
    # Pre-PR-#75 daemons answer /health without ``last_emit`` at all.
    # Dict-membership (not truthiness) distinguishes "field absent" from
    # "present but None" (daemon that hasn't emitted yet).
    if "last_emit" not in health:
        return "unknown"
    if health.get("weights_emitter_configured") is False:
        return "no-emitter"

    chain_seconds_ago = s.last_update_seconds_ago

    # Primary signal: did our own daemon SUCCEED recently? ``last_successful_emit``
    # only advances on a successful set_weights, so a later failed retry can't
    # mask it. Membership test gates the new path so legacy daemons fall through.
    if "last_successful_emit" in health:
        lse = health.get("last_successful_emit") or {}
        lse_at = lse.get("attempted_at")
        if lse_at is not None and (now - lse_at) < stale_threshold_seconds:
            return "self"
        # Daemon hasn't succeeded within the window. Fresh chain weights mean
        # something else is keeping them alive (can't prove who).
        if chain_seconds_ago is not None and chain_seconds_ago < stale_threshold_seconds:
            return "external"
        return "stale"

    # ── Legacy fallback (daemon predates last_successful_emit) ──
    last_emit = health.get("last_emit")
    last_emit_at = (last_emit or {}).get("attempted_at")
    last_emit_ok = (last_emit or {}).get("result") == "ok"
    if (
        chain_seconds_ago is not None
        and last_emit_at is not None
        and last_emit_ok
    ):
        chain_set_at = now - chain_seconds_ago
        if abs(last_emit_at - chain_set_at) <= alignment_tolerance_seconds:
            return "self"
    if chain_seconds_ago is not None and chain_seconds_ago < stale_threshold_seconds:
        return "external"
    return "stale"


def _identify_leader_uid(metagraph) -> int | None:
    """Pick the canonical "order consensus leader" UID.

    Definition matches the validator daemon's own election (see
    minotaur_subnet/validator/metagraph_sync.py:elect_leader): highest-
    stake permit-holder with an active axon, ties broken by hotkey
    lexicographic order. Returns None when no validator on the metagraph
    has both a permit and an axon — e.g. the moments after a clean redeploy.
    """
    candidates = []
    n = int(metagraph.n.item()) if hasattr(metagraph.n, "item") else int(metagraph.n)
    for uid in range(n):
        try:
            if not bool(metagraph.validator_permit[uid]):
                continue
            ax = metagraph.axons[uid]
            if ax.ip == "0.0.0.0" or ax.port == 0:
                continue
            candidates.append((
                -float(metagraph.stake[uid]),  # negative for descending stake sort
                metagraph.hotkeys[uid],         # tie-break: ascending hotkey
                uid,
            ))
        except (IndexError, AttributeError):
            continue
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def _dns_resolve_first_ip(host: str) -> str | None:
    """Resolve ``host`` (hostname or IPv4 string) to a single IPv4 address.

    Returns None on resolution failure. When ``host`` is already a numeric
    IP, returns it unchanged. The fail-soft behaviour keeps the script
    running through transient DNS hiccups instead of poisoning the whole
    workflow run.
    """
    import socket as _socket
    try:
        return _socket.gethostbyname(host)
    except (OSError, _socket.gaierror):
        return None


def build_statuses(
    registered_evms: dict[str, dict[str, bool]],
    identity_map: dict[str, dict],
    health_by_uid: dict[int, dict],
    metagraph,
    current_block: int,
    all_chain_names: list[str],
    api_health_by_uid: dict[int, dict] | None = None,
) -> list[ValidatorStatus]:
    """Combine the registry union with the identity-map / metagraph view."""
    now = time.time()
    leader_uid = _identify_leader_uid(metagraph)
    out: list[ValidatorStatus] = []
    for evm, chain_regs in sorted(registered_evms.items()):
        # Backfill any chain that wasn't in the per-EVM map at all — happens
        # when a registry read failed; render blank in the summary.
        for chain in all_chain_names:
            chain_regs.setdefault(chain, None)  # type: ignore[arg-type]

        s = ValidatorStatus(evm_address=evm, chain_registrations=dict(chain_regs))

        info = identity_map.get(evm)
        if info is not None:
            uid = info["uid"]
            s.hotkey = info["hotkey"]
            s.uid = uid
            s.axon_url = info["axon_url"]
            # ``axon_published`` reflects the metagraph axon registration
            # (does the operator serve an axon at all), independent of
            # whether we could reach it. ``identity_reachable`` is the
            # /identity probe outcome — False when we recovered the
            # mapping from KNOWN_VALIDATORS rather than a live probe, so
            # the operator can still tell which axons are silent.
            ax = metagraph.axons[uid]
            s.axon_published = ax.ip != "0.0.0.0" and ax.port != 0
            s.identity_reachable = info.get("source") == "probe"
            s.stake = float(metagraph.stake[uid])
            # ``validator_trust`` is the post-Yuma trust score (how well
            # this UID's vote aligns with network consensus). Distinct
            # from ``trust`` (which doesn't exist on Metagraph in current
            # bittensor versions). Healthy validators sit ~0.95+; values
            # below ~0.5 indicate a divergent voter being penalized.
            s.trust = float(metagraph.validator_trust[uid])
            last_update_block = int(metagraph.last_update[uid])
            s.last_update_seconds_ago = (
                max(0, current_block - last_update_block) * BLOCK_TIME_SECONDS
            )
            # On-chain validator identity (set via subtensor's
            # set_identity extrinsic, coldkey-scoped). Present in
            # metagraph.identities for free — no extra RPC. Falsy / None
            # for validators that haven't registered a display name.
            ident = (metagraph.identities[uid]
                     if metagraph.identities and uid < len(metagraph.identities)
                     else None)
            if ident:
                # bittensor returns identities as a dict-like; tolerate both
                # dict and dataclass shapes for forward-compat.
                def _get(k):
                    if isinstance(ident, dict):
                        v = ident.get(k)
                    else:
                        v = getattr(ident, k, None)
                    if v in ("", "~", None):
                        return None
                    return str(v).strip() or None
                s.display_name = _get("name")
                s.identity_url = _get("url")

            # KNOWN_VALIDATORS fallback. Subtensor's on-chain identity
            # wins when present — operators control their own display
            # name and we don't override it. The hardcoded entry kicks in
            # only when the operator hasn't run ``set_identity`` (eg.
            # the subnet team's own validator at uid=0 today).
            if s.display_name is None:
                fallback = KNOWN_VALIDATORS.get(evm)
                if fallback is not None:
                    s.display_name = fallback.get("name")
                    if s.identity_url is None:
                        s.identity_url = fallback.get("url")

            # /health-derived fields. We always populate these from the
            # uid keyed map regardless of /identity success — a daemon
            # with a misconfigured /identity but a working /health is
            # still meaningful diagnostic signal.
            health = health_by_uid.get(uid)
            if health is not None:
                s.health_reachable = True
                s.weights_emitter_configured = health.get("weights_emitter_configured")
                s.last_emit = health.get("last_emit")
                s.last_successful_emit = health.get("last_successful_emit")
                # Tier 1: lift raw /health fields onto the status object.
                # Each is None when the daemon predates the field (older
                # image) — alert logic must handle that case gracefully.
                s.image_sha = health.get("image_sha")
                s.owner_hotkey_resolved = health.get("owner_hotkey_resolved")
                s.bt_init = health.get("bt_init")
                s.loaded_intents = health.get("loaded_intents")
                s.uptime_seconds = health.get("uptime_seconds")
                s.my_uid_reported = health.get("my_uid")
                s.my_last_update_block_cached = health.get("my_last_update_block")
                s.block_loop_running = health.get("block_loop_running")
                # NOTE: the daemon's `orderbook` is the block-loop's in-memory
                # WORKING SET (~always empty — followers never run the loop, and
                # even the leader only holds in-flight orders), so it reads 0 even
                # when the store has orders. The OrderBook column is sourced below
                # from the api's durable store count instead. We intentionally do
                # NOT set s.orderbook_stats from the daemon here.

            # Live-solver state from the api /health (port 8080 on the
            # same host). Best-effort: api may not be exposed there, in
            # which case the fields stay None and no finding fires.
            api_health = (api_health_by_uid or {}).get(uid)
            if api_health is not None:
                s.api_health = api_health
                # OrderBook column = DURABLE persisted count from the api store
                # (count_orders_by_status), not the daemon's live working set —
                # this is what makes a leader-vs-follower order-sync drift visible.
                # Absent/None → "—" (api unreachable, or a legacy image without the
                # field); {} → "0" (api reachable, store empty).
                s.orderbook_stats = api_health.get("orderbook")
                s.live_solver_running = api_health.get("live_solver_running")
                lsd = api_health.get("live_solver") or {}
                s.live_solver_respawn_count = lsd.get("respawn_count")
                s.live_solver_last_crash_error = lsd.get("last_crash_error")

            s.weight_source = _classify_weight_source(
                s,
                health,
                now=now,
                stale_threshold_seconds=STALE_THRESHOLD_SECONDS,
            )

            # ── Tier 2 derived signals ──
            # phantom_leader: their daemon ran the block_loop (claims to
            # be leader) but the canonical metagraph election says
            # someone else holds the leadership. Two ways to land here:
            # (a) the daemon's metagraph_sync hit an exception at startup
            # and fell through to ``_is_leader=True`` (we saw this with
            # Yuma DCG on 2026-05-27), or (b) the daemon explicitly
            # honors ``FORCE_LEADER=1`` env. Both are operator-side
            # configurations; we just surface the mismatch.
            if (
                leader_uid is not None
                and uid is not None
                and uid != leader_uid
                and s.block_loop_running is True
            ):
                s.phantom_leader = True

            # metagraph_sync_stale: the operator's daemon's CACHED
            # last_update_block (populated by their MetagraphSync loop)
            # diverges from the chain's ACTUAL current last_update for
            # the same UID by more than one tempo. Means their sync is
            # wedged — they emitted to chain but their daemon never saw
            # it back. Compares the operator's view (from /health) to
            # the workflow's fresh chain read (last_update_block).
            # Threshold = 360 blocks ≈ 72 min on mainnet (one tempo).
            if (
                s.my_last_update_block_cached is not None
                and s.my_last_update_block_cached > 0
                and last_update_block - s.my_last_update_block_cached > 360
            ):
                s.metagraph_sync_stale = True

            # axon_dns_drift: /identity advertises an axon_url (operator-
            # configured VALIDATOR_AXON_URL). We DNS-resolve it from the
            # workflow runner and compare against what the chain has on
            # the metagraph. Mismatch = operator's host is reachable at
            # a different IP than what they published. Common transient
            # cause: ELB IP rotation between the daemon's serve_axon
            # ticks (benign, expected to converge within an hour).
            if s.axon_url:
                from urllib.parse import urlparse as _urlparse
                parsed = _urlparse(s.axon_url)
                host = parsed.hostname or ""
                if host:
                    resolved = _dns_resolve_first_ip(host)
                    if resolved is not None and resolved != ax.ip:
                        s.axon_dns_drift = True

        out.append(s)
    return out


# ── Issue detection ──────────────────────────────────────────────────────


def _bt_init_excerpt(s: ValidatorStatus) -> str | None:
    """One-line root cause from the daemon's own bt_init state, or None.

    Returns text only when the daemon reports its Bittensor bring-up as
    configured-but-failed — the state where every downstream symptom
    (no emitter, unresolved owner hotkey, never-attempted set_weights)
    is secondary to the ONE recorded exception. None on older images
    that don't expose ``bt_init``, or when the bring-up succeeded.
    """
    bi = s.bt_init
    if not isinstance(bi, dict) or not bi.get("configured") or bi.get("ok"):
        return None
    err = (bi.get("error") or "unknown error")[:200]
    return (
        f"The daemon's own diagnosis (`bt_init`): Bittensor bring-up failed "
        f"and is retrying in the background (attempt {bi.get('attempts')}): "
        f"`{err}`."
    )


def _no_emitter_cause(s: ValidatorStatus) -> str:
    """The cause sentence for a ``weights_emitter_configured=false`` alert.

    Three-way on the daemon's ``bt_init`` report:

    * bring-up FAILED (ok=false) → quote the recorded exception; the
      missing emitter is collateral of that one failure.
    * bring-up SUCCEEDED (ok=true) → subtensor connect and owner lookup
      are fine, so the missing emitter is wallet-side: the daemon runs
      metagraph-only (WALLET_NAME/HOTKEY_NAME unset, or the wallet load
      failed with VALIDATOR_HOTKEY_SS58 covering for it).
    * no ``bt_init`` (image predates the field) → can't disambiguate;
      name BOTH candidates instead of asserting one.
    """
    failed_excerpt = _bt_init_excerpt(s)
    if failed_excerpt is not None:
        return failed_excerpt
    bi = s.bt_init
    if isinstance(bi, dict) and bi.get("configured") and bi.get("ok"):
        return (
            "The daemon reports its Bittensor bring-up SUCCEEDED "
            "(`bt_init.ok=true`) — subtensor connect and owner lookup are "
            "fine — so the emitter is missing because the wallet never "
            "loaded: WALLET_NAME / HOTKEY_NAME unset, or the wallet load "
            "failed and VALIDATOR_HOTKEY_SS58 covered for it "
            "(metagraph-only mode; classic cause: wallet dir not readable "
            "by uid 1000). The boot log's `Failed to load wallet` warning "
            "has the exact error."
        )
    return (
        "The Bittensor bring-up failed at startup. The recurring culprit "
        "is the subtensor websocket connect at boot (check SUBTENSOR_URL "
        "reachability / rate limits); a wallet-load failure (WALLET_NAME "
        "/ HOTKEY_NAME envs, wallet dir readable by uid 1000) looks the "
        "same from here. The daemon boot log's `Bittensor init failed:` "
        "line names the real one."
    )


def detect_findings(
    statuses: list[ValidatorStatus],
    *,
    onchain_btevm_validators: set[str] | None = None,
) -> list[dict]:
    """Return alert payloads for stale-weights / low-trust conditions.

    EVMs without a discoverable hotkey are skipped — they're registry-only
    entries (on-chain in the ValidatorRegistry but with no metagraph axon
    served), typically operators mid-deploy or with a daemon we couldn't
    reach this run. That state is reflected in the summary; it's not an
    "incident" in itself.

    ``onchain_btevm_validators`` is the runner's own read of the BT-EVM
    ValidatorRegistry ``getValidators()`` set (chain truth). When supplied,
    each node's reported ``champion_consensus.registry_view`` is diffed
    against it to raise ``stale_registry_view``. When None (e.g. unit tests
    or no BT-EVM registry configured), that check is skipped.
    """
    findings: list[dict] = []
    for s in statuses:
        if s.uid is None:
            continue  # not a metagraph validator — nothing to alert on

        if (
            s.last_update_seconds_ago is not None
            and s.last_update_seconds_ago > STALE_THRESHOLD_SECONDS
        ):
            # /health-derived diagnostics, when we got a probe through.
            # These narrow the failure mode for the operator instead of
            # leaving them to read daemon logs:
            #   no-emitter  → wallet didn't load, daemon can't set_weights
            #   external    → chain saw recent weights, but not from our
            #                 daemon. Probably stale-by-coincidence (the
            #                 weights are still fresh, just from a parallel
            #                 process) — but if the operator only runs our
            #                 stack, this is a smoking gun for a rogue
            #                 emitter on the same hotkey.
            extra = ""
            if s.weight_source == "no-emitter":
                extra = (
                    " The daemon is running but its weight emitter never "
                    "loaded (`weights_emitter_configured=false`). "
                    + _no_emitter_cause(s)
                )
            elif s.weight_source == "external":
                extra = (
                    " Note: chain `last_update` is still fresh, so this "
                    "row may auto-resolve without operator action — but "
                    "the recent weight-set was NOT from our daemon "
                    "(`last_emit` doesn't account for it). Another "
                    "process is signing for the same hotkey."
                )
            elif s.weight_source == "stale":
                last_emit = (s.last_emit or {})
                if last_emit.get("result") == "error":
                    extra = (
                        f" Daemon's last set_weights attempt failed: "
                        f"`{(last_emit.get('error') or '')[:200]}`."
                    )
            elif s.weight_source == "unknown":
                extra = (
                    " (/health probe failed from this runner — cannot tell "
                    "whether the daemon attempted recently.)"
                )
            findings.append({
                "type": "stale_weights",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "axon_url": s.axon_url,
                "display_name": s.display_name,
                "identity_url": s.identity_url,
                "weight_source": s.weight_source,
                "details": (
                    f"No weight update for {s.last_update_seconds_ago // 60} min "
                    f"(threshold {STALE_THRESHOLD_SECONDS // 60} min). "
                    f"Validator may be down, rate-limited, or its weight-emitter "
                    f"has crashed."
                    + extra
                ),
            })

        if s.trust is not None and s.trust < LOW_TRUST_THRESHOLD:
            findings.append({
                "type": "low_trust",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "display_name": s.display_name,
                "identity_url": s.identity_url,
                "axon_url": s.axon_url,
                "details": (
                    f"Yuma trust {s.trust:.3f} < threshold {LOW_TRUST_THRESHOLD:.2f}. "
                    f"This validator's weight vector diverges from consensus and "
                    f"is being penalized (reduced dividends). Check what it's "
                    f"voting on and why it differs from the rest of the network."
                ),
            })

        # ── Tier 1 finding types ──
        # All gated on health_reachable so we only fire when we got a
        # /health probe through. ``None`` fields are silent (predates the
        # daemon's PR — older operators won't false-fire).

        if s.health_reachable and s.weights_emitter_configured is False:
            findings.append({
                "type": "no_emitter",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "display_name": s.display_name,
                "identity_url": s.identity_url,
                "axon_url": s.axon_url,
                "details": (
                    "Daemon is up on /health but its weight emitter isn't "
                    "configured (`weights_emitter_configured=false`) — it "
                    "cannot sign chain TXs. "
                    + _no_emitter_cause(s)
                ),
            })

        if s.health_reachable and s.owner_hotkey_resolved is False:
            bt_excerpt = _bt_init_excerpt(s)
            findings.append({
                "type": "no_owner_hotkey",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "display_name": s.display_name,
                "identity_url": s.identity_url,
                "axon_url": s.axon_url,
                "details": (
                    "Daemon's subtensor lookup of SubnetOwnerHotkey "
                    "returned empty (`owner_hotkey_resolved=false`). "
                    "Result: emissions before a real miner champion go "
                    "into an empty weights dict and are silently dropped. "
                    "Check SUBTENSOR_URL connectivity OR set "
                    "SUBNET_OWNER_HOTKEY env as the fallback."
                    + (" " + bt_excerpt if bt_excerpt is not None else "")
                ),
            })

        # Live-solver-down finding: api /health surfaced
        # ``live_solver_running=false``, meaning the leader's swap pipeline
        # is currently broken (quote + order endpoints will 500 until
        # respawn or the next solver request succeeds). Only fires when
        # api /health was reachable AND the field is present AND false —
        # operators whose api doesn't expose /health on 8080 stay silent.
        if s.live_solver_running is False:
            crash_excerpt = (s.live_solver_last_crash_error or "")[:200]
            respawn_count = s.live_solver_respawn_count
            findings.append({
                "type": "live_solver_down",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "display_name": s.display_name,
                "identity_url": s.identity_url,
                "axon_url": s.axon_url,
                "details": (
                    f"Api process reports `live_solver_running=false` — the "
                    f"Docker session backing /v1/apps/*/quote and /orders "
                    f"is currently down. Quotes will 500 until the runtime "
                    f"auto-respawns on the next request "
                    f"(respawn_count so far: "
                    f"`{respawn_count if respawn_count is not None else 'unknown'}`). "
                    f"Last crash: `{crash_excerpt}`."
                    if crash_excerpt
                    else
                    f"Api process reports `live_solver_running=false` — the "
                    f"Docker session backing /v1/apps/*/quote and /orders "
                    f"is currently down. No champion may be adopted, OR the "
                    f"genesis solver image failed to start at boot. Check "
                    f"the api logs for 'Live champion container started' "
                    f"and any subsequent errors."
                ),
            })

        # Live-solver crash-loop finding: respawn count rising fast points
        # at a stuck/poisoned input rather than a one-off blip. Threshold
        # picked conservatively — 3+ respawns within a single workflow run
        # indicates real instability, not noise.
        if (
            s.live_solver_respawn_count is not None
            and s.live_solver_respawn_count >= 3
        ):
            crash_excerpt = (s.live_solver_last_crash_error or "")[:200]
            findings.append({
                "type": "live_solver_crashloop",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "display_name": s.display_name,
                "identity_url": s.identity_url,
                "axon_url": s.axon_url,
                "details": (
                    f"Live solver has respawned "
                    f"**{s.live_solver_respawn_count}** times since the "
                    f"api last started. Most recent crash: "
                    f"`{crash_excerpt or 'unknown'}`. A persistent crash "
                    f"loop usually means: (a) a malformed user order is "
                    f"hanging the solver, (b) an RPC endpoint (Alchemy, "
                    f"BT EVM) is unreachable, or (c) the solver image "
                    f"itself has a bug. Inspect api logs near the "
                    f"'Live solver respawned' INFO lines."
                ),
            })

        # No SUCCESSFUL emit in a long time — the actionable signal. We
        # deliberately do NOT alert on a single errored attempt: with the
        # ~100-block rate limit a daemon retrying slightly early logs routine
        # rate-limit failures while still setting weights every window. Only
        # a sustained gap (a daemon that WAS succeeding and then went quiet
        # for > the staleness window) means weights/reputation are at risk.
        # Gated on a positive last_successful_emit record so legacy daemons
        # (no field) and never-yet-emitted fresh daemons don't false-fire.
        lse = s.last_successful_emit
        if (
            s.health_reachable
            and s.weights_emitter_configured is True
            and lse is not None
            and lse.get("attempted_at") is not None
        ):
            success_age = time.time() - lse["attempted_at"]
            if success_age > STALE_THRESHOLD_SECONDS:
                findings.append({
                    "type": "no_successful_emit",
                    "validator_evm": s.evm_address,
                    "hotkey": s.hotkey,
                    "uid": s.uid,
                    "display_name": s.display_name,
                    "identity_url": s.identity_url,
                    "axon_url": s.axon_url,
                    "details": (
                        f"Daemon last set weights successfully "
                        f"**{_fmt_seconds_ago(int(success_age))}** "
                        f"(threshold {STALE_THRESHOLD_SECONDS // 60}m). "
                        f"Transient/rate-limited errors are fine, but a "
                        f"sustained gap means it's no longer landing weight-"
                        f"sets — the validator's weights and reputation will "
                        f"go stale. Check finney connectivity, the hotkey "
                        f"wallet, and the daemon's set_weights logs."
                    ),
                })

        if s.health_reachable and s.loaded_intents == 0:
            findings.append({
                "type": "no_loaded_intents",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "display_name": s.display_name,
                "identity_url": s.identity_url,
                "axon_url": s.axon_url,
                "details": (
                    "Daemon's JsExecutionEngine has 0 loaded App Intents. "
                    "Cannot score any incoming consensus proposals. Either "
                    "the app catalog sync (LEADER_API_URL polling) hasn't "
                    "run yet, or it's failing. Check the daemon logs for "
                    "'Loaded JS for new app' messages."
                ),
            })

        # ── Tier 2 finding types ──

        if s.phantom_leader:
            findings.append({
                "type": "phantom_leader",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "display_name": s.display_name,
                "identity_url": s.identity_url,
                "axon_url": s.axon_url,
                "details": (
                    "Daemon reports `block_loop_running=true` (claims to "
                    "be the order-consensus leader) but the metagraph "
                    "election picks a different validator. On images with "
                    "the init-retry fix (those exposing `bt_init` in "
                    "/health) leadership fails CLOSED, so this means "
                    "FORCE_LEADER=1 is set in their env — or a genuine "
                    "election bug. On older images the usual cause is a "
                    "startup exception falling through to the fail-open "
                    "`_is_leader=true` branch. Cosmetic on prod (followers "
                    "have no relayer key) but wastes CPU on a no-op "
                    "block_loop and pollutes logs."
                ),
            })

        if s.metagraph_sync_stale:
            findings.append({
                "type": "metagraph_sync_stuck",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "display_name": s.display_name,
                "identity_url": s.identity_url,
                "axon_url": s.axon_url,
                "details": (
                    "Daemon's cached `my_last_update_block` lags the "
                    "chain's actual value by more than one tempo "
                    "(~72 min). Their MetagraphSync background loop is "
                    "wedged — they're scoring + voting based on stale "
                    "metagraph snapshots. Check subtensor connectivity "
                    "and look for repeated 'Metagraph sync failed' "
                    "WARNINGs in the daemon log."
                ),
            })

        # axon_dns_drift is surfaced in the diagnostics table only — not
        # an alert (false positives during ELB rotation would spam).

        # ── stale champion-registry view ──
        # Compares the on-chain validator count + set THIS node is acting on
        # (lifted from /health.champion_consensus.registry_view) against chain
        # truth read by this runner. A mismatch is the root cause of an
        # impossible-looking quorum like 5-of-5 on a 6-validator network: the
        # node's quorum denominator AND its peer-discovery authorization both
        # derive from this view, so a stale one mis-certifies consensus and
        # silently drops live validators from discovery.
        if onchain_btevm_validators is not None:
            cc = (s.api_health or {}).get("champion_consensus") or {}
            rv = cc.get("registry_view") if cc.get("enabled") else None
            if rv:
                truth = {a.lower() for a in onchain_btevm_validators}
                reported_count = rv.get("on_chain_validator_count")
                reported_set = {a.lower() for a in (rv.get("on_chain_validators") or [])}
                msgs: list[str] = []
                # Count drift (the quorum denominator). The count is read +
                # cached separately from the set, and is sticky on RPC
                # failure — so it can be stale even when the set looks fresh.
                if reported_count is not None and reported_count != len(truth):
                    msgs.append(
                        f"reports on-chain validator count = {reported_count}, "
                        f"chain has {len(truth)} (quorum denominator is wrong)"
                    )
                # Set drift (drives peer-discovery authorization).
                missing = sorted(truth - reported_set)
                extra = sorted(reported_set - truth)
                if missing:
                    msgs.append(
                        "authorized set is MISSING current validator(s) "
                        + ", ".join(f"`{_short(a)}`" for a in missing)
                        + " — they get rejected by this node's peer discovery"
                    )
                if extra:
                    msgs.append(
                        "authorized set still lists departed validator(s) "
                        + ", ".join(f"`{_short(a)}`" for a in extra)
                    )
                # Freshness — a frozen cache is why a drift persists.
                err = rv.get("last_refresh_error")
                last_ok = rv.get("last_successful_refresh")
                if err:
                    msgs.append(f"registry refresh is erroring (`{str(err)[:120]}`)")
                elif last_ok is not None and (
                    time.time() - last_ok
                ) > REGISTRY_REFRESH_STALE_SECONDS:
                    msgs.append(
                        f"registry not refreshed in "
                        f"{int((time.time() - last_ok) // 60)}m (cache frozen)"
                    )
                if msgs:
                    blk = rv.get("rpc_block_number")
                    findings.append({
                        "type": "stale_registry_view",
                        "validator_evm": s.evm_address,
                        "hotkey": s.hotkey,
                        "uid": s.uid,
                        "display_name": s.display_name,
                        "identity_url": s.identity_url,
                        "axon_url": s.axon_url,
                        "details": (
                            "Stale on-chain validator-registry view: "
                            + "; ".join(msgs)
                            + f" (RPC at block {blk})."
                            + " This node computes its champion-consensus "
                            "quorum denominator and peer-discovery "
                            "authorization from this view, so it can "
                            "mis-certify consensus and drop live validators "
                            "until its BT-EVM RPC / registry config is fixed "
                            "and the api restarted. Verify with "
                            "`cast call <registry> \"getValidators()(address[])\" "
                            "--rpc-url <their-rpc>`."
                        ),
                    })

    return findings


# ── Markdown summary ─────────────────────────────────────────────────────


def _short(s: str, head: int = 6, tail: int = 4) -> str:
    if len(s) <= head + tail + 1:
        return s
    return f"{s[:head]}…{s[-tail:]}"


def _fmt_seconds_ago(secs: int | None) -> str:
    if secs is None:
        return "—"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d ago"


def _fmt_check(val: bool | None) -> str:
    if val is None:
        return "·"  # unknown — registry read failed
    return "✅" if val else "❌"


def _fmt_trust(t: float | None) -> str:
    if t is None:
        return "—"
    marker = "✅" if t >= LOW_TRUST_THRESHOLD else "⚠️"
    return f"{t:.3f} {marker}"


def _fmt_image(sha: str | None) -> str:
    """Render the image_sha column with a marker for non-standard builds.

    ``image_sha`` from /health is one of:
      - a short git SHA (e.g. "2f14b2e") — a GHCR-published build
      - a non-hex string like "dev" / "local" — built outside CI, no
        traceable provenance
      - None — daemon predates PR #70 (which added the field)

    Operators glance at this column to see whether everyone's on the same
    release; the ⚠ flags rogue local builds without forcing operators to
    learn a separate emoji legend.
    """
    if not sha:
        return "—"
    looks_like_hex = (
        len(sha) >= 7
        and all(c in "0123456789abcdef" for c in sha.lower())
    )
    if looks_like_hex:
        return f"`{sha[:8]}`"
    return f"`{sha}` ⚠"


def _fmt_uptime(secs: float | None) -> str:
    """Render uptime in a single compact unit: ``2h 14m`` / ``3d 4h``."""
    if secs is None:
        return "—"
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _fmt_block_loop(s: "ValidatorStatus") -> str:
    """Render block_loop_running as the operator's role this run.

    Distinguishes the three meaningful states:
      - phantom_leader (block_loop_running=True but NOT the elected leader)
        → ⚠ phantom-leader — see ``phantom_leader`` field doc for causes
      - block_loop_running=True (and IS the elected leader) → ✅ leader
      - block_loop_running=False → follower (normal state for non-leaders)
      - None → field absent (older image) or /health unreachable
    """
    if s.block_loop_running is None:
        return "—"
    if s.phantom_leader:
        return "⚠ phantom-leader"
    return "✅ leader" if s.block_loop_running else "follower"


def _fmt_last_emit(last_emit: dict | None, *, now: float) -> str:
    """Render last_emit as a compact ``Nm · source · ✅`` triple.

    Tuned for the transposed dump where this is one cell in a per-validator
    column, so it stays short: the ✅/❌ glyph carries the result (no
    trailing word) and ``source`` is abbreviated — ``burn`` for
    ``burn_fallback``, ``api`` for ``queued_from_api`` (PR #95's single-
    emit-path discriminator). ``—`` for the source when the daemon predates
    that field, and ``·`` for the result when no emit has been attempted yet.
    """
    if not last_emit:
        return "—"
    attempted = last_emit.get("attempted_at")
    when = "—"
    if attempted is not None:
        secs = int(max(0, now - attempted))
        when = _fmt_seconds_ago(secs)
    src_raw = last_emit.get("source")
    # "champion"/"burn" are the current emit labels; "burn_fallback" is the legacy label
    # kept here so already-persisted last_emit state still abbreviates cleanly.
    src = {
        "champion": "champ", "burn": "burn",
        "burn_fallback": "burn", "queued_from_api": "api",
    }.get(src_raw, src_raw or "—")
    result = last_emit.get("result")
    marker = {"ok": "✅", "error": "❌"}.get(result, "·")
    return f"{when} · {src} · {marker}"


def _fmt_orderbook(stats: dict | None) -> str:
    """Render the validator's OrderBook stats (``status → count``).

    Sourced from the api ``/health`` durable store count (all persisted
    orders), NOT the daemon's in-memory working set. ``—`` when unavailable
    (api ``:8080`` unreachable, or a legacy image without the field). ``0``
    when the api is reachable but the store is empty (a follower that hasn't
    synced the leader's order book, or simply no orders yet). Otherwise a
    compact ``status:count`` join, e.g. ``filled:32 rejected:46``.
    """
    if stats is None:
        return "—"
    if not stats:
        return "0"
    return " ".join(f"{k}:{v}" for k, v in sorted(stats.items()))


def _fmt_running(val: str | None) -> str:
    """Render an api ``running``/``disabled`` string as a glyph + label."""
    if val is None:
        return "—"
    if val == "running":
        return "✅ running"
    if val == "disabled":
        return "❌ disabled"
    return str(val)


def _fmt_solver_round(sr: dict | None) -> str:
    """Render the api ``solver_round`` block as ``#id status[· accepting]``."""
    if not sr:
        return "—"
    rid = sr.get("round_id")
    status = sr.get("status") or "?"
    tail = " · accepting" if sr.get("accepting_submissions") else ""
    return f"#{rid} {status}{tail}" if rid is not None else f"{status}{tail}"


def _fmt_champion_consensus(cc: dict | None) -> str:
    """Render the api ``champion_consensus`` block compactly.

    ``off`` when disabled, otherwise ``{quorum}-of-{validators}, {peers} peers``
    so config drift in the consensus set is visible at a glance.
    """
    if not cc:
        return "—"
    if not cc.get("enabled"):
        return "off"
    q = cc.get("quorum_required")
    n = cc.get("validator_count")
    peers = cc.get("peer_count")
    base = f"{q}-of-{n}" if q is not None and n is not None else "on"
    if peers is not None:
        base += f", {peers} peer{'s' if peers != 1 else ''}"
    return base


def _fmt_best_effort_quorum(bq: dict | None) -> str:
    """Render the monitor-only ``best_effort_champion_quorum`` tally compactly.

    ``—`` when absent (legacy image / gate off / no harvest yet), else
    ``{approved}/{validators} approved (target {req}@{bps}bps {OK|short}) missing=[..]``
    — how many validators approved the certified champion vs the broadcast/monitor
    target. A 3rd-party down/disagreeing shows in ``missing``; the champion still certs
    at the floor. A ⚠ marks a breached floor (live quorum > 1, deadlock risk).
    """
    if not bq:
        return "—"
    approved = bq.get("collected")
    n = bq.get("validator_count")
    req = bq.get("target_required")
    bps = bq.get("target_bps")
    verdict = "OK" if bq.get("would_reach_at_target") else "short"
    base = f"{approved}/{n} (target {req}@{bps}bps {verdict})"
    missing = bq.get("missing") or []
    if missing:
        base += f" missing=[{','.join(m[:8] for m in missing)}]"
    lq = bq.get("live_quorum_required")
    if isinstance(lq, int) and lq > 1:
        base += " ⚠floor-breached"
    return base


def _fmt_registry_view(cc: dict | None) -> str:
    """Render champion_consensus.registry_view — the on-chain count + block +
    refresh freshness the node is acting on.

    ``—`` when absent (daemon predates the field or api unreachable). Shows
    ``n=<count> · blk <h> · ok <age>`` normally, or ``⚠ err`` / ``⚠ <age>``
    when the registry refresh is failing / frozen. The automated chain-truth
    diff lives in detect_findings; this row is for eyeballing.
    """
    if not cc or not cc.get("enabled"):
        return "—"
    rv = cc.get("registry_view")
    if not rv:
        return "—"
    parts: list[str] = []
    count = rv.get("on_chain_validator_count")
    if count is not None:
        parts.append(f"n={count}")
    blk = rv.get("rpc_block_number")
    if blk:
        parts.append(f"blk {blk / 1e6:.2f}M" if blk >= 1_000_000 else f"blk {blk}")
    err = rv.get("last_refresh_error")
    last_ok = rv.get("last_successful_refresh")
    if err:
        parts.append("⚠ err")
    elif last_ok is not None:
        age = int(time.time() - last_ok)
        marker = "⚠ " if age > REGISTRY_REFRESH_STALE_SECONDS else "ok "
        parts.append(marker + _fmt_seconds_ago(age))
    return " · ".join(parts) if parts else "—"


def _fmt_emitter(configured: bool | None) -> str:
    if configured is None:
        return "—"
    return "✅" if configured else "❌"


def _fmt_loaded_intents(n: int | None) -> str:
    if n is None:
        return "—"
    return str(n)


def _fmt_live_solver(s: "ValidatorStatus") -> str:
    """Render live_solver state. ``—`` when api /health wasn't reachable."""
    if s.live_solver_running is None:
        return "—"
    marker = "✅" if s.live_solver_running else "❌"
    respawns = s.live_solver_respawn_count
    if respawns is None or respawns == 0:
        return marker
    return f"{marker} ({respawns} respawn{'s' if respawns != 1 else ''})"


def _fmt_weight_source(src: str | None, last_emit: dict | None = None) -> str:
    """Render the weight_source column for the dashboard.

    Codes are deliberately compact emoji + label so the table stays
    narrow at the cost of operators learning four glyphs once. The
    workflow-generated incident issue carries the prose explanation.

    For ``self`` classifications we append the ``last_emit.source``
    sub-indicator when available (post single-emit-path refactor):

      - ``self·queued`` — last emit was a per-miner ranking from the
        api EpochManager's queue POST (``source="queued_from_api"``).
      - ``self·champ`` — last emit weighted a resolved champion (the
        CHAMPION_MINER_WEIGHT_FRACTION split; ``source="champion"``).
      - ``self·burn`` — last emit was a definitive owner burn (no
        champion adopted; ``source="burn"``, or the legacy
        ``"burn_fallback"`` label from pre-rename daemons).

    Older daemons (pre-refactor) don't populate ``last_emit.source``;
    in that case we render just ``🟢 self`` exactly as before.
    """
    base = {
        None:          "—",
        "self":        "🟢 self",
        "external":    "🟠 external",
        "no-emitter":  "🔴 no-emitter",
        "stale":       "⚪ stale",
        "unknown":     "·",
    }.get(src, src or "—")
    if src == "self" and last_emit is not None:
        emit_source = (last_emit or {}).get("source")
        # "champion"/"burn" are the current emit labels (validator _epoch_loop);
        # "burn_fallback" is the pre-rename label still emitted by older daemons
        # during a staggered rollout; "queued_from_api" is the api-queue ranking.
        sub = {
            "queued_from_api": "queued",
            "champion": "champ",
            "burn": "burn",
            "burn_fallback": "burn",
        }.get(emit_source)
        if sub:
            return f"{base}·{sub}"
    return base


def _health_col_label(s: ValidatorStatus) -> str:
    """Short two-line column header for a validator: name over uid.

    Names are truncated to the first word (and anything before a paren
    dropped) so six columns stay narrow; the uid line disambiguates and
    matches the Probe-diagnostics table. ``<br>`` stacks the two lines —
    GitHub renders it inside table cells.
    """
    name = (s.display_name or "").split("(")[0].split(",")[0].strip()
    first = name.split(" ")[0] if name else _short(s.evm_address)
    uid = f"uid {s.uid}" if s.uid is not None else "uid —"
    return f"**{first}**<br>{uid}"


def _render_health_detail_table(statuses: list[ValidatorStatus]) -> str:
    """Render the per-validator ``/health`` deep-dive, transposed.

    Validators run *across* the columns and the fields run *down* the
    side, so adding a field grows the table downward (scrollable) rather
    than wider (GitHub wraps wide tables badly), and reading across any
    row surfaces the odd-one-out — config drift becomes visible at a
    glance. Rows are restricted to ``health_reachable`` validators;
    everyone else is implicitly "unknown" and already flagged in the main
    table's "Last set by" column.

    Two stacked sub-tables: the daemon /health (port 9100) fields are
    present for every reachable validator; the api-process /health (port
    8080) fields render as a second, narrower table covering only the
    columns whose api answered — typically just the elected leader running
    the swap pipeline — so validator-only nodes don't drag a wall of ``—``.
    """
    now = time.time()
    detailed = [s for s in statuses if s.health_reachable]
    detailed.sort(key=lambda s: s.uid if s.uid is not None else 1 << 30)
    if not detailed:
        return (
            "## Daemon /health detail\n\n"
            "_No `/health` probes succeeded this run — see Probe diagnostics "
            "for per-axon reasons._"
        )

    cols = [_health_col_label(s) for s in detailed]

    lines: list[str] = []
    lines.append("## Daemon /health detail")
    lines.append("")
    lines.append(
        f"Transposed — one column per validator whose `/health` answered "
        f"this run (**{len(detailed)}** of {len(statuses)}), fields down the "
        f"side. Read across a row to spot the odd one out. `—` = the daemon "
        f"predates that field (older image) or it's N/A for the daemon's role; "
        f"⚠ flags a known-bad value (non-hex image, phantom-leader)."
    )
    lines.append("")

    def _table(rows: list[tuple[str, list[str]]], headers: list[str]) -> None:
        head = ["Field"] + headers
        lines.append("| " + " | ".join(head) + " |")
        lines.append("| " + " | ".join(["---"] * len(head)) + " |")
        for label, cells in rows:
            lines.append("| " + " | ".join([label] + cells) + " |")

    daemon_rows: list[tuple[str, list[str]]] = [
        ("Image", [_fmt_image(s.image_sha) for s in detailed]),
        ("Uptime", [_fmt_uptime(s.uptime_seconds) for s in detailed]),
        ("Block loop", [_fmt_block_loop(s) for s in detailed]),
        ("Apps loaded", [_fmt_loaded_intents(s.loaded_intents) for s in detailed]),
        ("Weights emitter", [_fmt_emitter(s.weights_emitter_configured) for s in detailed]),
        ("Owner hotkey", [_fmt_emitter(s.owner_hotkey_resolved) for s in detailed]),
        ("Last success", [_fmt_last_emit(s.last_successful_emit, now=now) for s in detailed]),
        ("Last attempt", [_fmt_last_emit(s.last_emit, now=now) for s in detailed]),
        ("OrderBook", [_fmt_orderbook(s.orderbook_stats) for s in detailed]),
    ]
    _table(daemon_rows, cols)

    # API-process sub-table — only the columns whose port-8080 /health
    # answered (leader only, in practice). Omitted entirely when none did,
    # so third-party validator-only stacks don't show a dead section.
    api = [s for s in detailed if s.api_health is not None]
    if api:
        api_cols = [_health_col_label(s) for s in api]
        lines.append("")
        lines.append("### API process `/health` (port 8080)")
        lines.append("")
        lines.append(
            f"Only the elected leader runs the api / swap pipeline, so this "
            f"covers the **{len(api)}** validator(s) whose api answered — the "
            f"rest expose nothing on 8080."
        )
        lines.append("")
        api_rows: list[tuple[str, list[str]]] = [
            ("Live solver", [_fmt_live_solver(s) for s in api]),
            ("Benchmark worker", [_fmt_running((s.api_health or {}).get("benchmark_worker")) for s in api]),
            ("Solver-round coord", [_fmt_running((s.api_health or {}).get("solver_round_coordinator")) for s in api]),
            ("Solver-round role", [(s.api_health or {}).get("solver_round_role") or "—" for s in api]),
            ("Current round", [_fmt_solver_round((s.api_health or {}).get("solver_round")) for s in api]),
            ("Champion consensus", [_fmt_champion_consensus((s.api_health or {}).get("champion_consensus")) for s in api]),
            ("Champion quorum (best-effort)", [_fmt_best_effort_quorum((s.api_health or {}).get("best_effort_champion_quorum")) for s in api]),
            ("Registry view", [_fmt_registry_view((s.api_health or {}).get("champion_consensus")) for s in api]),
        ]
        _table(api_rows, api_cols)

    lines.append("")
    lines.append(
        "_**Last success** = most recent *successful* set_weights (the "
        "health-relevant signal); **Last attempt** = latest attempt of any "
        "result. Format `time · source · result` — source ``burn`` = "
        "burn-to-owner fallback, ``api`` = per-miner ranking queued by the api "
        "EpochManager (PR #95 discriminator); ✅=ok ❌=error, ``·``=not yet "
        "attempted. A ❌ on **Last attempt** with a recent **Last success** is "
        "normal (a rate-limited retry between epochs). **Block loop**: ``✅ "
        "leader`` elected order-consensus leader, ``follower`` normal non-"
        "leader, ``⚠ phantom-leader`` daemon claims leadership the metagraph "
        "election doesn't grant. **OrderBook**: DURABLE persisted order counts "
        "by status from the api `/health` store (`status:count`); ``—`` = api "
        "`:8080` unreachable / legacy image; ``0`` = reachable but store empty "
        "(follower not synced, or no orders). Leader vs. followers diverging here "
        "flags an order-sync problem._"
    )
    return "\n".join(lines)


def render_summary(
    statuses: list[ValidatorStatus],
    findings: list[dict],
    *,
    probe_outcomes: list[dict],
    chain_names: list[str],
    current_block: int,
    netuid: int,
) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    header = ["Name", "EVM", "Hotkey", "UID", "Stake (TAO)", "Image", "Last weights", "Last set by", "Trust", "Axon", "/identity"]
    header.extend(chain_names)
    sep = ["---"] * len(header)

    n_probes = len(probe_outcomes)
    n_probes_ok = sum(1 for o in probe_outcomes if o["status"] == "ok")
    n_recovered = sum(1 for o in probe_outcomes if o.get("recovered"))
    # "Coverage" = whether every registered validator made it into the
    # table with a uid binding. A failed probe that was rescued by
    # KNOWN_VALIDATORS still counts as covered; only validators whose
    # row is genuinely missing a hotkey (and a registry-only entry that
    # ISN'T in the known map) trigger the warning.
    unmapped_registry = sum(1 for s in statuses if s.uid is None)
    coverage_incomplete = unmapped_registry > 0

    lines: list[str] = []
    lines.append("# Validator Health Status")
    lines.append("")
    lines.append(f"_Last updated: **{ts}**  ·  netuid={netuid}  ·  block={current_block:,}_")
    if coverage_incomplete:
        # Visible banner above the table — a row without a hotkey can be
        # either a registry-only entry (operator mid-deploy) or a known
        # validator whose axon was unreachable AND isn't in
        # KNOWN_VALIDATORS. The "Probe diagnostics" section below lists
        # each failure with its reason.
        lines.append("")
        lines.append(
            f"> ⚠️ **{unmapped_registry} row(s) without a hotkey** — "
            f"`/identity` succeeded on {n_probes_ok}/{n_probes} live probes "
            f"(+{n_recovered} recovered via `KNOWN_VALIDATORS`). The rows "
            f"below may be unreachable operators or new registrations not "
            f"yet in the known-validators map — see the Probe diagnostics "
            f"section."
        )
    elif n_recovered:
        # Subtler — coverage is complete, but operators should know how
        # many rows leaned on the fallback this run (so a sustained-high
        # number flags an upstream issue like a runner-network change).
        lines.append("")
        lines.append(
            f"> ℹ️ Full coverage: **{n_probes_ok}/{n_probes}** live `/identity` "
            f"probes succeeded; **{n_recovered}** row(s) recovered via "
            f"`KNOWN_VALIDATORS` (see Probe diagnostics for which axons)."
        )
    lines.append("")
    lines.append(f"## Registered validators ({len(statuses)})")
    lines.append("")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(sep) + " |")

    for s in statuses:
        if s.display_name:
            # Bold the name; if the operator set a URL, link it.
            name_cell = (f"**[{s.display_name}]({s.identity_url})**"
                         if s.identity_url else f"**{s.display_name}**")
        elif s.uid is not None:
            name_cell = "_(no identity set)_"
        else:
            name_cell = "_(registry-only)_"
        row = [
            name_cell,
            f"`{_short(s.evm_address)}`",
            f"`{_short(s.hotkey or '—', head=8, tail=4)}`" if s.hotkey else "—",
            str(s.uid) if s.uid is not None else "—",
            f"{s.stake:,.0f}" if s.stake is not None else "—",
            _fmt_image(s.image_sha) if s.uid is not None else "—",
            _fmt_seconds_ago(s.last_update_seconds_ago),
            _fmt_weight_source(s.weight_source, s.last_emit) if s.uid is not None else "—",
            _fmt_trust(s.trust),
            _fmt_check(s.axon_published) if s.uid is not None else "—",
            _fmt_check(s.identity_reachable) if s.uid is not None else "—",
        ]
        for chain in chain_names:
            row.append(_fmt_check(s.chain_registrations.get(chain)))
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    lines.append(
        "_**Last set by** legend: 🟢 self = our validator daemon emitted; "
        "🟠 external = chain saw recent weights but `last_emit` doesn't "
        "account for it (another process is signing on this hotkey); "
        "🔴 no-emitter = daemon's weight emitter never loaded "
        "(`weights_emitter_configured=false`); ⚪ stale = neither chain "
        "nor daemon shows a recent successful set; · = /health probe "
        "failed this run. Sub-indicators on `🟢 self`: `·queued` = "
        "per-miner ranking from api EpochManager; `·burn` = burn fallback "
        "(no champion adopted, or queue was empty). **Image** column "
        "⚠ marker = non-hex `image_sha` (operator is running a local/dev "
        "build, not a GHCR-published one)._"
    )
    lines.append("")
    lines.append(_render_health_detail_table(statuses))
    lines.append("")
    lines.append("## Active alerts")
    lines.append("")
    if not findings:
        lines.append("_None._")
    else:
        for f in findings:
            kind = {
                "stale_weights": "Stale weights",
                "low_trust": "Low Yuma trust",
                "no_emitter": "Weight emitter not configured",
                "no_owner_hotkey": "Owner hotkey unresolved",
                "recent_emit_error": "Recent emit error",
                "no_loaded_intents": "No loaded App Intents",
                "phantom_leader": "Phantom-leader state",
                "metagraph_sync_stuck": "Metagraph sync wedged",
                "live_solver_down": "Live solver down",
                "live_solver_crashloop": "Live solver crash-looping",
                "stale_registry_view": "Stale registry view",
            }.get(f["type"], f["type"])
            name = f.get("display_name")
            if name and f.get("identity_url"):
                who = f"**[{name}]({f['identity_url']})** (`{_short(f['validator_evm'])}`)"
            elif name:
                who = f"**{name}** (`{_short(f['validator_evm'])}`)"
            else:
                who = f"`{_short(f['validator_evm'])}`"
            lines.append(
                f"- **{kind}** — {who} "
                f"— uid={f['uid']}, hk=`{_short(f['hotkey'] or '?', 8, 4)}`\n"
                f"  {f['details']}"
            )

    lines.append("")
    lines.append("## Probe diagnostics")
    lines.append("")
    if not probe_outcomes:
        lines.append("_No axon-serving UIDs in the metagraph this run._")
    else:
        lines.append(
            f"Probed **{n_probes}** axon-serving UID(s) in the subnet-{netuid} "
            f"metagraph; **{n_probes_ok}** answered with a valid identity, "
            f"**{n_probes - n_probes_ok}** failed."
        )
        lines.append("")
        lines.append("| UID | Axon | Outcome | EVM (if mapped) |")
        lines.append("| --- | --- | --- | --- |")
        for o in sorted(probe_outcomes, key=lambda x: x["uid"]):
            if o["status"] == "ok":
                outcome = "✅ ok"
            elif o.get("recovered"):
                # Probe failed but the row IS in the table — KNOWN_VALIDATORS
                # supplied the evm↔hotkey binding. Annotate so operators
                # don't waste time investigating a "missing" validator.
                outcome = f"❌ {o['error']} → recovered via `KNOWN_VALIDATORS`"
            else:
                outcome = f"❌ {o['error']}"
            evm_col = f"`{_short(o['evm'])}`" if o["evm"] else "—"
            lines.append(
                f"| {o['uid']} | `{o['axon_url']}` | {outcome} | {evm_col} |"
            )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_Generated by `.github/workflows/validator-health.yml` "
        "(runs every 15 min). Configuration: `STALE_THRESHOLD_SECONDS="
        f"{STALE_THRESHOLD_SECONDS}`, `LOW_TRUST_THRESHOLD={LOW_TRUST_THRESHOLD}`, "
        f"`PROBE_TIMEOUT_SECONDS={PROBE_TIMEOUT_SECONDS}`, "
        f"`REGISTRY_REFRESH_STALE_SECONDS={REGISTRY_REFRESH_STALE_SECONDS}`._"
    )
    return "\n".join(lines) + "\n"


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> int:
    registries = parse_registries()
    if not registries:
        print(
            "ERROR: no registries configured. Set REGISTRIES env "
            "(format: name|chain_id|rpc_url|registry_addr, comma-separated)",
            file=sys.stderr,
        )
        return 2

    print(
        f"[info] checking netuid={NETUID} on {SUBTENSOR_NETWORK} "
        f"across {len(registries)} registries: "
        f"{', '.join(r.name for r in registries)}",
        file=sys.stderr,
    )

    registered = fetch_registered_evms(registries)
    print(f"[info] {len(registered)} unique EVMs registered across all chains",
          file=sys.stderr)

    # Lazy import — bittensor is heavy. If it fails to import we'd rather
    # error here than at module load time.
    try:
        import bittensor as bt
    except ImportError as exc:
        print(f"ERROR: bittensor import failed: {exc}", file=sys.stderr)
        return 2

    try:
        sub = bt.Subtensor(network=SUBTENSOR_NETWORK)
        metagraph = sub.metagraph(netuid=NETUID)
        current_block = int(sub.get_current_block())
    except Exception as exc:
        print(f"ERROR: subtensor query failed: {exc}", file=sys.stderr)
        return 2

    identity_map, probe_outcomes, health_by_uid, api_health_by_uid = asyncio.run(
        discover_identity_map(metagraph)
    )
    total_axons = sum(
        1 for ax in metagraph.axons if ax.ip != "0.0.0.0" and ax.port != 0
    )
    print(
        f"[info] identity-mapped {len(identity_map)} of {total_axons} "
        f"axon-serving UIDs; /health-reachable on {len(health_by_uid)}; "
        f"api-/health-reachable on {len(api_health_by_uid)}",
        file=sys.stderr,
    )

    statuses = build_statuses(
        registered_evms=registered,
        identity_map=identity_map,
        health_by_uid=health_by_uid,
        metagraph=metagraph,
        current_block=current_block,
        all_chain_names=[r.name for r in registries],
        api_health_by_uid=api_health_by_uid,
    )

    # Chain truth for the stale_registry_view diff: the BT-EVM (chain 964)
    # ValidatorRegistry getValidators() set this runner just read. Champion
    # consensus is anchored on BT EVM, so that's the registry each node's
    # registry_view should match. None if no BT-EVM registry is configured.
    btevm_reg = next((r for r in registries if r.chain_id == 964), None)
    onchain_btevm = None
    if btevm_reg is not None:
        onchain_btevm = {
            evm for evm, regs in registered.items()
            if regs.get(btevm_reg.name) is True
        }

    findings = detect_findings(statuses, onchain_btevm_validators=onchain_btevm)

    summary_md = render_summary(
        statuses=statuses,
        findings=findings,
        probe_outcomes=probe_outcomes,
        chain_names=[r.name for r in registries],
        current_block=current_block,
        netuid=NETUID,
    )
    with open("summary.md", "w") as f:
        f.write(summary_md)

    # ``observed_uids`` carries the per-uid health state for validators we
    # actually probed this run. The workflow uses it to safely auto-close
    # alert issues — closing only on POSITIVE evidence the condition has
    # cleared, never on the absence of a finding (which could mean the
    # probe just failed and we don't know).
    observed_uids = {
        s.uid: {
            "evm": s.evm_address,
            "display_name": s.display_name,
            "last_update_seconds_ago": s.last_update_seconds_ago,
            "trust": s.trust,
            "weight_source": s.weight_source,
            "weights_emitter_configured": s.weights_emitter_configured,
            # Did the /health probe succeed this run? The auto-close logic
            # uses this to gate /health-derived alert types — it won't close
            # them on a probe miss (no positive evidence the condition cleared).
            "health_reachable": s.health_reachable,
            "stale_weights": (
                s.last_update_seconds_ago is not None
                and s.last_update_seconds_ago > STALE_THRESHOLD_SECONDS
            ),
            "low_trust": (
                s.trust is not None and s.trust < LOW_TRUST_THRESHOLD
            ),
        }
        for s in statuses if s.uid is not None
    }

    with open("findings.json", "w") as f:
        json.dump({
            "findings": findings,
            "observed_uids": observed_uids,
            "generated_at": int(time.time()),
            "netuid": NETUID,
            "current_block": current_block,
            "thresholds": {
                "stale_weights_seconds": STALE_THRESHOLD_SECONDS,
                "low_trust": LOW_TRUST_THRESHOLD,
            },
        }, f, indent=2)

    print(
        f"[info] wrote summary.md ({len(summary_md)} bytes) + "
        f"findings.json ({len(findings)} findings)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
