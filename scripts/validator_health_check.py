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
    # ``weight_source`` classifies who last set weights on this hotkey:
    #   "self"     daemon emitted; ``last_emit.attempted_at`` aligns with the
    #              chain's ``last_update`` block time (within tolerance).
    #   "external" chain shows a recent weight-set, but our daemon's last
    #              emit doesn't account for it (None, older, or failed).
    #              Indicates another process is also setting weights on the
    #              same hotkey (eg. a standalone burn script, btcli, or a
    #              second validator implementation running in parallel).
    #   "no-emitter" weights_emitter_configured=false — daemon can't sign
    #              chain TXs at all (wallet didn't load).
    #   "stale"    chain hasn't seen weights within the staleness window
    #              AND daemon hasn't emitted recently either.
    #   "unknown"  /health probe failed; can't classify.
    health_reachable: bool = False
    weights_emitter_configured: bool | None = None
    last_emit: dict | None = None
    weight_source: str | None = None


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


async def discover_identity_map(
    metagraph,
) -> tuple[dict[str, dict], list[dict], dict[int, dict]]:
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
        return {}, [], {}

    async with aiohttp.ClientSession() as session:
        # Run both probes in parallel per axon — single session reuses
        # connections. ``return_exceptions=False`` would surface a probe
        # exception as a task failure, but our probes already trap and
        # return ``(None, err)`` so this is safe.
        identity_results, health_results = await asyncio.gather(
            asyncio.gather(*(probe_identity(session, url) for _, _, url in candidates)),
            asyncio.gather(*(probe_health(session, url) for _, _, url in candidates)),
        )

    identity_map: dict[str, dict] = {}
    outcomes: list[dict] = []
    health_by_uid: dict[int, dict] = {}
    for (uid, hk, url), (data, err), (hdata, herr) in zip(
        candidates, identity_results, health_results,
    ):
        if hdata is not None:
            health_by_uid[uid] = hdata

        if data is not None:
            evm = (data.get("evm_address") or "").lower()
            if evm:
                identity_map[evm] = {"hotkey": hk, "axon_url": url, "uid": uid}
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
    return identity_map, outcomes, health_by_uid


def _classify_weight_source(
    s: ValidatorStatus,
    health: dict | None,
    *,
    now: float,
    stale_threshold_seconds: int,
    alignment_tolerance_seconds: int = 120,
) -> str | None:
    """Decide who last set weights for this validator's hotkey.

    See ``ValidatorStatus.weight_source`` for the codes. Logic:

    * ``health is None``                                   → "unknown"
    * health says ``weights_emitter_configured == false``  → "no-emitter"
    * ``last_emit.result == "ok"`` AND its ``attempted_at``
      is within ±``alignment_tolerance_seconds`` of the
      chain's last_update wall-clock time                  → "self"
    * chain ``last_update_seconds_ago`` fresh
      (< ``stale_threshold_seconds``)                      → "external"
    * otherwise                                            → "stale"

    The alignment tolerance covers the gap between when our daemon called
    ``set_weights`` and when the extrinsic actually landed in a finalized
    block (one or two blocks of subtensor latency, plus clock skew between
    the daemon and a GitHub runner). 120s is a generous default — false
    "external" classifications were the larger risk than false "self"
    ones, so we err toward "self" when in doubt.
    """
    if health is None:
        return "unknown"
    # Pre-PR-#75 daemons answer /health without ``last_emit`` at all.
    # We can't infer source from their response — don't false-flag as
    # "external" just because the field is missing. Dict-membership test
    # (not truthiness) distinguishes "field absent" from "field present
    # but None" (#75+ daemon that hasn't emitted yet).
    if "last_emit" not in health:
        return "unknown"
    if health.get("weights_emitter_configured") is False:
        return "no-emitter"

    chain_seconds_ago = s.last_update_seconds_ago
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


def build_statuses(
    registered_evms: dict[str, dict[str, bool]],
    identity_map: dict[str, dict],
    health_by_uid: dict[int, dict],
    metagraph,
    current_block: int,
    all_chain_names: list[str],
) -> list[ValidatorStatus]:
    """Combine the registry union with the identity-map / metagraph view."""
    now = time.time()
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
            s.axon_published = True
            s.identity_reachable = True
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

            # /health-derived fields. We always populate these from the
            # uid keyed map regardless of /identity success — a daemon
            # with a misconfigured /identity but a working /health is
            # still meaningful diagnostic signal.
            health = health_by_uid.get(uid)
            if health is not None:
                s.health_reachable = True
                s.weights_emitter_configured = health.get("weights_emitter_configured")
                s.last_emit = health.get("last_emit")
            s.weight_source = _classify_weight_source(
                s,
                health,
                now=now,
                stale_threshold_seconds=STALE_THRESHOLD_SECONDS,
            )
        out.append(s)
    return out


