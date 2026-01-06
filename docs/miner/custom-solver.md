# Writing Your Own Solver

This guide explains how to create a custom solver and integrate it with the Minotaur miner.

## Overview

A solver is a service that provides swap quotes and executes orders. The miner manages one or more solver instances and registers them with the aggregator. To create a custom solver, you need to:

1. Create a solver class that implements the OIF v1 API
2. Register your solver class with the miner
3. Implement the required Flask routes
4. Handle quote requests and order execution

## Solver Interface

Your solver class must implement the following interface:

### Constructor

```python
def __init__(
    self,
    solver_id: str,
    port: int,
    latency_ms: int = 100,
    quality: float = 1.0,
    logger=None
):
```

**Parameters:**
- `solver_id`: Unique identifier for this solver instance
- `port`: Port number to run the Flask server on
- `latency_ms`: Simulated latency in milliseconds (optional)
- `quality`: Quality multiplier for quotes (optional)
- `logger`: Logger instance (optional)

### Required Methods

#### `setup_routes()`
Sets up Flask routes for the OIF v1 API endpoints. This method should be called in `__init__`.

#### `run(debug=False)`
Starts the Flask server. This method should run the Flask app on the configured port.

### Required Flask Routes

Your solver must implement the following OIF v1 API endpoints:

