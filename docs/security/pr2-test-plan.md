# PR-2 fail-closed gate test plan

Run against a fresh `make testnet-up` with `RELAYER_URL`, `ADMIN_API_KEY`,
and `SOLVER_ROUND_INTERNAL_API_KEY` all set in compose env (the PR-1
defaults). Every command below MUST come from an anonymous client (no
admin header).

```bash
# C1 — /apps/{id}/deploy gated when RELAYER_URL set (was: open if ADMIN_API_KEY empty)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/v1/apps/foo/deploy   # expect 401
# H3 — /apps/validate gated + per-IP rate-limited
for i in $(seq 1 8); do curl -s -o /dev/null -w '%{http_code} ' -X POST http://localhost:8080/v1/apps/validate -H 'content-type: application/json' -d '{"js_code":"","solidity_code":""}'; done; echo   # expect 401 401 ... (gate fires before bucket)
# H4 — /apps/{id}/score gated
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/v1/apps/anyid/score -H 'content-type: application/json' -d '{"plan":{},"params":{}}'   # expect 401
# C2 — internal round endpoints fail-closed when key unset (start api without SOLVER_ROUND_INTERNAL_API_KEY)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/v1/solver/round/internal/close -H 'content-type: application/json' -d '{"round_id":"x"}'   # expect 503
# H5 — /v1/apps/{id}/orders refused on follower (ENABLE_SOLVER_ROUND_COORDINATOR=0)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8081/v1/apps/foo/orders -H 'content-type: application/json' -d '{"submitted_by":"0x0","params":{}}'   # expect 404
# H6 — /native-bittensor/sim-swap gated
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/v1/native-bittensor/sim-swap -H 'content-type: application/json' -d '{"origin_netuid":0,"destination_netuid":112,"amount_rao":1}'   # expect 401
# M-wallets — /v1/wallets/ gated
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/v1/wallets/ -H 'content-type: application/json' -d '{"chain_ids":[31337]}'   # expect 401
# M-dry-run — /v1/orders/{id}/dry-run gated
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/v1/orders/nope/dry-run -H 'content-type: application/json' -d '{"interactions":[]}'   # expect 401
# M-orders-leak — GET /orders without reader-sig strips user_signature
curl -s http://localhost:8080/v1/orders | jq '.orders[0].user_signature // "ABSENT"'   # expect "" or "ABSENT"
```