# ── Issue detection ──────────────────────────────────────────────────────


def detect_findings(statuses: list[ValidatorStatus]) -> list[dict]:
    """Return alert payloads for stale-weights / low-trust conditions.

    EVMs without a discoverable hotkey are skipped — they're registry-only
    entries (on-chain in the ValidatorRegistry but with no metagraph axon
    served), typically operators mid-deploy or with a daemon we couldn't
    reach this run. That state is reflected in the summary; it's not an
    "incident" in itself.
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
                    "loaded (`weights_emitter_configured=false`) — almost "
                    "certainly a wallet-load failure on startup. Check "
                    "WALLET_NAME / HOTKEY_NAME envs and that the wallet "
                    "dir is readable by uid 1000."
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


def _fmt_weight_source(src: str | None) -> str:
    """Render the weight_source column for the dashboard.

    Codes are deliberately compact emoji + label so the table stays
    narrow at the cost of operators learning four glyphs once. The
    workflow-generated incident issue carries the prose explanation.
    """
    return {
        None:          "—",
        "self":        "🟢 self",
        "external":    "🟠 external",
        "no-emitter":  "🔴 no-emitter",
        "stale":       "⚪ stale",
        "unknown":     "·",
    }.get(src, src or "—")


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

    header = ["Name", "EVM", "Hotkey", "UID", "Stake (TAO)", "Last weights", "Last set by", "Trust", "Axon", "/identity"]
    header.extend(chain_names)
    sep = ["---"] * len(header)

    n_probes = len(probe_outcomes)
    n_probes_ok = sum(1 for o in probe_outcomes if o["status"] == "ok")
    coverage_incomplete = n_probes > 0 and n_probes_ok < n_probes

    lines: list[str] = []
    lines.append("# Validator Health Status")
    lines.append("")
    lines.append(f"_Last updated: **{ts}**  ·  netuid={netuid}  ·  block={current_block:,}_")
    if coverage_incomplete:
        # Visible banner above the table — a stale row can otherwise look
        # like a registry-only entry when really we just couldn't reach
        # the operator's daemon during this run. The "Probe diagnostics"
        # section below this table lists each failure with its reason.
        lines.append("")
        lines.append(
            f"> ⚠️ **Coverage incomplete this run**: {n_probes_ok}/{n_probes} "
            f"`/identity` probes succeeded. Validator rows without a hotkey "
            f"below may be unreachable rather than genuinely registry-only "
            f"(no axon served) — see the Probe diagnostics section."
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
            _fmt_seconds_ago(s.last_update_seconds_ago),
            _fmt_weight_source(s.weight_source) if s.uid is not None else "—",
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
        "failed this run._"
    )
    lines.append("")
    lines.append("## Active alerts")
    lines.append("")
    if not findings:
        lines.append("_None._")
    else:
        for f in findings:
            kind = {"stale_weights": "Stale weights", "low_trust": "Low Yuma trust"}.get(
                f["type"], f["type"],
            )
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
            outcome = "✅ ok" if o["status"] == "ok" else f"❌ {o['error']}"
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
        f"`PROBE_TIMEOUT_SECONDS={PROBE_TIMEOUT_SECONDS}`._"
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

    identity_map, probe_outcomes, health_by_uid = asyncio.run(
        discover_identity_map(metagraph)
    )
    total_axons = sum(
        1 for ax in metagraph.axons if ax.ip != "0.0.0.0" and ax.port != 0
    )
    print(
        f"[info] identity-mapped {len(identity_map)} of {total_axons} "
        f"axon-serving UIDs; /health-reachable on {len(health_by_uid)}",
        file=sys.stderr,
    )

    statuses = build_statuses(
        registered_evms=registered,
        identity_map=identity_map,
        health_by_uid=health_by_uid,
        metagraph=metagraph,
        current_block=current_block,
        all_chain_names=[r.name for r in registries],
    )

    findings = detect_findings(statuses)

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
