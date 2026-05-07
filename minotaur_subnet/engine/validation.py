"""Pre-flight validation for App Intent JS and Solidity code.

Validates code at create time so errors surface immediately rather than
at scoring time (JS) or deploy time (Solidity).
"""

from __future__ import annotations

import logging
import re

from minotaur_subnet.shared.types import CodeValidationResult

from .sandbox import JsSandbox, JsSandboxError, JsRuntimeError, JsTimeoutError

logger = logging.getLogger(__name__)


def _parse_forge_errors(raw_error: str) -> list[str]:
    """Extract meaningful error/warning lines from forge stderr.

    Strips ANSI escape codes and pulls lines containing Error, Warning,
    or --> (source location pointers).
    """
    # Strip ANSI escape codes
    ansi_pattern = re.compile(r"\x1b\[[0-9;]*m")
    cleaned = ansi_pattern.sub("", raw_error)

    lines = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Keep lines with error/warning indicators or source pointers
        if any(marker in stripped for marker in ("Error", "error", "Warning", "warning", "-->")):
            lines.append(stripped)
    return lines


async def validate_js_code(js_code: str) -> CodeValidationResult:
    """Validate JavaScript scoring code by loading it in a sandbox.

    Checks that the code parses, loads without error, and exports a
    ``score()`` function (required). Warns if ``config`` or ``manifest``
    exports are missing.

    Args:
        js_code: JavaScript module source code.

    Returns:
        CodeValidationResult with errors, warnings, and extracted metadata.
    """
    errors: list[str] = []
    warnings: list[str] = []
    js_config = None
    js_manifest = None
    js_exports: list[str] = []

    sandbox = JsSandbox(timeout_ms=3000, max_memory_mb=64)

    # Evaluate user code then inspect module.exports. A semicolon after user
    # code prevents dangling expressions (e.g. "let x =") from absorbing
    # the introspection wrapper — the semicolon forces a SyntaxError instead.
    introspection_code = js_code + ";\n" + """
// ── introspection wrapper ──
module.exports.__introspect__ = function() {
    var exports = module.exports;
    var keys = Object.keys(exports).filter(function(k) {
        return k !== '__introspect__';
    });
    var has_score = typeof exports.score === 'function';
    var has_validate = typeof exports.validate === 'function';
    var has_config = exports.config != null && typeof exports.config === 'object';
    var has_manifest = exports.manifest != null && typeof exports.manifest === 'object';
    return {
        exports: keys,
        has_score: has_score,
        has_validate: has_validate,
        has_config: has_config,
        has_manifest: has_manifest,
        config: has_config ? exports.config : null,
        manifest: has_manifest ? exports.manifest : null
    };
};
"""

    try:
        result = await sandbox.execute_async(
            introspection_code, "__introspect__", []
        )
    except JsTimeoutError:
        errors.append("JavaScript execution timed out (>3s). Check for infinite loops.")
        return CodeValidationResult(valid=False, errors=errors, warnings=warnings)
    except JsRuntimeError as exc:
        msg = str(exc)
        if msg.startswith("JS "):
            msg = msg[3:]
        errors.append(f"JavaScript error: {msg}")
        return CodeValidationResult(valid=False, errors=errors, warnings=warnings)
    except JsSandboxError as exc:
        errors.append(f"JavaScript loading failed: {exc}")
        return CodeValidationResult(valid=False, errors=errors, warnings=warnings)

    if not isinstance(result, dict):
        errors.append("Introspection returned unexpected result. The JS code may be malformed.")
        return CodeValidationResult(valid=False, errors=errors, warnings=warnings)

    # Extract metadata
    js_exports = result.get("exports", [])
    js_config = result.get("config")
    js_manifest = result.get("manifest")

    # Check required exports
    if not result.get("has_score"):
        errors.append(
            "Missing required export: score(). "
            "The JS module must export a score(plan, state, context) function."
        )

    # Check recommended exports
    if not result.get("has_config"):
        warnings.append(
            "No 'config' export found. "
            "A config export (with name, version) helps with identification."
        )
    if not result.get("has_manifest"):
        warnings.append(
            "No 'manifest' export found. "
            "Without a manifest, MCP tools cannot auto-detect intent functions."
        )

    valid = len(errors) == 0
    return CodeValidationResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        js_config=js_config,
        js_manifest=js_manifest,
        js_exports=js_exports,
    )


