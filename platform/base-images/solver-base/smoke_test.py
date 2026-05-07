"""solver-base smoke test.

Run inside the built image (via RUN in the Dockerfile) to catch missing
or broken imports BEFORE we tag the image. If any import here fails,
the build fails — the image is never tagged.

Keep this list aligned with what a miner solver is reasonably expected
to use. If we add a dep to the Dockerfile, add the import here too.
"""

from __future__ import annotations

import sys


REQUIRED_IMPORTS = [
    # Core EVM primitives (inherited from base v1)
    "eth_abi",
    "eth_hash",
    "eth_utils",
    "eth_typing",
    # HTTP / async (added in v2)
    "aiohttp",
    "requests",
    "urllib3",
    # Web3 + signing (added in v2)
    "web3",
    "eth_account",
    "eth_keys",
    "hexbytes",
    "rlp",
    "eth_rlp",
    # Crypto
    "ckzg",
    "pycryptodome",  # package name is pycryptodome, module is Crypto
    # Utilities
    "pydantic",
    "numpy",
]


def _import(name: str) -> bool:
    # pycryptodome exposes itself as the ``Crypto`` module, not
    # ``pycryptodome``. Keep the PyPI name in the list above for human
    # readability but import the right symbol.
    module_name = "Crypto" if name == "pycryptodome" else name
    try:
        __import__(module_name)
    except Exception as exc:
        print(f"  FAIL {name}: {exc}", file=sys.stderr)
        return False
    return True


def main() -> int:
    print(f"solver-base smoke test: {len(REQUIRED_IMPORTS)} imports")
    failures = [n for n in REQUIRED_IMPORTS if not _import(n)]
    if failures:
        print(
            f"\nsolver-base smoke test FAILED — missing: {failures}",
            file=sys.stderr,
        )
        return 1
    # Quick sanity on web3 — instantiating the main class shouldn't need
    # network. Catches broken transitive deps that bypassed imports.
    try:
        from web3 import Web3
        Web3()
    except Exception as exc:
        print(f"\nweb3.Web3() construction FAILED: {exc}", file=sys.stderr)
        return 1
    print("solver-base smoke test PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
