# Miner Quickstart (Current CLI)

This guide reflects the current `minotaur_subnet.miner.main` CLI and submission routes.

## Prerequisites

- Python 3.12+
- Git
- Optional: Docker (required for git-based submission screening on validator/API side)
- Optional: Bittensor wallet hotkey (required for git-based signed submissions)

## 1) Install

```bash
git clone https://github.com/subnet112/minotaur_subnet.git
cd minotaur_subnet
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Start services

Local development usually targets the API service on `:8080`:

```bash
python -m minotaur_subnet.api.server --port 8080
```

Or bring up the full local testnet:

```bash
make testnet-up
```

## 3) Run the agent loop (recommended)

Agent mode discovers active apps, generates strategies, tests them, and submits source code to `/v1/submissions/source`.

```bash
python -m minotaur_subnet.miner.main agent \
  --validator-url http://localhost:8080 \
  --strategy-dir ./strategies \
  --miner-id my-miner-001
```

## 4) Submit a git-based solver

Current CLI subcommand:

```bash
python -m minotaur_subnet.miner.main submit \
  --repo-url https://github.com/youruser/my-solver \
  --commit-hash <commit> \
  --hotkey <wallet-name> \
  --epoch 0 \
  --validator-url http://localhost:8080 \
  --poll
```

Notes:

- `--epoch` is required in practice unless your target exposes `GET /v1/status` for auto-detection.
- The `submit`/`status` CLI defaults are `http://localhost:9100`, but local testnet submissions are typically handled by the API service on `:8080`.

## 5) Optional: direct source submission (local/dev)

You can submit inline solver code directly:

```bash
curl -X POST http://localhost:8080/v1/submissions/source \
  -H "Content-Type: application/json" \
  -d '{
    "solver_source": "from minotaur_subnet.sdk.intent_solver import IntentSolver\nclass S(IntentSolver):\n    def initialize(self, config):\n        pass\n    def generate_plan(self, intent, state, snapshot=None):\n        raise NotImplementedError\n    def metadata(self):\n        from minotaur_subnet.sdk.intent_solver import SolverMetadata\n        return SolverMetadata(name=\"s\", version=\"0.1.0\", author=\"local\")\nSOLVER_CLASS = S",
    "hotkey": "local-miner",
    "epoch": 0,
    "solver_name": "local-dev"
  }'
```

This route skips screening and queues directly into benchmarking.

## 6) Check submission status

```bash
python -m minotaur_subnet.miner.main status \
  --submission-id sub_xxx \
  --validator-url http://localhost:8080
```

Common statuses:

- `queued`
- `screening_stage_1`
- `screening_stage_2`
- `screening_stage_3`
- `benchmarking`
- `scored`
- `adopted`
- `rejected`

## Next steps

- [Configuration](./configuration.md) for full CLI flags
- [Solver API](./solver-api.md) for `IntentSolver` and `Strategy` contracts
- [Custom Solver](./custom-solver.md) for implementation guidance
