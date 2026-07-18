/**
 * App Intent JS Scoring Runner
 *
 * Executes UNTRUSTED JS scoring functions in a real V8 isolate (isolated-vm).
 * Communicates with the Python JsExecutionEngine via stdin/stdout (JSON).
 *
 * Protocol:
 *   stdin  -> { jsCode: string, functionName: string, args: any[] }
 *   stdout <- { success: true, result: any }
 *           | { success: false, error: string, errorType: string }
 */

"use strict";

const ivm = require("isolated-vm");
const http = require("http");
const https = require("https");

// ── Network helpers for JS scoring code ─────────────────────────────────────
// These run in the HOST (trusted) process and are bridged into the isolate as
// ivm.References (see execute()). All calls are read-only and subject to
// timeouts + domain/method allowlists.

const RPC_TIMEOUT_MS = 10000;
const HTTP_TIMEOUT_MS = 10000;
const HTTP_MAX_RESPONSE_BYTES = 1024 * 1024; // 1 MB

// RPC URLs per chain (configured via environment)
const RPC_URLS = {};
if (process.env.ANVIL_RPC_URL) {
    RPC_URLS[1] = process.env.ANVIL_RPC_URL;
    RPC_URLS[31337] = process.env.ANVIL_RPC_URL;
}
if (process.env.BASE_RPC_URL) {
    RPC_URLS[8453] = process.env.BASE_RPC_URL;
}
// Allow explicit per-chain override: RPC_URL_1=http://..., RPC_URL_8453=http://...
for (const [key, val] of Object.entries(process.env)) {
    const match = key.match(/^RPC_URL_(\d+)$/);
    if (match) RPC_URLS[parseInt(match[1])] = val;
}

// HTTP domain allowlist (empty = DENY ALL requests).
// Security: deny-by-default. Only domains explicitly listed in
// JS_SCORING_ALLOWED_DOMAINS are reachable from scoring code.
const HTTP_ALLOWED_DOMAINS = (process.env.JS_SCORING_ALLOWED_DOMAINS || "")
    .split(",")
    .map(d => d.trim().toLowerCase())
    .filter(Boolean);

// SSRF blocklist: always block private/internal network addresses regardless
// of the allowlist. This prevents scoring code from probing internal services,
// cloud metadata endpoints, or Docker-internal hostnames.
const BLOCKED_HOSTS_EXACT = new Set([
    "127.0.0.1", "localhost", "::1",
    // Docker service names used in the local testnet
    "api", "anvil", "anvil-base", "subtensor", "relayer", "lit-bridge",
    // Audit H8: container-escape surfaces and cloud metadata
    "docker-socket-proxy",
    "host.docker.internal",
    "169.254.169.254",   // AWS/GCP/Azure IMDS
    "metadata.google.internal",
    "metadata",
]);
const BLOCKED_HOST_KEYWORDS = ["internal", "local"];
const BLOCKED_IP_PATTERNS = [
    /^10\./,                       // 10.0.0.0/8
    /^172\.(1[6-9]|2\d|3[01])\./,  // 172.16.0.0/12
    /^192\.168\./,                 // 192.168.0.0/16
    /^169\.254\./,                 // link-local + cloud metadata
];

// Audit H8: JSON-RPC method allowlist for ethCall / ethBlockNumber. Cheat
// codes (anvil_*, hardhat_*, evm_*) let scoring code mutate the local
// fork — minting balances, time-travelling, replaying state — which
// trivially breaks score parity between leader and followers. Debug /
// admin / personal namespaces leak operator keys or DOS the node.
const ALLOWED_RPC_METHODS = new Set([
    "eth_call",
    "eth_blockNumber",
    "eth_getBalance",
    "eth_getBlockByNumber",
    "eth_getBlockByHash",
    "eth_getTransactionByHash",
    "eth_getTransactionReceipt",
    "eth_getStorageAt",
    "eth_getCode",
    "eth_chainId",
    "eth_gasPrice",
    "eth_estimateGas",
    "eth_getLogs",
]);
const REJECTED_RPC_PREFIXES = [
    "anvil_", "hardhat_", "evm_",
    "debug_", "trace_", "txpool_",
    "admin_", "personal_", "miner_",
];

function _validateRpcMethod(method) {
    if (typeof method !== "string") return false;
    for (const p of REJECTED_RPC_PREFIXES) {
        if (method.startsWith(p)) return false;
    }
    return ALLOWED_RPC_METHODS.has(method);
}

/**
 * Check if a hostname targets a private/internal network.
 * Returns true if the host should be blocked.
 */
function _isBlockedHost(hostname) {
    const h = hostname.toLowerCase();
    if (BLOCKED_HOSTS_EXACT.has(h)) return true;
    if (BLOCKED_HOST_KEYWORDS.some(kw => h.includes(kw))) return true;
    if (BLOCKED_IP_PATTERNS.some(re => re.test(h))) return true;
    return false;
}

