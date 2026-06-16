"""LLM-based code generation for App Intents.

When an agent creates an App Intent without providing JS/Solidity code,
this module generates both artifacts from the intent description using
an LLM (Anthropic Claude by default, OpenAI as alternative).

Public functions:
  - generate_scoring_js(name, intent_type, description, supported_chains) -> str
  - generate_solidity(name, intent_type, description, supported_chains) -> str

Configuration via environment variables:
  - LLM_PROVIDER: "anthropic" (default) or "openai"
  - ANTHROPIC_API_KEY / OPENAI_API_KEY: API key for the chosen provider
"""

from __future__ import annotations

import os
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent / "templates"
# Vendored copies of the canonical platform contracts from
# subnet112/minotaur_contracts. Used as reference text in the LLM prompt
# (NOT compiled — they're just shown to the LLM so it sees the real
# inheritance contract). Refresh by re-copying from minotaur_contracts/src/.
_INTERFACE_DIR = _TEMPLATE_DIR / "contracts"


def _read_file(path: Path) -> str:
    """Read a file, returning empty string if missing."""
    if path.exists():
        return path.read_text()
    return ""


# ─── reference materials loaded once ─────────────────────────────────────────

_JS_EXAMPLE: str | None = None
_SOL_EXAMPLE: str | None = None
_IAPPINTENT_SOL: str | None = None
_APPINTENTBASE_SOL: str | None = None


def _get_js_example() -> str:
    global _JS_EXAMPLE
    if _JS_EXAMPLE is None:
        _JS_EXAMPLE = _read_file(_TEMPLATE_DIR / "swap_intent.js")
    return _JS_EXAMPLE


def _get_sol_example() -> str:
    global _SOL_EXAMPLE
    if _SOL_EXAMPLE is None:
        _SOL_EXAMPLE = _read_file(_TEMPLATE_DIR / "swap_intent.sol")
    return _SOL_EXAMPLE


def _get_iappintent_sol() -> str:
    global _IAPPINTENT_SOL
    if _IAPPINTENT_SOL is None:
        _IAPPINTENT_SOL = _read_file(_INTERFACE_DIR / "IAppIntentBase.sol")
    return _IAPPINTENT_SOL


def _get_appintentbase_sol() -> str:
    global _APPINTENTBASE_SOL
    if _APPINTENTBASE_SOL is None:
        _APPINTENTBASE_SOL = _read_file(_INTERFACE_DIR / "AppIntentBase.sol")
    return _APPINTENTBASE_SOL


# ─── prompt construction ─────────────────────────────────────────────────────

def _js_system_prompt() -> str:
    return """\
You are a code generator for the Minotaur App Intents platform. You generate \
JavaScript scoring modules that run on validators inside a sandboxed environment.

The JS module MUST export three async functions via module.exports:

1. validate(plan, state, context) -> { valid: bool, reason?: string }
   - Check structural validity of the execution plan
   - plan.interactions is an array of { target, value, callData }
   - plan.deadline is a unix timestamp
   - state.contractAddress, state.chainId, state.nonce, state.owner
   - app params live in state.typed_context (preferred) or state.raw_params
   - harness/runtime control metadata lives in state.control
   - context.timestamp is the current unix timestamp

2. score(plan, state, context) -> { score: number (0-1), breakdown: {}, metadata?: {} }
   - Score the execution plan on a 0-1 scale
   - Higher is better

3. shouldTrigger(state, context) -> bool
   - For auto-triggered intents, return true when conditions warrant execution
   - For user-triggered intents, return false

The context object has a simulation field:
  context.simulation = {
    success: bool,
    gasUsed (or gas_used): number,
    tokenTransfers (or token_transfers): [{token, from_addr, to_addr, amount}]
  }

Use this pattern to get simulation data:
  const simulation = context.simulation || await context.simulator.simulate(plan);

IMPORTANT:
- Export via: module.exports = { validate, score, shouldTrigger };
- All three functions must be async
- score() must return a number between 0 and 1
- Use both camelCase and snake_case fallbacks for simulation fields (e.g. sim.gasUsed || sim.gas_used)
- Return only the JavaScript code, no markdown fences or explanation"""


