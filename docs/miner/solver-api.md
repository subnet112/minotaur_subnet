# Solver API Reference

The solver implements the OIF v1 API specification and exposes the following endpoints.

## Base URL

Each solver runs on its own port, starting from `MINER_BASE_PORT`. For example, if `MINER_BASE_PORT=8000` and you run 2 solvers:
- Solver 0: `http://localhost:8000`
- Solver 1: `http://localhost:8001`

## Endpoints

### GET /health

Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "solver_id": "5GxnM...-solver-00",
  "chain": "base",
  "chainId": 8453,
  "engine": "uniswap-v3"
}
```

### POST /quotes

Get swap quotes for available inputs and requested outputs.

**Request:**
```json
{
  "user": "0x01...UserInteropAddress",
  "availableInputs": [
    {
      "asset": "0x01...TokenInteropAddress",
      "amount": "1000000000000000000"
    }
  ],
  "requestedOutputs": [
    {
      "asset": "0x01...TokenInteropAddress",
      "minAmount": "0",
      "receiver": "0x01...UserInteropAddress"
    }
  ]
}
```

**Response:**
```json
{
  "quotes": [
    {
      "quoteId": "unique-quote-id",
      "provider": "solver-id",
      "orders": [],
      "validUntil": 1715000000,
      "eta": 10,
      "details": {
        "availableInputs": [
          { "asset": "0x01...TokenInteropAddress", "amount": "1000000000000000000", "user": "0x01...UserInteropAddress" }
        ],
        "requestedOutputs": [
          { "asset": "0x01...TokenInteropAddress", "amount": "1800000000", "receiver": "0x01...UserInteropAddress" }
        ]
      },
      "settlement": {
        "contractAddress": "0xSettlementAddr...",
        "deadline": 1715000000,
        "nonce": "0x...",
        "callValue": "0",
        "gasEstimate": 150000,
        "interactionsHash": "0x...",
        "permit": {
          "permitType": "standard_approval",
          "permitCall": "0x",
          "amount": "1000000000000000000",
          "deadline": 1715000000
        },
        "executionPlan": {
          "preInteractions": [],
          "interactions": [
            { "target": "0xRouter...", "value": "0", "callData": "0x..." }
          ],
          "postInteractions": []
        }
      }
    }
  ]
}
```

### POST /orders

Submit an order for execution.

**Request:**
```json
{
  "quoteId": "unique-quote-id"
}
```

**Response:**
```json
{
  "status": "success",
  "orderId": "solver-order-id",
  "order": { "...": "..." },
  "message": "Order accepted"
}
```

### GET /orders/<order_id>

Get order status.

**Response:**
```json
{
  "order": {
    "id": "solver-order-id",
    "status": "pending",
    "createdAt": 1715000000,
    "updatedAt": 1715000001,
    "quoteId": "unique-quote-id",
    "inputAmount": { "asset": "0x01...TokenInteropAddress", "amount": "1000000000000000000" },
    "outputAmount": { "asset": "0x01...TokenInteropAddress", "amount": "1800000000" },
    "settlement": { "type": "escrow", "data": { "estimatedGasUnits": 150000 } },
    "fillTransaction": null
  }
}
```

### GET /tokens

List supported tokens.

**Response:**
```json
{
  "networks": {
    "8453": {
      "chain_id": 8453,
      "name": "Base",
      "input_settler": "0xSettlementAddr...",
      "output_settler": "0xSettlementAddr...",
      "tokens": [
        { "address": "0x4200000000000000000000000000000000000006", "symbol": "WETH", "decimals": 18 },
        { "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "symbol": "USDC", "decimals": 6 }
      ]
    }
  }
}
```

## Solver Types

### Uniswap V3 (Mainnet)
- **Type:** `v3` or `uniswap-v3`
- **Chain:** Ethereum mainnet (chain ID: 1)
- **DEX:** Uniswap V3
- **Default:** Yes

### Uniswap V2 (Mainnet)
- **Type:** `v2` or `uniswap-v2`
- **Chain:** Ethereum mainnet (chain ID: 1)
- **DEX:** Uniswap V2

### Uniswap V3 (Base)
- **Type:** `base`, `base-v3`, or `uniswap-v3-base`
- **Chain:** Base (chain ID: 8453)
- **DEX:** Uniswap V3

## Token Discovery

Solvers automatically discover tokens from:
- Uniswap token lists
- CoinGecko token lists
- Uniswap V3 pool scanning (Base only)
- On-chain token metadata queries

The number of tokens advertised can be limited using the `MOCK_SOLVER_TOKEN_LIMIT` environment variable (default: 10000).

## Error Handling

All endpoints return appropriate HTTP status codes:
- `200 OK`: Success
- `400 Bad Request`: Invalid request format
- `500 Internal Server Error`: Server error

Error responses include an `error` field with a description:
```json
{
  "error": "Invalid request: missing required fields"
}
```

See also: [Configuration](./configuration.md), [Troubleshooting](./troubleshooting.md).