/**
 * Make a JSON-RPC eth_call to a chain. Read-only.
 * Returns the raw hex result string.
 *
 * Note: eth_call is the only method exposed here intentionally — the
 * helper signature is (chainId, to, data, blockTag). For other methods
 * the allowlist in _validateRpcMethod is the enforcement point.
 */
function ethCall(chainId, to, data, blockTag) {
    const rpcUrl = RPC_URLS[chainId];
    if (!rpcUrl) {
        return Promise.reject(new Error(`No RPC URL configured for chain ${chainId}`));
    }
    const method = "eth_call";
    if (!_validateRpcMethod(method)) {
        return Promise.reject(new Error(`RPC method not allowed: ${method}`));
    }
    const payload = JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method,
        params: [{ to, data }, blockTag || "latest"],
    });
    return _httpPost(rpcUrl, payload, RPC_TIMEOUT_MS).then(body => {
        const parsed = JSON.parse(body);
        if (parsed.error) throw new Error(`RPC error: ${parsed.error.message || JSON.stringify(parsed.error)}`);
        return parsed.result;
    });
}

/**
 * Get the current block number for a chain.
 */
function ethBlockNumber(chainId) {
    const rpcUrl = RPC_URLS[chainId];
    if (!rpcUrl) {
        return Promise.reject(new Error(`No RPC URL configured for chain ${chainId}`));
    }
    const method = "eth_blockNumber";
    if (!_validateRpcMethod(method)) {
        return Promise.reject(new Error(`RPC method not allowed: ${method}`));
    }
    const payload = JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method,
        params: [],
    });
    return _httpPost(rpcUrl, payload, RPC_TIMEOUT_MS).then(body => {
        const parsed = JSON.parse(body);
        return parsed.result;
    });
}

/**
 * Fetch a URL via GET. Returns the response body as a string.
 *
 * Security: deny-by-default. When JS_SCORING_ALLOWED_DOMAINS is empty (the
 * default), ALL httpGet calls are blocked. Even when an allowlist is provided,
 * private/internal network addresses are always blocked to prevent SSRF
 * attacks against internal services, cloud metadata endpoints, etc.
 */
function httpGet(url) {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();

    // Always block private/internal network targets (SSRF protection)
    if (_isBlockedHost(host)) {
        return Promise.reject(new Error(`Blocked: requests to internal/private networks are forbidden: ${host}`));
    }

    // Deny-by-default: if no allowlist is configured, block everything
    if (HTTP_ALLOWED_DOMAINS.length === 0) {
        return Promise.reject(new Error(`httpGet blocked: no allowed domains configured (set JS_SCORING_ALLOWED_DOMAINS)`));
    }

    // Check against explicit allowlist
    if (!HTTP_ALLOWED_DOMAINS.some(d => host === d || host.endsWith("." + d))) {
        return Promise.reject(new Error(`Domain not allowed: ${host}`));
    }
    return new Promise((resolve, reject) => {
        const mod = parsed.protocol === "https:" ? https : http;
        const req = mod.get(url, { timeout: HTTP_TIMEOUT_MS }, (res) => {
            if (res.statusCode < 200 || res.statusCode >= 300) {
                reject(new Error(`HTTP ${res.statusCode}: ${url}`));
                res.resume();
                return;
            }
            const chunks = [];
            let totalBytes = 0;
            res.on("data", (chunk) => {
                totalBytes += chunk.length;
                if (totalBytes > HTTP_MAX_RESPONSE_BYTES) {
                    reject(new Error(`Response too large (>${HTTP_MAX_RESPONSE_BYTES} bytes)`));
                    res.destroy();
                    return;
                }
                chunks.push(chunk);
            });
            res.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
            res.on("error", reject);
        });
        req.on("timeout", () => { req.destroy(); reject(new Error("HTTP timeout")); });
        req.on("error", reject);
    });
}

/** Internal: HTTP POST helper for RPC calls.
 *
 * Audit H8: defence-in-depth — even though the only callers are
 * ethCall / ethBlockNumber against operator-configured RPC URLs, apply
 * the same SSRF blocklist and method allowlist that httpGet enforces.
 * Operator-configured RPC URLs are exempt from the host blocklist
 * (otherwise `http://anvil:8545` style local RPCs break); arbitrary
 * URLs are still rejected if they hit internal/private targets.
 */