#### `GET /health`
Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "solver_id": "your-solver-id",
  "chain": "ethereum",
  "chainId": 1,
  "engine": "your-engine-name"
}
```

#### `POST /quotes`
Get swap quotes for available inputs and requested outputs.

**Request:**
```json
{
  "user": "0x01...InteropAddress",
  "availableInputs": [
    {
      "asset": "0x01...InteropAddress",
      "amount": "1000000000000000000"
    }
  ],
  "requestedOutputs": [
    {
      "asset": "0x01...InteropAddress",
      "minAmount": "0",
      "receiver": "0x01...InteropAddress"
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
      "provider": "your-solver-id",
      "orders": [],
      "validUntil": 1715000000,
      "eta": 10,
      "details": {
        "availableInputs": [
          { "asset": "0x01...InteropAddress", "amount": "1000000000000000000", "user": "0x01...InteropAddress" }
        ],
        "requestedOutputs": [
          { "asset": "0x01...InteropAddress", "amount": "1800000000", "receiver": "0x01...InteropAddress" }
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

#### `POST /orders`
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

#### `GET /orders/<order_id>`
Get order status.

**Response:**
```json
{
  "orderId": "order-123",
  "status": "completed",
  "txHash": "0x..."
}
```

#### `GET /tokens`
List supported tokens.

**Response:**
```json
{
  "networks": {
    "1": {
      "chain_id": 1,
      "input_settler": "0xSettlementAddr...",
      "output_settler": "0xSettlementAddr...",
      "tokens": [
        { "address": "0x...", "symbol": "WETH", "decimals": 18 }
      ]
    }
  }
}
```

## Example: Minimal Custom Solver

Here's a minimal example of a custom solver:

```python
from flask import Flask, jsonify, request
import logging
from typing import Dict, List, Any, Optional

class CustomSolver:
    """Minimal custom solver implementation."""
    
    def __init__(
        self,
        solver_id: str,
        port: int,
        latency_ms: int = 100,
        quality: float = 1.0,
        logger=None
    ):
        self.solver_id = solver_id
        self.port = port
        self.latency_ms = latency_ms
        self.quality = quality
        self.logger = logger or logging.getLogger(__name__)
        
        # Initialize Flask app
        self.app = Flask(f"solver-{solver_id}")
        self.setup_routes()
        
        # Store orders
        self.orders: Dict[str, Dict[str, Any]] = {}
        
        # Supported tokens (example)
        self.supported_tokens = [
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
    
    def setup_routes(self):
        """Setup Flask routes for OIF v1 API."""
        
        @self.app.route('/health', methods=['GET'])
        def health():
            return jsonify({
                "status": "healthy",
                "solver_id": self.solver_id,
                "chain": "ethereum",
                "chainId": 1,
                "engine": "custom"
            }), 200
        
        @self.app.route('/quotes', methods=['POST'])
        def get_quotes():
            """Handle quote requests."""
            try:
                req = request.json
                if not req:
                    return jsonify({"error": "Invalid request: empty JSON"}), 400
                
                available_inputs = req.get('availableInputs', [])
                requested_outputs = req.get('requestedOutputs', [])
                
                if not available_inputs or not requested_outputs:
                    return jsonify({"error": "Missing required fields"}), 400
                
                # Generate quotes (simplified example matching this repo's solver schema)
                quotes = []
                for input_entry in available_inputs:
                    for output_entry in requested_outputs:
                        quote_id = f"{self.solver_id}-quote-123"
                        amount_out = str(int(input_entry["amount"]) * self.quality)  # Simplified
                        quotes.append({
                            "quoteId": quote_id,
                            "provider": self.solver_id,
                            "orders": [],
                            "details": {
                                "availableInputs": [{
                                    "asset": input_entry["asset"],
                                    "amount": str(input_entry["amount"]),
                                    "user": input_entry.get("user") or req.get("user"),
                                }],
                                "requestedOutputs": [{
                                    "asset": output_entry["asset"],
                                    "amount": amount_out,
                                    "receiver": output_entry.get("receiver") or input_entry.get("user") or req.get("user"),
                                }]
                            },
                            # NOTE: sample solvers in this repo generally include `settlement`
                            # with `executionPlan`. This minimal example omits it for brevity.
                        })
                
                return jsonify({"quotes": quotes}), 200
                
            except Exception as e:
                self.logger.error(f"Error processing quote request: {e}")
                return jsonify({"error": str(e)}), 500
        
        @self.app.route('/orders', methods=['POST'])
        def submit_order():
            """Handle order submission."""
            try:
                req = request.json
                if not req:
                    return jsonify({"error": "Invalid request"}), 400
                
                quote_id = req.get('quoteId')
                if not quote_id:
                    return jsonify({"error": "Missing quoteId"}), 400
                order_id = f"{self.solver_id}-order-123"
                
                # Store order
                self.orders[order_id] = {
                    "orderId": order_id,
                    "status": "pending",
                    **req
                }
                
                # Execute order (your logic here)
                # ...
                
                return jsonify({
                    "orderId": order_id,
                    "status": "pending"
                }), 200
                
            except Exception as e:
                self.logger.error(f"Error processing order: {e}")
                return jsonify({"error": str(e)}), 500
        
        @self.app.route('/orders/<order_id>', methods=['GET'])
        def get_order(order_id: str):
            """Get order status."""
            if order_id not in self.orders:
                return jsonify({"error": "Order not found"}), 404
            
            order = self.orders[order_id]
            return jsonify(order), 200
        
        @self.app.route('/tokens', methods=['GET'])
        def get_tokens():
            """List supported tokens."""
            return jsonify({
                "networks": {
                    "1": {
                        "chain_id": 1,
                        "input_settler": "0x0000000000000000000000000000000000000000",
                        "output_settler": "0x0000000000000000000000000000000000000000",
                        "tokens": self.supported_tokens
                    }
                }
            }), 200
    
    def run(self, debug=False):
        """Start the Flask server."""
        self.logger.info(f"Starting solver {self.solver_id} on port {self.port}")
        self.app.run(host='0.0.0.0', port=self.port, debug=debug, use_reloader=False)
```

## Registering Your Solver

To use your custom solver with the miner, you need to register it in the `SOLVER_TYPES` dictionary in `neurons/miner.py`:

```python
from .your_custom_solver import CustomSolver

SOLVER_TYPES = {
    "v3": SolverV3,
    "uniswap-v3": SolverV3,
    "v2": SolverV2,
    "uniswap-v2": SolverV2,
    "base": SolverBase,
    "base-v3": SolverBase,
    "uniswap-v3-base": SolverBase,
    "custom": CustomSolver,  # Add your solver here
    "my-solver": CustomSolver,  # You can add multiple aliases
}
```

Then use it with:

```bash
python -m neurons.miner \
  --miner.solver_type custom \
  --miner.id my-miner \
  --aggregator.url http://localhost:4000 \
  --miner.api_key your-api-key
```

## Advanced Example: DEX Integration

Here's a more complete example that integrates with a DEX:

```python
from flask import Flask, jsonify, request
from web3 import Web3
from typing import Dict, List, Any, Optional
import logging
import os

class DEXSolver:
    """Custom solver with DEX integration."""
    
    def __init__(
        self,
        solver_id: str,
        port: int,
        latency_ms: int = 100,
        quality: float = 1.0,
        logger=None
    ):
        self.solver_id = solver_id
        self.port = port
        self.latency_ms = latency_ms
        self.quality = quality
        self.logger = logger or logging.getLogger(__name__)
        
        # Initialize Web3 connection
        rpc_url = os.getenv("ETHEREUM_RPC_URL", "https://eth.llamarpc.com")
        self.web3 = Web3(Web3.HTTPProvider(rpc_url))
        
        # Initialize DEX contracts
        # self.dex_router = self.web3.eth.contract(...)
        
        # Initialize Flask
        self.app = Flask(f"solver-{solver_id}")
        self.setup_routes()
        
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.supported_tokens = self._load_supported_tokens()
    
    def _load_supported_tokens(self) -> List[Dict[str, Any]]:
        """Load list of supported tokens."""
        # Your token loading logic
        return [
            {"address": "0x...", "symbol": "WETH", "decimals": 18},
            # ...
        ]
    
    def _get_quote(self, token_in: str, token_out: str, amount_in: int) -> Optional[int]:
        """Get quote from DEX."""
        try:
            # Your DEX quote logic here
            # For example, call DEX router contract
            # quote = self.dex_router.functions.getAmountsOut(amount_in, [token_in, token_out]).call()
            # return quote[1]  # Output amount
            
            # Placeholder
            return int(amount_in * 0.95)  # 5% slippage example
        except Exception as e:
            self.logger.error(f"Error getting quote: {e}")
            return None
    
    def setup_routes(self):
        """Setup Flask routes."""
        
        @self.app.route('/health', methods=['GET'])
        def health():
            return jsonify({
                "status": "healthy",
                "solver_id": self.solver_id,
                "chain": "ethereum",
                "chainId": 1,
                "engine": "dex-custom"
            }), 200
        
        @self.app.route('/quotes', methods=['POST'])
        def get_quotes():
            """Handle quote requests."""
            try:
                req = request.json
                if not req:
                    return jsonify({"error": "Invalid request"}), 400
                
                available_inputs = req.get('availableInputs', [])
                requested_outputs = req.get('requestedOutputs', [])
                
                if not available_inputs or not requested_outputs:
                    return jsonify({"error": "Missing required fields"}), 400
                
                quotes = []
                for input_token in available_inputs:
                    for output_token in requested_outputs:
                        amount_in = int(input_token["amount"])
                        amount_out = self._get_quote(
                            input_token["asset"],
                            output_token["asset"],
                            amount_in
                        )
                        
                        if amount_out:
                            # Apply quality multiplier
                            amount_out = int(amount_out * self.quality)
                            
                            quote = {
                                "input": input_token,
                                "output": {
                                    "asset": output_token["asset"],
                                    "amount": str(amount_out)
                                },
                                "plan": {
                                    "interactions": self._build_interactions(
                                        input_token["asset"],
                                        output_token["asset"],
                                        amount_in
                                    ),
                                    "interactionsHash": self._compute_interactions_hash(...)
                                }
                            }
                            quotes.append(quote)
                
                return jsonify({"quotes": quotes}), 200
                
            except Exception as e:
                self.logger.error(f"Error processing quote: {e}")
                return jsonify({"error": str(e)}), 500
        
        # ... implement other routes ...
    
    def _build_interactions(self, token_in: str, token_out: str, amount: int) -> List[Dict]:
        """Build interaction plan for swap."""
        # Your interaction building logic
        return []
    
    def _compute_interactions_hash(self, interactions: List[Dict]) -> str:
        """Compute hash of interactions."""
        # Your hashing logic
        return "0x0000000000000000000000000000000000000000000000000000000000000000"
    
    def run(self, debug=False):
        """Start the Flask server."""
        self.logger.info(f"Starting DEX solver {self.solver_id} on port {self.port}")
        self.app.run(host='0.0.0.0', port=self.port, debug=debug, use_reloader=False)
```

## Best Practices

### 1. Error Handling
Always handle errors gracefully and return appropriate HTTP status codes:

```python
try:
    # Your logic
    return jsonify({"quotes": quotes}), 200
except ValueError as e:
    return jsonify({"error": str(e)}), 400
except Exception as e:
    self.logger.error(f"Unexpected error: {e}")
    return jsonify({"error": "Internal server error"}), 500
```

### 2. Logging
Use the provided logger for consistent logging:

```python
if self.logger:
    self.logger.info(f"Processing quote request")
    self.logger.error(f"Error: {e}")
```

### 3. Token Management
Maintain a list of supported tokens and update it regularly:

```python
def _load_supported_tokens(self):
    """Load and cache supported tokens."""
    # Load from token lists, on-chain discovery, etc.
    pass
```

### 4. Quote Caching
Consider caching quotes to reduce computation:

```python
def _get_cached_quote(self, key: str) -> Optional[Dict]:
    """Get cached quote if available."""
    if key in self.quote_cache:
        cached = self.quote_cache[key]
        if time.time() - cached["timestamp"] < 60:  # 60 second cache
            return cached["quote"]
    return None
```

### 5. Rate Limiting
Implement rate limiting if needed to prevent abuse:

```python
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=self.app,
    key_func=get_remote_address,
    default_limits=["100 per minute"]
)

@limiter.limit("10 per second")
@self.app.route('/quotes', methods=['POST'])
def get_quotes():
    # ...
```

### 6. Testing
Test your solver locally before integrating:

```python
if __name__ == "__main__":
    solver = CustomSolver(
        solver_id="test-solver",
        port=8000,
        logger=logging.getLogger()
    )
    solver.run(debug=True)
```

Then test with:

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/quotes \
  -H "Content-Type: application/json" \
  -d '{"availableInputs": [...], "requestedOutputs": [...]}'
```

## Integration Checklist

- [ ] Solver class implements `__init__`, `setup_routes()`, and `run()` methods
- [ ] All required OIF v1 API endpoints are implemented
- [ ] Error handling is implemented for all endpoints
- [ ] Solver is registered in `SOLVER_TYPES` dictionary
- [ ] Solver can be started and responds to health checks
- [ ] Quote requests return valid responses
- [ ] Order submission and status checking work correctly
- [ ] Token list endpoint returns supported tokens
- [ ] Solver integrates correctly with the miner
- [ ] Solver registers successfully with the aggregator

## Next Steps

1. Review existing solver implementations (`neurons/solver.py`, `neurons/solver_base.py`)
2. Implement your custom solver class
3. Register it in `neurons/miner.py`
4. Test locally with the miner
5. Deploy and monitor

See also: [Solver API](./solver-api.md), [Configuration](./configuration.md), [Troubleshooting](./troubleshooting.md).