def _sol_system_prompt() -> str:
    return """\
You are a code generator for the Minotaur App Intents platform. You generate \
Solidity contracts that serve as the on-chain safety backstop for App Intents.

The contract MUST:
1. Inherit from AppIntentBase
2. Implement score() - basic on-chain validation (the real scoring is JS)
3. Implement _execute() - execute the solver's interaction plan
4. Implement intentType() - return the intent type string
5. Use Solidity ^0.8.20

The on-chain score() is a SAFETY NET only. It checks structural validity and \
returns a pass/fail in BPS (0-10000). The sophisticated scoring happens in the \
JS layer on validators.

IMPORTANT:
- Return only the Solidity code, no markdown fences or explanation
- Import AppIntentBase from "minotaur_contracts/src/AppIntentBase.sol"
  (resolved via the apps repo's foundry remappings — see
  github.com/subnet112/minotaur-apps for the full layout)
- Use SafeERC20 for token transfers
- Include proper error types and events"""


def _build_js_prompt(
    name: str,
    intent_type: str,
    description: str,
    supported_chains: list[int],
) -> str:
    example = _get_js_example()
    example_section = ""
    if example:
        example_section = f"\n\nHere is a reference example (swap intent):\n\n{example}"

    return f"""\
Generate a JavaScript scoring module for the following App Intent:

Name: {name}
Intent Type: {intent_type}
Description: {description}
Supported Chains: {supported_chains}
{example_section}

Generate a complete, production-quality JS scoring module for this "{intent_type}" intent. \
The scoring logic should be specific to the described use case."""


def _build_sol_prompt(
    name: str,
    intent_type: str,
    description: str,
    supported_chains: list[int],
) -> str:
    iappintent = _get_iappintent_sol()
    appintentbase = _get_appintentbase_sol()
    example = _get_sol_example()

    interface_section = ""
    if iappintent:
        interface_section += f"\n\nIAppIntent interface:\n\n{iappintent}"
    if appintentbase:
        interface_section += f"\n\nAppIntentBase base contract:\n\n{appintentbase}"

    example_section = ""
    if example:
        example_section = f"\n\nReference example (SwapIntent):\n\n{example}"

    return f"""\
Generate a Solidity contract for the following App Intent:

Name: {name}
Intent Type: {intent_type}
Description: {description}
Supported Chains: {supported_chains}
{interface_section}
{example_section}

Generate a complete Solidity contract that inherits from AppIntentBase and \
implements the on-chain safety checks for this "{intent_type}" intent."""


# ─── LLM call ────────────────────────────────────────────────────────────────

def _get_provider() -> str:
    return os.environ.get("LLM_PROVIDER", "anthropic").lower()


def _call_llm(system: str, user: str) -> str:
    """Call the configured LLM provider and return the response text."""
    provider = _get_provider()

    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY environment variable is required for code generation. "
                "Either provide js_code and solidity_code directly, or set ANTHROPIC_API_KEY."
            )
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text

    elif provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY environment variable is required for code generation. "
                "Either provide js_code and solidity_code directly, or set OPENAI_API_KEY."
            )
        import openai

        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=4096,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content

    else:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER: {provider!r}. Supported: 'anthropic', 'openai'."
        )


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if the LLM included them despite instructions."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


# ─── public API ──────────────────────────────────────────────────────────────

def generate_scoring_js(
    name: str,
    intent_type: str,
    description: str,
    supported_chains: list[int],
) -> str:
    """Generate JavaScript scoring code for an App Intent via LLM.

    Raises RuntimeError if no API key is configured.
    """
    system = _js_system_prompt()
    user = _build_js_prompt(name, intent_type, description, supported_chains)
    raw = _call_llm(system, user)
    return _strip_code_fences(raw)


def generate_solidity(
    name: str,
    intent_type: str,
    description: str,
    supported_chains: list[int],
) -> str:
    """Generate Solidity contract code for an App Intent via LLM.

    Raises RuntimeError if no API key is configured.
    """
    system = _sol_system_prompt()
    user = _build_sol_prompt(name, intent_type, description, supported_chains)
    raw = _call_llm(system, user)
    return _strip_code_fences(raw)