function _httpPost(url, body, timeoutMs) {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    const isConfiguredRpc = Object.values(RPC_URLS).some(rpc => {
        try {
            return new URL(rpc).hostname.toLowerCase() === host;
        } catch (_) {
            return false;
        }
    });
    if (!isConfiguredRpc && _isBlockedHost(host)) {
        return Promise.reject(new Error(
            `Blocked: requests to internal/private networks are forbidden: ${host}`,
        ));
    }
    // If the body parses as JSON-RPC, enforce the method allowlist.
    try {
        const peek = JSON.parse(body);
        if (peek && typeof peek === "object" && "method" in peek) {
            if (!_validateRpcMethod(peek.method)) {
                return Promise.reject(new Error(
                    `RPC method not allowed: ${peek.method}`,
                ));
            }
        }
    } catch (_) {
        // Non-JSON or non-RPC body — let the caller decide.
    }
    return new Promise((resolve, reject) => {
        const mod = parsed.protocol === "https:" ? https : http;
        const req = mod.request(url, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
            timeout: timeoutMs,
        }, (res) => {
            const chunks = [];
            res.on("data", (chunk) => chunks.push(chunk));
            res.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
            res.on("error", reject);
        });
        req.on("timeout", () => { req.destroy(); reject(new Error("RPC timeout")); });
        req.on("error", reject);
        req.write(body);
        req.end();
    });
}

/**
 * Read all of stdin as a string.
 */
function readStdin() {
    return new Promise((resolve, reject) => {
        const chunks = [];
        process.stdin.setEncoding("utf8");
        process.stdin.on("data", (chunk) => chunks.push(chunk));
        process.stdin.on("end", () => resolve(chunks.join("")));
        process.stdin.on("error", reject);
    });
}

// ── Isolated-VM sandbox ─────────────────────────────────────────────────────
// SECURITY: app scoring JS is UNTRUSTED. Node's built-in `vm` module is
// explicitly NOT a security boundary — a guest escapes `vm.createContext` into
// the host realm via an injected function's `.constructor`
// (`ethCall.constructor('return process')()`) and reaches process.env,
// require('fs'), require('child_process'), and the network (this was exploited,
// incident 2026-07-18). We therefore run the guest in a real V8 isolate
// (isolated-vm): a separate heap with NO host realm — no `require`, no
// `process`, no `Buffer`, no network. The RPC/HTTP host helpers are bridged as
// ivm.References captured inside a closure the guest cannot reach; the guest
// only ever receives structured-clone COPIES, never a host object. All standard
// JS built-ins (Object/Array/Math/JSON/BigInt/Date/Promise/Map/Set/…) are
// present natively in the isolate, so existing scoring code is unaffected.

const MEMORY_LIMIT_MB = 128;
const EVAL_TIMEOUT_MS = 10000;

/**
 * Execute untrusted JS in an isolated-vm isolate, invoke the named export, and
 * return a structured-clone copy of its result. The isolate is always disposed.
 */
