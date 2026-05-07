# Example IntentSolver

Minimal working solver for the Minotaur subnet (SN112).

## How it works

This solver wraps the baseline `BaselineSwapSolver` from the SDK, which builds Uniswap V3 single-hop swap execution plans. Override `generate_plan()` and `check_trigger()` with your own strategies to improve scores.

## Local testing

```bash
python -m minotaur_subnet.harness.runner solver.py
```

## Submission

1. Fork this template and implement your strategy
2. Push to a public git repository
3. Submit via the validator API:

```bash
curl -X POST https://validator-url/v1/submissions \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/your-user/your-solver",
    "commit_hash": "abc123...",
    "epoch": 42,
    "hotkey": "5Gxyz..."
  }'
```

## Key files

- `solver.py` — Your IntentSolver implementation (MUST export `SOLVER_CLASS`)
- `Dockerfile` — MUST use `FROM ghcr.io/subnet112/solver-base:v1`
- `requirements.txt` — Additional pip dependencies
- `README.md` — Description of your approach (required)
