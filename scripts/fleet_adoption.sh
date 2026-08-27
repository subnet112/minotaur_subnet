#!/usr/bin/env bash
# Fleet adoption check — is every validator running the same code, and do they
# agree on the round anchor?
#
# Run this BEFORE any consensus-static change goes live (a new benchmarked
# chain, a lookback_epochs / pin change, anything folded into
# benchmark_pack_hash). A split value surfaces as PACK_HASH_MISMATCH, and the
# cheapest moment to catch it is before you flip the switch.
#
#   ./scripts/fleet_adoption.sh              # one shot
#   ./scripts/fleet_adoption.sh --watch      # poll until uniform
#   ./scripts/fleet_adoption.sh --expect 813132a
#
# Peers are discovered from the leader's own champion_consensus block, so a
# validator added to the registry is picked up without editing this file.
set -uo pipefail

LEADER=${LEADER:-https://api.minotaursubnet.com}
EXPECT=""; WATCH=0; INTERVAL=${INTERVAL:-60}
while [ $# -gt 0 ]; do
  case "$1" in
    --watch) WATCH=1 ;;
    --expect) EXPECT="${2:-}"; shift ;;
    --interval) INTERVAL="${2:-60}"; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

probe() {
  python3 - "$LEADER" "$EXPECT" <<'PY'
import json, sys, urllib.request

leader_url, expect = sys.argv[1], (sys.argv[2] or "")

def get(url, timeout=12):
    with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
        return json.load(r)

rows, ok = [], True
try:
    lead = get(leader_url)
except Exception as exc:
    print(f"LEADER UNREACHABLE: {exc}"); sys.exit(2)

cc = lead.get("champion_consensus") or {}
rows.append(("leader", leader_url, lead))
for p in cc.get("peer_endpoints") or []:
    # One retry: a peer that answered a minute ago can time out on a single
    # poll, and reporting that as "unreachable" cries wolf.
    for attempt in (1, 2):
        try:
            rows.append((p.get("validator_id", "")[:12], p["url"], get(p["url"], timeout=20)))
            break
        except Exception as exc:
            if attempt == 2:
                rows.append((p.get("validator_id", "")[:12], p["url"], {"_err": str(exc)[:40]}))

print(f"{'node':<14} {'image':<10} {'status':<7} {'anchor':<9} pins")
shas, pinsets, deferring = set(), set(), 0
for name, url, d in rows:
    if "_err" in d:
        print(f"{name:<14} {'-':<10} UNREACHABLE  {d['_err']}"); ok = False; continue
    a = d.get("round_anchor") or {}
    pins = a.get("pins") or {}
    pin_s = ",".join(f"{k}:{pins[k]}" for k in sorted(pins, key=int)) or "-"
    sha = str(d.get("image_sha"))
    shas.add(sha); pinsets.add(",".join(sorted(pins)))
    if not pins: deferring += 1
    print(f"{name:<14} {sha:<10} {str(d.get('status')):<7} {str(a.get('status')):<9} {pin_s}")
    if d.get("status") != "ok":
        ok = False

print()
# NOT an equality test. The leader tracks :latest (built from develop); the
# followers track :stable (built from main), so their image_sha DIFFER BY
# DESIGN even when the code is identical — main's sha is the merge commit that
# CONTAINS develop's. Comparing shas directly reports a permanent false split.
# What matters is whether each node's build CONTAINS the commit you care about,
# which is what --contains answers (needs a local clone with both refs).
print(f"IMAGE:  {', '.join(sorted(shas))}"
      + ("  (leader :latest / followers :stable differ by design)"
         if len(shas) > 1 else "  (uniform)"))
# A DEFERRING node contributes an EMPTY pin set. That is abstention, not
# disagreement: it derived no pins, so it signs nothing and cannot pull anyone
# else's pack hash apart. Comparing raw sets made "one node is deferring" print
# the same "pack-hash hazard" as "two nodes pinned DIFFERENT chains", which is
# the one case that actually threatens consensus. Judge only the nodes that
# actually produced pins, and report deferral separately.
pinning = {p for p in pinsets if p}
if len(pinning) > 1:
    print(f"CHAINS: SPLIT pin sets {sorted(pinning)}  <-- pack-hash hazard"); ok = False
elif pinning:
    print(f"CHAINS: uniform pin set ({pinning.pop()})"
          + (f"  [{deferring} node(s) deferring — abstaining, not disagreeing]"
             if deferring else ""))
else:
    print("CHAINS: no node derived pins"); ok = False
# NOTE quorum_required=1 means the leader self-quorums for CHAMPION
# certification and followers adopt its signed champion. That is a separate
# question from code uniformity: a consensus-static constant must still match
# everywhere, or the fleet disagrees the moment quorum is ever raised.
print(f"QUORUM: {cc.get('quorum_required')} of {cc.get('validator_count')} registered")
if expect:
    # Ask git whether each node's build contains the commit, rather than whether
    # it equals it. That is the real question: "has everyone adopted #1669?"
    import subprocess
    def _git(*args, timeout=60):
        return subprocess.run(["git", *args], capture_output=True, timeout=timeout)

    def contains(sha):
        """True / False / None(unknown).

        MUST distinguish "not contained" from "I do not have that commit
        locally" — `git merge-base --is-ancestor` exits non-zero for BOTH, and
        collapsing them reports a follower that HAS adopted as one that has
        not. That false negative reads as "keep waiting" forever, and teaches
        you to ignore the check, which is worse than not having it.
        """
        try:
            if _git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
                _git("fetch", "--quiet", "origin", "main", "develop")   # try once
                if _git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
                    return None
            if _git("cat-file", "-e", f"{expect}^{{commit}}").returncode != 0:
                return None
            return _git("merge-base", "--is-ancestor", expect, sha, timeout=15).returncode == 0
        except Exception:
            return None
    print(f"EXPECT: every build contains {expect}")
    for n, _u, d in rows:
        if "_err" in d:
            print(f"  {n:<14} unknown (unreachable)"); ok = False; continue
        sha = str(d.get("image_sha"))
        c = contains(sha)
        mark = {True: "yes", False: "NO",
                None: "UNKNOWN — commit not in this clone, cannot judge"}[c]
        print(f"  {n:<14} {sha:<10} {mark}")
        if c is not True:
            ok = False
sys.exit(0 if ok else 1)
PY
}

if [ "$WATCH" = 1 ]; then
  while true; do
    echo "── $(date -u +%H:%M:%SZ) ──"
    probe && { echo "FLEET UNIFORM"; exit 0; }
    sleep "$INTERVAL"
  done
else
  probe
fi
