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
  "availableInputs": [
    {
      "token": "0x4200000000000000000000000000000000000006",
      "amount": "1000000000000000000"
    }
  ],
  "requestedOutputs": [
    {
      "token": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
      "amount": null
    }
  ]
}
```

**Response:**
```json
{
  "quotes": [
    {
      "input": {
        "token": "0x4200000000000000000000000000000000000006",
        "amount": "1000000000000000000"
      },
      "output": {
        "token": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "amount": "1800000000"
      },
      "plan": {
        "interactions": [...],
        "interactionsHash": "0x..."
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
  "orderId": "order-123",
  "input": {
    "token": "0x4200000000000000000000000000000000000006",
    "amount": "1000000000000000000"
  },
  "output": {
    "token": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "amount": "1800000000"
  },
  "plan": {
    "interactions": [...],
    "interactionsHash": "0x..."
  }
}
```

**Response:**
```json
{
  "orderId": "order-123",
  "status": "pending"
}
```

### GET /orders/<order_id>

Get order status.

**Response:**
```json
{
  "orderId": "order-123",
  "status": "completed",
  "txHash": "0x..."
}
```

### GET /tokens

List supported tokens.

**Response:**
```json
{
  "tokens": [
    {
      "address": "0x4200000000000000000000000000000000000006",
      "symbol": "WETH",
      "decimals": 18
    },
    {
      "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
      "symbol": "USDC",
      "decimals": 6
    }
  ]
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

