/**
 * App Intent JS Scoring Runner
 *
 * Executes JS scoring functions in a sandboxed VM context.
 * Communicates with the Python JsExecutionEngine via stdin/stdout (JSON).
 *
 * Protocol:
 *   stdin  -> { jsCode: string, functionName: string, args: any[] }
 *   stdout <- { success: true, result: any }
 *           | { success: false, error: string, errorType: string }
 */

"use strict";

const vm = require("vm");
const http = require("http");
const https = require("https");

// ── Network helpers for JS scoring code ─────────────────────────────────────
// These are injected into the sandbox so scoring code can independently
// verify on-chain state and fetch external data. All calls are read-only
// and subject to timeouts + domain restrictions.

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

/**
 * Build a minimal sandbox context. No access to Node built-ins
 * (require, process, fs, network, etc.). Only safe globals are exposed.
 */
function buildSandbox(contextExtras) {
    const sandbox = {
        // Safe JS built-ins
        console: {
            log: (...args) => {
                // Redirect to stderr so it doesn't pollute the JSON result on stdout
                process.stderr.write("[js:log] " + args.map(String).join(" ") + "\n");
            },
            warn: (...args) => {
                process.stderr.write("[js:warn] " + args.map(String).join(" ") + "\n");
            },
            error: (...args) => {
                process.stderr.write("[js:error] " + args.map(String).join(" ") + "\n");
            },
            info: (...args) => {
                process.stderr.write("[js:info] " + args.map(String).join(" ") + "\n");
            },
            debug: (...args) => {
                process.stderr.write("[js:debug] " + args.map(String).join(" ") + "\n");
            },
        },
        // Math and JSON are useful for scoring
        Math,
        JSON,
        // Timing helpers that scoring code might use
        Date,
        parseInt,
        parseFloat,
        isNaN,
        isFinite,
        Number,
        String,
        Boolean,
        Array,
        Object,
        Map,
        Set,
        Promise,
        Error,
        TypeError,
        RangeError,
        // Module system stub - the scoring code uses module.exports
        module: { exports: {} },
        exports: {},
    };

    // Network helpers — controlled RPC and HTTP access for scoring code.
    // These run outside the VM sandbox (in the Node.js host process) but
    // are injected as callable functions the scoring code can await.
    sandbox.ethCall = ethCall;
    sandbox.ethBlockNumber = ethBlockNumber;
    sandbox.httpGet = httpGet;

    // Merge any extra context
    if (contextExtras) {
        Object.assign(sandbox, contextExtras);
    }

    return sandbox;
}

/**
 * Execute JS code in the sandbox, call a function by name, and return the result.
 */
async function execute(jsCode, functionName, args) {
    const sandbox = buildSandbox();
    const ctx = vm.createContext(sandbox, {
        name: "AppIntentSandbox",
        // Prevent code from breaking out of the sandbox
        codeGeneration: {
            strings: false, // Disallow eval("...")
            wasm: false,    // Disallow WebAssembly compilation
        },
    });

    // Security: freeze all built-in prototypes inside the sandbox to prevent
    // prototype pollution attacks. Malicious scoring code could otherwise
    // modify shared prototypes (e.g., Object.prototype.toString) to escape
    // the sandbox or corrupt host state. This runs on the VM-internal copies,
    // not the host's originals, so the host process is unaffected.
    vm.runInContext(`
        Object.freeze(Object.prototype);
        Object.freeze(Array.prototype);
        Object.freeze(Function.prototype);
        Object.freeze(String.prototype);
        Object.freeze(Number.prototype);
        Object.freeze(Boolean.prototype);
        Object.freeze(Error.prototype);
        Object.freeze(Promise.prototype);
        Object.freeze(RegExp.prototype);
        Object.freeze(Map.prototype);
        Object.freeze(Set.prototype);
    `, ctx);

    // Execute the scoring module code to populate module.exports
    const script = new vm.Script(jsCode, {
        filename: "app_intent_scoring.js",
        timeout: 10000, // 10s compile timeout (execution timeout handled by Python)
    });
    script.runInContext(ctx);

    // Retrieve the exported module
    const mod = sandbox.module.exports;
    if (!mod || typeof mod !== "object") {
        throw new Error("JS code did not set module.exports to an object");
    }

    const fn = mod[functionName];
    if (typeof fn !== "function") {
        const available = Object.keys(mod)
            .filter((k) => typeof mod[k] === "function")
            .join(", ");
        throw new Error(
            `Function "${functionName}" not found in module.exports. ` +
            `Available functions: ${available || "(none)"}`
        );
    }

    // Call the function. It may be sync or async.
    const result = await fn.apply(mod, args);
    return result;
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
        const errorType =
            err.code === "ERR_SCRIPT_EXECUTION_TIMEOUT"
                ? "TimeoutError"
                : err.constructor.name || "RuntimeError";
        const response = {
            success: false,
            error: err.message || String(err),
            errorType,
        };
        process.stdout.write(JSON.stringify(response) + "\n");
        process.exit(1);
    }
}

main();