def validate_solidity_code(
    solidity_code: str,
    contract_name: str = "",
    cleanup: bool = True,
) -> CodeValidationResult:
    """Validate Solidity code by compiling it with Forge.

    Auto-detects the contract name from source if not provided.
    Reuses the existing ``ForgeCompiler`` pipeline.

    Args:
        solidity_code: Solidity source code.
        contract_name: Contract name override. Auto-detected if empty.
        cleanup: Whether to remove the generated .sol file after compilation.

    Returns:
        CodeValidationResult with errors, warnings, and ABI on success.
    """
    from minotaur_subnet.deployment.compiler import ForgeCompiler

    errors: list[str] = []
    warnings: list[str] = []

    # Auto-detect contract name
    if not contract_name:
        match = re.search(r"contract\s+(\w+)", solidity_code)
        if match:
            contract_name = match.group(1)
        else:
            errors.append(
                "Could not detect contract name from Solidity source. "
                "Ensure the code contains a 'contract <Name>' declaration."
            )
            return CodeValidationResult(valid=False, errors=errors, warnings=warnings)

    compiler = ForgeCompiler()
    gen_dir = compiler.contracts_dir / "src" / "generated"
    sol_path = gen_dir / f"{contract_name}.sol"

    try:
        result = compiler.compile(contract_name, solidity_code)

        if result.error:
            # Parse forge error output for structured messages
            parsed = _parse_forge_errors(result.error)
            if parsed:
                errors.append(
                    "Solidity compilation failed:\n" + "\n".join(parsed)
                )
            else:
                errors.append(f"Solidity compilation failed: {result.error}")

            return CodeValidationResult(
                valid=False,
                errors=errors,
                warnings=warnings,
                solidity_contract_name=contract_name,
            )

        return CodeValidationResult(
            valid=True,
            errors=errors,
            warnings=warnings,
            solidity_abi=result.abi,
            solidity_contract_name=contract_name,
        )
    finally:
        if cleanup and sol_path.exists():
            try:
                sol_path.unlink()
            except OSError:
                pass


async def validate_app_intent(
    js_code: str,
    solidity_code: str,
    skip_solidity: bool = False,
) -> CodeValidationResult:
    """Validate both JS and Solidity code for an App Intent.

    Always validates JS (fast, <2s). Optionally validates Solidity
    (slower, 5-10s). Merges errors/warnings from both into a single result.

    Args:
        js_code: JavaScript scoring module source.
        solidity_code: Solidity contract source.
        skip_solidity: If True, skip Solidity compilation check.

    Returns:
        Combined CodeValidationResult.
    """
    # Always validate JS
    js_result = await validate_js_code(js_code)

    errors = list(js_result.errors)
    warnings = list(js_result.warnings)

    solidity_abi = None
    solidity_contract_name = ""

    # Optionally validate Solidity
    if not skip_solidity and solidity_code and solidity_code.strip():
        sol_result = validate_solidity_code(solidity_code)
        errors.extend(sol_result.errors)
        warnings.extend(sol_result.warnings)
        solidity_abi = sol_result.solidity_abi
        solidity_contract_name = sol_result.solidity_contract_name

    valid = len(errors) == 0
    return CodeValidationResult(
        valid=valid,
        errors=errors,
        warnings=warnings,
        js_config=js_result.js_config,
        js_manifest=js_result.js_manifest,
        js_exports=js_result.js_exports,
        solidity_abi=solidity_abi,
        solidity_contract_name=solidity_contract_name,
    )
