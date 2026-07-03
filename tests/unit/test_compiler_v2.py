"""Compiler support for V2 app source (DexAggregatorAppV2).

Apps import the platform base via ``minotaur_contracts/src/AppIntentBaseV2.sol``;
generated code lives in ``contracts/src/generated/``, so the compiler rewrites
that prefix to ``../`` to reach ``contracts/src/``. The string rewrite is pure
(always tested); a forge-gated probe confirms the base actually resolves +
compiles against the (V2-containing) contracts submodule.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from minotaur_subnet.deployment.compiler import ForgeCompiler

_REPO_ROOT = Path(__file__).resolve().parents[2]
_V2_BASE = _REPO_ROOT / "contracts" / "src" / "AppIntentBaseV2.sol"
_FORGE = shutil.which("forge") is not None


def test_rewrite_maps_v2_submodule_prefix():
    c = ForgeCompiler()
    out = c._rewrite_imports('import "minotaur_contracts/src/AppIntentBaseV2.sol";')
    assert out == 'import "../AppIntentBaseV2.sol";'


def test_rewrite_maps_nested_submodule_paths():
    c = ForgeCompiler()
    src = (
        'import "minotaur_contracts/src/ExecutorProxy.sol";\n'
        'import "minotaur_contracts/src/interfaces/IAppRegistry.sol";'
    )
    out = c._rewrite_imports(src)
    assert '"../ExecutorProxy.sol"' in out
    assert '"../interfaces/IAppRegistry.sol"' in out


def test_rewrite_leaves_oz_and_other_imports_untouched():
    c = ForgeCompiler()
    src = (
        'import "@openzeppelin/contracts/token/ERC20/IERC20.sol";\n'
        'import "forge-std/Test.sol";'
    )
    assert c._rewrite_imports(src) == src


@pytest.mark.skipif(
    not (_FORGE and _V2_BASE.exists()),
    reason="needs forge + a V2-containing contracts submodule",
)
def test_v2_base_import_resolves_and_compiles():
    """End-to-end: a contract importing the V2 base (via the submodule prefix)
    resolves and compiles — proving the submodule bump + rewrite work together."""
    probe = (
        "// SPDX-License-Identifier: MIT\n"
        "pragma solidity ^0.8.24;\n"
        'import "minotaur_contracts/src/AppIntentBaseV2.sol";\n'
        "contract ImportProbeV2 {}\n"
    )
    result = ForgeCompiler().compile("ImportProbeV2", probe)
    assert not result.error, result.error
    assert result.bytecode, "probe should compile to bytecode once imports resolve"
