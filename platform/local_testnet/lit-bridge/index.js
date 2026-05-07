/**
 * Lit Protocol Bridge — Local Testnet Implementation
 *
 * A lightweight Express service that implements the API contract expected
 * by `minotaur_subnet/wallet/lit_wallet.py` (LitMpcWallet). For local testnet,
 * it uses ethers.js to generate real Ethereum keys that work with Anvil.
 *
 * Production upgrade path: swap the ethers.js backend for
 * `@lit-protocol/lit-node-client` (PKP minting + distributed ECDSA).
 *
 * Endpoints:
 *   POST /wallets          — Create a new wallet
 *   GET  /wallets          — List all wallets
 *   GET  /wallets/:address — Get a single wallet
 *   POST /sign/transaction — Sign a raw transaction
 *   POST /sign/message     — Sign a message (hex-encoded)
 *   GET  /health           — Health check
 */

const express = require("express");
const { ethers } = require("ethers");

const app = express();
app.use(express.json());

const PORT = parseInt(process.env.PORT || "3100", 10);
const ANVIL_RPC_URL = process.env.ANVIL_RPC_URL || "http://anvil:8545";
const BASE_RPC_URL = process.env.BASE_RPC_URL || "";

// Chain ID → RPC URL mapping for multi-chain transaction signing
const BITTENSOR_EVM_RPC_URL = process.env.BITTENSOR_EVM_RPC_URL || "";

const RPC_BY_CHAIN = {
  1: ANVIL_RPC_URL,      // Ethereum mainnet (Anvil fork)
  31337: ANVIL_RPC_URL,  // Anvil local chain ID
  8453: BASE_RPC_URL || ANVIL_RPC_URL, // Base mainnet (Anvil fork)
  964: BITTENSOR_EVM_RPC_URL,          // Bittensor EVM mainnet
};

// In-memory wallet store: address (lowercase) → { wallet, metadata }
const wallets = new Map();

// Auto-incrementing fake PKP token ID
let nextPkpTokenId = 1000;

// ── POST /wallets — Create a new wallet ────────────────────────────────────

app.post("/wallets", (req, res) => {
  try {
    const { chain_ids = [1] } = req.body || {};

    const wallet = ethers.Wallet.createRandom();
    const address = wallet.address;
    const pkpTokenId = `0x${(nextPkpTokenId++).toString(16)}`;

    const metadata = {
      address,
      pkp_token_id: pkpTokenId,
      public_key: wallet.signingKey.publicKey,
      chain_ids,
      created_at: Date.now() / 1000,
    };

    wallets.set(address.toLowerCase(), { wallet, metadata });

    console.log(`[wallet] Created ${address} (chains: ${chain_ids})`);
    res.json(metadata);
  } catch (err) {
    console.error("[wallet] Create error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── GET /wallets — List all wallets ────────────────────────────────────────

app.get("/wallets", (_req, res) => {
  const list = [];
  for (const { metadata } of wallets.values()) {
    list.push(metadata);
  }
  res.json({ wallets: list, total: list.length });
});

// ── GET /wallets/:address — Get a single wallet ───────────────────────────

app.get("/wallets/:address", (req, res) => {
  const entry = wallets.get(req.params.address.toLowerCase());
  if (!entry) {
    return res.status(404).json({ error: "Wallet not found" });
  }
  res.json(entry.metadata);
});

// ── POST /sign/transaction — Sign a transaction ───────────────────────────

app.post("/sign/transaction", async (req, res) => {
  try {
    const { address, transaction, chain_id = 1 } = req.body;

    if (!address || !transaction) {
      return res.status(400).json({ error: "address and transaction required" });
    }

    const entry = wallets.get(address.toLowerCase());
    if (!entry) {
      return res.status(404).json({ error: `Wallet not found: ${address}` });
    }

    // Connect to the correct chain's Anvil fork for nonce/gas
    const rpcUrl = RPC_BY_CHAIN[chain_id] || ANVIL_RPC_URL;
    const provider = new ethers.JsonRpcProvider(rpcUrl);
    const connectedWallet = entry.wallet.connect(provider);

    const tx = {
      to: transaction.to,
      value: transaction.value || "0x0",
      data: transaction.data || "0x",
      chainId: chain_id,
    };

    // Let ethers fill in nonce, gasLimit, etc.
    if (transaction.nonce !== undefined) tx.nonce = transaction.nonce;
    if (transaction.gasLimit) tx.gasLimit = transaction.gasLimit;
    if (transaction.gasPrice) tx.gasPrice = transaction.gasPrice;
    if (transaction.maxFeePerGas) tx.maxFeePerGas = transaction.maxFeePerGas;
    if (transaction.maxPriorityFeePerGas)
      tx.maxPriorityFeePerGas = transaction.maxPriorityFeePerGas;

    const signedTx = await connectedWallet.signTransaction(tx);

    console.log(`[sign] Transaction signed by ${address}`);
    res.json({ signed_tx: signedTx, address });
  } catch (err) {
    console.error("[sign] Transaction error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── POST /sign/message — Sign a message ───────────────────────────────────

app.post("/sign/message", async (req, res) => {
  try {
    const { address, message_hex } = req.body;

    if (!address || !message_hex) {
      return res.status(400).json({ error: "address and message_hex required" });
    }

    const entry = wallets.get(address.toLowerCase());
    if (!entry) {
      return res.status(404).json({ error: `Wallet not found: ${address}` });
    }

    // message_hex is hex-encoded bytes
    const messageBytes = ethers.getBytes("0x" + message_hex.replace(/^0x/, ""));
    const signature = await entry.wallet.signMessage(messageBytes);

    console.log(`[sign] Message signed by ${address}`);
    res.json({ signature, address });
  } catch (err) {
    console.error("[sign] Message error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── POST /sign/hash — Sign a raw hash (EIP-712 digest) ───────────────────

app.post("/sign/hash", async (req, res) => {
  try {
    const { address, hash_hex } = req.body;

    if (!address || !hash_hex) {
      return res.status(400).json({ error: "address and hash_hex required" });
    }

    const entry = wallets.get(address.toLowerCase());
    if (!entry) {
      return res.status(404).json({ error: `Wallet not found: ${address}` });
    }

    // Raw ECDSA sign (NOT signMessage which adds EIP-191 prefix)
    const digest = ethers.getBytes("0x" + hash_hex.replace(/^0x/, ""));
    const sig = entry.wallet.signingKey.sign(digest);

    console.log(`[sign] Hash signed by ${address}`);
    res.json({ signature: sig.serialized, address });
  } catch (err) {
    console.error("[sign] Hash error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── GET /health ────────────────────────────────────────────────────────────

app.get("/health", async (_req, res) => {
  let connected = false;
  try {
    const provider = new ethers.JsonRpcProvider(ANVIL_RPC_URL);
    const blockNumber = await provider.getBlockNumber();
    connected = blockNumber >= 0;
  } catch {
    // Anvil not reachable — still healthy (bridge itself is fine)
  }

  res.json({
    status: "ok",
    lit_network: "local_testnet",
    connected,
    wallets_count: wallets.size,
  });
});

// ── Start ──────────────────────────────────────────────────────────────────

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Lit bridge listening on port ${PORT}`);
  console.log(`Anvil RPC (ETH): ${ANVIL_RPC_URL}`);
  if (BASE_RPC_URL) {
    console.log(`Anvil RPC (Base): ${BASE_RPC_URL}`);
  }
});
