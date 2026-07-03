"""Forge-based Solidity compiler wrapper.

Compiles Solidity source via ``forge build`` and extracts bytecode/ABI
from the resulting JSON artifact. Also supports reading pre-built
artifacts when tests or tooling need them explicitly.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from minotaur_subnet.shared.types import CompilationResult

logger = logging.getLogger(__name__)


class ForgeCompiler:
    """Compiles Solidity and extracts bytecode/ABI from Forge artifacts."""

    def __init__(self, contracts_dir: Path | None = None) -> None:
        self.contracts_dir = contracts_dir or Path(__file__).parents[2] / "contracts"

    # ── public API ────────────────────────────────────────────────────────

    def compile(self, contract_name: str, solidity_source: str) -> CompilationResult:
        """Write source to ``contracts/src/generated/<name>.sol``, run
        ``forge build``, and extract bytecode + ABI from the artifact.
        """
        # Security: reject contract names with path-traversal or shell-injection
        # characters. Only alphanumeric and underscores are allowed so that the
        # resulting filename stays safely inside the generated/ directory.
        if not re.match(r"^[a-zA-Z0-9_]+$", contract_name):
            return CompilationResult(
                contract_name=contract_name,
                bytecode="",
                abi=[],
                error=f"Invalid contract_name: only [a-zA-Z0-9_] characters are allowed",
            )

        gen_dir = self.contracts_dir / "src" / "generated"
        gen_dir.mkdir(parents=True, exist_ok=True)

        sol_path = gen_dir / f"{contract_name}.sol"

        # Security: verify the resolved path is within gen_dir to prevent any
        # path traversal (defence-in-depth beyond the regex check above).
        if not sol_path.resolve().is_relative_to(gen_dir.resolve()):
            return CompilationResult(
                contract_name=contract_name,
                bytecode="",
                abi=[],
                error="Path traversal detected: output path escapes generated/ directory",
            )
        rewritten = self._rewrite_imports(solidity_source)
        sol_path.write_text(rewritten)

        proc = self._run_forge_build()
        if proc.returncode != 0:
            return CompilationResult(
                contract_name=contract_name,
                bytecode="",
                abi=[],
                error=f"forge build failed: {proc.stderr[:500]}",
            )

        return self._read_artifact(contract_name)

    def extract_existing(self, contract_name: str) -> CompilationResult:
        """Extract bytecode/ABI from an already-built Forge artifact.

        Looks in ``contracts/out/<name>.sol/<name>.json``.
        """
        return self._read_artifact(contract_name)

    # ── internals ─────────────────────────────────────────────────────────

    def _read_artifact(self, contract_name: str) -> CompilationResult:
        """Read a Forge artifact JSON and return a CompilationResult."""
        artifact_path = (
            self.contracts_dir / "out" / f"{contract_name}.sol" / f"{contract_name}.json"
        )
        if not artifact_path.exists():
            return CompilationResult(
                contract_name=contract_name,
                bytecode="",
                abi=[],
                error=f"Artifact not found: {artifact_path}",
            )

        try:
            data = json.loads(artifact_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            return CompilationResult(
                contract_name=contract_name,
                bytecode="",
                abi=[],
                error=f"Failed to read artifact: {exc}",
            )

        bytecode_obj = data.get("bytecode", {})
        bytecode_hex = bytecode_obj.get("object", "")
        if bytecode_hex and not bytecode_hex.startswith("0x"):
            bytecode_hex = "0x" + bytecode_hex

        abi = data.get("abi", [])

        if not bytecode_hex:
            return CompilationResult(
                contract_name=contract_name,
                bytecode="",
                abi=abi,
                error="Artifact has no bytecode (abstract contract?)",
            )

        return CompilationResult(
            contract_name=contract_name,
            bytecode=bytecode_hex,
            abi=abi,
        )

    def _rewrite_imports(self, source: str) -> str:
        """Rewrite import paths so submitted app source resolves during
        ``forge build``.

        Apps (minotaur-apps repo, V2 base) import the platform base contracts
        via the ``minotaur_contracts/src/...`` submodule path, e.g.
        ``import "minotaur_contracts/src/AppIntentBaseV2.sol"``. Generated code
        is written to ``contracts/src/generated/``, so mapping the prefix to
        ``../`` points it at this repo's ``contracts/src/`` — reaching
        ``AppIntentBaseV2.sol``, ``ExecutorProxy.sol``, ``interfaces/...`` etc.
        (V1 is not supported on this path — we only deploy V2 contracts.)
        """
        return source.replace('"minotaur_contracts/src/', '"../')

    def _run_forge_build(self) -> subprocess.CompletedProcess:
        """Run ``forge build`` in the contracts directory."""
        logger.info("Running forge build in %s", self.contracts_dir)
        return subprocess.run(
            ["forge", "build"],
            capture_output=True,
            text=True,
            cwd=str(self.contracts_dir),
            timeout=120,
        )
