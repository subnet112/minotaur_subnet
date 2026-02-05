# Minotaur Subnet - Claude Code Knowledge Base

## Project Overview

Minotaur is **Bittensor Subnet 112** - a distributed DEX aggregator where miners (solvers) compete to find optimal swap routes for user intents. The subnet combines on-chain Bittensor incentives with Docker-based simulation for deterministic validation.

## Directory Structure

```
minotaur_subnet/
├── neurons/                    # Core implementation
│   ├── miner.py               # Miner entry point
│   ├── validator.py           # Validator entry point
│   ├── solver.py              # Uniswap V3 (Ethereum) solver
│   ├── solver_base.py         # Uniswap V3 (Base) solver
│   ├── solver_uv2.py          # Uniswap V2 solver
│   ├── solver_crosschain.py   # Cross-chain solver
│   ├── solver_common.py       # Shared utilities
│   ├── bridges/               # Bridge integrations (CCTP, Across)
│   ├── validation_engine.py   # Core validation pipeline
│   └── simulator.py           # Docker-based simulation
├── app_intents/               # App Intents R&D (NEW)
├── tests/                     # Unit and integration tests
├── docs/                      # Documentation
└── scripts/                   # Setup utilities
```

## App Intents (R&D)

**Location:** `app_intents/`
**Status:** Concept/design phase

App Intents are a novel abstraction layer for intent-based dApp development. Instead of writing execution-focused code, developers define **outcomes and scoring**, letting solvers/AI agents figure out optimal execution.

### Core Value Propositions

1. **Outcome-based verification**: Apps verify results (e.g., "correct token amounts"), not which contracts were called
2. **Obfuscated scoring**: JS scoring logic hidden from solvers - they only receive scores, not why
3. **AI training**: Solvers train models against hidden scoring (black-box optimization)
4. **Dynamic execution**: Plans adapt to market conditions (impossible with static contracts)
5. **Agent deployment**: AI agents define goals, solver network handles execution

### Architecture

Two-layer design with different purposes:

| Layer | Location | Purpose |
|-------|----------|---------|
| **JavaScript** | Validators | Primary scoring (obfuscated), flexible validation, market data access, upgradeable |
| **Solidity** | On-chain | Safety backstop, hard constraints, final rejection authority, signature verification |

**Flow**: JS validation gates on-chain execution. Solvers submit plans → Validators run hidden JS scoring → If approved, validators sign → Solver submits tx with signatures → On-chain verifies sigs + safety threshold.

### Documentation

| File | Description |
|------|-------------|
| `app_intents/slides/app_intents_presentation.html` | **Visual slide deck** (open in browser) |
| `app_intents/slides/app_intents_overview.md` | Text-based slide outline |
| `app_intents/notes/app_intents_concept.md` | Core concept, architecture, execution flow |
| `app_intents/notes/architecture_overview.md` | Minotaur system overview |
| `app_intents/notes/swap_intents.md` | Current swap intent processing |
| `app_intents/notes/solvers.md` | Solver implementations |
| `app_intents/notes/validators.md` | Validation system |
| `app_intents/notes/bittensor_ops.md` | Bittensor integration |

### Prototypes

| File | Description |
|------|-------------|
| `app_intents/prototypes/interfaces/IAppIntent.sol` | Core Solidity interface |
| `app_intents/prototypes/interfaces/AppIntentBase.sol` | Base implementation |
| `app_intents/prototypes/js-sdk/types.ts` | TypeScript SDK types |
| `app_intents/prototypes/examples/LimitOrderIntent.sol` | Example: Limit order contract |
| `app_intents/prototypes/examples/limit-order.intent.ts` | Example: Limit order JS layer |

### Key Design Decisions

- **Scoring**: 1-100 scale, JS score is authoritative, on-chain threshold is safety net
- **Validator quorum**: Not finalized (options: % of validators, stake-weighted, per-app config)
- **JS runtime**: Not finalized (simple scripts to sandboxed V8 isolates)
- **Triggers**: Solvers monitor conditions and initiate execution
- **Security**: User opt-in trust model + on-chain safety constraints
- **Upgradeability**: JS code can be updated by developers

### Solver Incentives

Solvers earn rewards through two mechanisms:
1. **Bittensor TAO emissions** (volume-based): More successful executions = higher weights = more TAO
2. **Per-execution rewards** (optional): Each App Intent can configure its own reward model (user fees, treasury, spread capture, etc.)

### Confidential Execution (Anti-Collusion)

**Problem**: Validators run JS scoring logic - they could share/sell code to solvers.
**Solution**: Trusted Execution Environments (TEEs) - JS runs in hardware enclaves where even the machine operator can't read the code.
- **Primary**: AMD SEV-SNP / Intel TDX confidential VMs (~2-5% overhead, production-ready on cloud)
- **Integration candidate**: Phala dstack (Docker containers in TEEs, has JS/TS SDK, 1,000+ nodes)
- **Defense-in-depth**: TEE + WASM obfuscation + MPC for key parameters + remote attestation
- **Limitation**: TEEs are hardware-enforced, not mathematically perfect (side-channel attacks exist but require physical access or CPU bugs)

### Developer Learning & Anti-Lock-in

**Core principle**: Developers should never be locked into Minotaur.
- Developers see **winning execution plans** (what actually got executed)
- **Built-in analytics** provide execution patterns across market conditions
- Developers can train AI on historical executions, extract optimal logic
- **Graduation path**: Optionally redeploy as static smart contract with best execution built-in
- Minotaur must earn continued participation - no artificial lock-in

### Relationship to Minotaur

App Intents is a **superset** of the current swap system:
- Current swaps become one type of App Intent (MVP)
- Existing validator/solver infrastructure extends to support App Intents
- Bittensor incentives apply to all App Intent types

## Cross-Chain Support

Implemented in `neurons/solver_crosschain.py` with bridge integrations:
- **CCTP**: Circle's protocol for USDC (zero fee, ~15 min)
- **Across**: Multi-token bridging (USDC, USDT, WETH, WBTC, ~0.1% fee, ~2 min)

Bridge code: `neurons/bridges/`

## Testing

```bash
# Unit tests
pytest tests/unit/test_solver_common.py -v
pytest tests/unit/test_bridges.py -v

# Integration tests
pytest tests/integration/test_crosschain_solver.py -v
```

## Common Commands

```bash
# Run solver
python neurons/solver.py --port 8000

# Run miner
python neurons/miner.py --wallet.name <name> --wallet.hotkey <hotkey>

# Run validator
python neurons/validator.py --wallet.name <name> --wallet.hotkey <hotkey>
```