async function execute(jsCode, functionName, args) {
    const isolate = new ivm.Isolate({ memoryLimit: MEMORY_LIMIT_MB });
    try {
        const context = await isolate.createContext();
        const jail = context.global;
        // Node-style `global` self-reference (points at the ISOLATE's own global,
        // never the host). `globalThis` already exists natively in the isolate.
        await jail.set("global", jail.derefInto());

        // console.* -> host stderr. The guest passes only a pre-joined STRING to
        // the host writer (copied in); it never holds the host function object.
        await context.evalClosure(
            `globalThis.console = Object.freeze({
                log:   function(){ $0.applyIgnored(undefined, ['[js:log] '   + Array.prototype.map.call(arguments, String).join(' ')], { arguments: { copy: true } }); },
                warn:  function(){ $0.applyIgnored(undefined, ['[js:warn] '  + Array.prototype.map.call(arguments, String).join(' ')], { arguments: { copy: true } }); },
                error: function(){ $0.applyIgnored(undefined, ['[js:error] ' + Array.prototype.map.call(arguments, String).join(' ')], { arguments: { copy: true } }); },
                info:  function(){ $0.applyIgnored(undefined, ['[js:info] '  + Array.prototype.map.call(arguments, String).join(' ')], { arguments: { copy: true } }); },
                debug: function(){ $0.applyIgnored(undefined, ['[js:debug] ' + Array.prototype.map.call(arguments, String).join(' ')], { arguments: { copy: true } }); },
            });`,
            [(line) => { process.stderr.write(String(line) + "\n"); }],
            { arguments: { reference: true } },
        );

        // Bridge the async RPC/HTTP helpers. Each guest function copies its args
        // to the host, awaits the host Promise, and receives a COPIED result.
        // Pass the RAW host fn: `{arguments:{reference:true}}` makes isolated-vm
        // wrap it as the Reference $0 exactly ONCE (passing an already-made
        // ivm.Reference double-wraps it → the guest's $0.apply invokes a
        // Reference-to-a-Reference and throws "Reference is not a function"). $0
        // lives ONLY in this closure's scope — never assigned to a guest-reachable
        // property — so the guest cannot walk `.constructor` back to the host realm.
        const bridge = async (name, fn) => {
            // Wrap the host fn so it ALWAYS RESOLVES with an envelope. A REJECTED
            // host promise escapes isolated-vm's cross-isolate result marshaling as
            // an uncaught host exception (crashing the runner with empty stdout);
            // resolving an {ok:false,error} envelope and re-throwing INSIDE the
            // guest keeps the rejection on the guest side where the scoring code's
            // try/catch can handle it.
            const wrapped = async (...callArgs) => {
                try { return { ok: true, value: await fn(...callArgs) }; }
                catch (e) { return { ok: false, error: (e && e.message) || String(e) }; }
            };
            await context.evalClosure(
                `globalThis[${JSON.stringify(name)}] = function() {
                    return $0.apply(undefined, Array.prototype.slice.call(arguments), {
                        arguments: { copy: true },
                        result: { copy: true, promise: true },
                    }).then(function(env){
                        if (env && env.ok) return env.value;
                        throw new Error((env && env.error) || (${JSON.stringify(name)} + ' failed'));
                    });
                };`,
                [wrapped],
                { arguments: { reference: true } },
            );
        };
        await bridge("ethCall", ethCall);
        await bridge("ethBlockNumber", ethBlockNumber);
        await bridge("httpGet", httpGet);

        // CommonJS shim so existing `module.exports = { ... }` scoring code works.
        await context.eval("var module = { exports: {} }; var exports = module.exports;");

        // Compile + run the untrusted scoring module. The isolate {timeout} bounds
        // SYNCHRONOUS execution (e.g. infinite loops); the overall wall-clock —
        // including async awaits and a never-settling promise — is bounded by the
        // outer asyncio.wait_for + proc.kill in sandbox.py, which is the effective
        // ceiling for the async path.
        const script = await isolate.compileScript(jsCode, {
            filename: "app_intent_scoring.js",
        });
        await script.run(context, { timeout: EVAL_TIMEOUT_MS });

        // Invoke the named export INSIDE the isolate. Args are copied in; the
        // return value (sync or async) is awaited and copied out. A non-clonable
        // result throws inside isolated-vm and surfaces as a normal error.
        const result = await context.evalClosure(
            `const _exports = (typeof module === 'object' && module) ? module.exports : undefined;
             const _fn = _exports && _exports[$0];
             if (typeof _fn !== 'function') {
                 const _avail = _exports
                     ? Object.keys(_exports).filter(function(k){ return typeof _exports[k] === 'function'; }).join(', ')
                     : '';
                 throw new Error('Function "' + $0 + '" not found in module.exports. Available functions: ' + (_avail || '(none)'));
             }
             return Promise.resolve(_fn.apply(_exports, $1));`,
            [functionName, args],
            {
                arguments: { copy: true },
                result: { copy: true, promise: true },
                timeout: EVAL_TIMEOUT_MS,
            },
        );
        return result;
    } finally {
        isolate.dispose();
    }
}

/**
 * Main entry point.
 */
async function main() {
    let input;
    try {
        const raw = await readStdin();
        input = JSON.parse(raw);
    } catch (err) {
        const response = {
            success: false,
            error: `Failed to parse input JSON: ${err.message}`,
            errorType: "InputError",
        };
        process.stdout.write(JSON.stringify(response) + "\n");
        process.exit(1);
    }

    const { jsCode, functionName, args } = input;

    if (typeof jsCode !== "string" || !jsCode.trim()) {
        const response = {
            success: false,
            error: "jsCode must be a non-empty string",
            errorType: "InputError",
        };
        process.stdout.write(JSON.stringify(response) + "\n");
        process.exit(1);
    }

    if (typeof functionName !== "string" || !functionName.trim()) {
        const response = {
            success: false,
            error: "functionName must be a non-empty string",
            errorType: "InputError",
        };
        process.stdout.write(JSON.stringify(response) + "\n");
        process.exit(1);
    }

    try {
        const result = await execute(jsCode, functionName, args || []);
        const response = { success: true, result };
        process.stdout.write(JSON.stringify(response) + "\n");
    } catch (err) {
        const msg = err && err.message ? err.message : String(err);
        // isolated-vm surfaces a timeout as an Error whose message contains
        // "timed out"; the legacy vm path used the ERR_SCRIPT_EXECUTION_TIMEOUT code.
        const isTimeout =
            (err && err.code === "ERR_SCRIPT_EXECUTION_TIMEOUT") ||
            /script execution timed out|timed out/i.test(msg);
        const errorType = isTimeout
            ? "TimeoutError"
            : (err && err.constructor && err.constructor.name) || "RuntimeError";
        const response = {
            success: false,
            error: msg,
            errorType,
        };
        process.stdout.write(JSON.stringify(response) + "\n");
        process.exit(1);
    }
}

main();
