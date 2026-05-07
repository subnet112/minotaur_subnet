"""Tests for pre-flight code validation (engine/validation.py).

Run with:
    python -m pytest minotaur_subnet/engine/test_validation.py -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.engine.validation import (
    validate_js_code,
    validate_solidity_code,
    validate_app_intent,
)


# ═══════════════════════════════════════════════════════════════════════════════
#                       JS VALIDATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════

VALID_JS = '''
var config = { name: "Test", version: "1.0.0", type: "test" };

var manifest = {
    intent_functions: [{
        name: "swap",
        params: { input_token: { type: "address" } },
        example_params: { input_token: "0x1234" },
    }],
};

function score(plan, state, context) {
    return { score: 0.8, valid: true, reason: "ok" };
}

module.exports = { config: config, manifest: manifest, score: score };
'''

VALID_JS_WITH_VALIDATE = '''
function score(plan, state, context) {
    return { score: 0.5, valid: true };
}
function validate(plan, state, context) {
    return { valid: true };
}
module.exports = { score: score, validate: validate };
'''

JS_MISSING_SCORE = '''
var config = { name: "Bad", version: "1.0.0" };
module.exports = { config: config };
'''

JS_SYNTAX_ERROR = '''
function score(plan, state, context {  // missing closing paren
    return { score: 0 };
}
module.exports = { score: score };
'''

JS_RUNTIME_ERROR = '''
// This will throw at load time
var x = undefined;
x.foo.bar;
module.exports = { score: function() {} };
'''


class TestValidateJsCode(unittest.TestCase):
    """Tests for validate_js_code()."""

    def test_valid_js_with_all_exports(self):
        result = asyncio.run(validate_js_code(VALID_JS))
        self.assertTrue(result.valid)
        self.assertEqual(len(result.errors), 0)
        self.assertEqual(len(result.warnings), 0)
        self.assertIn("score", result.js_exports)
        self.assertIsNotNone(result.js_config)
        self.assertEqual(result.js_config["name"], "Test")
        self.assertIsNotNone(result.js_manifest)

    def test_valid_js_without_config_warns(self):
        result = asyncio.run(validate_js_code(VALID_JS_WITH_VALIDATE))
        self.assertTrue(result.valid)
        self.assertEqual(len(result.errors), 0)
        # Should warn about missing config and manifest
        self.assertGreater(len(result.warnings), 0)
        warning_text = " ".join(result.warnings)
        self.assertIn("config", warning_text)

    def test_missing_score_function(self):
        result = asyncio.run(validate_js_code(JS_MISSING_SCORE))
        self.assertFalse(result.valid)
        self.assertGreater(len(result.errors), 0)
        error_text = " ".join(result.errors)
        self.assertIn("score", error_text)

    def test_syntax_error(self):
        result = asyncio.run(validate_js_code(JS_SYNTAX_ERROR))
        self.assertFalse(result.valid)
        self.assertGreater(len(result.errors), 0)

    def test_runtime_error_at_load(self):
        result = asyncio.run(validate_js_code(JS_RUNTIME_ERROR))
        self.assertFalse(result.valid)
        self.assertGreater(len(result.errors), 0)

    def test_empty_code(self):
        result = asyncio.run(validate_js_code(""))
        self.assertFalse(result.valid)

    def test_exports_list_populated(self):
        result = asyncio.run(validate_js_code(VALID_JS))
        self.assertIn("config", result.js_exports)
        self.assertIn("manifest", result.js_exports)
        self.assertIn("score", result.js_exports)


# ═══════════════════════════════════════════════════════════════════════════════
#                       COMBINED VALIDATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidateAppIntent(unittest.TestCase):
    """Tests for combined JS + Solidity validation."""

    def test_js_only_skip_solidity(self):
        result = asyncio.run(validate_app_intent(VALID_JS, "", skip_solidity=True))
        self.assertTrue(result.valid)
        self.assertEqual(len(result.errors), 0)

    def test_invalid_js_fails_even_with_skip_solidity(self):
        result = asyncio.run(validate_app_intent(JS_MISSING_SCORE, "", skip_solidity=True))
        self.assertFalse(result.valid)

    def test_empty_solidity_skipped(self):
        """Empty solidity_code is silently skipped even without skip_solidity."""
        result = asyncio.run(validate_app_intent(VALID_JS, ""))
        self.assertTrue(result.valid)

    def test_bad_solidity_fails(self):
        """Invalid Solidity code causes validation failure."""
        bad_sol = "pragma solidity ^0.8.24; contract Broken { function x() }"
        result = asyncio.run(validate_app_intent(VALID_JS, bad_sol))
        self.assertFalse(result.valid)
        error_text = " ".join(result.errors)
        self.assertTrue(
            "Solidity" in error_text or "compilation" in error_text.lower()
            or "contract name" in error_text.lower(),
        )


# ═══════════════════════════════════════════════════════════════════════════════
#                       SOLIDITY VALIDATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidateSolidityCode(unittest.TestCase):
    """Tests for validate_solidity_code()."""

    def test_no_contract_name_detected(self):
        """Code without a contract declaration fails."""
        result = validate_solidity_code("pragma solidity ^0.8.24;")
        self.assertFalse(result.valid)
        error_text = " ".join(result.errors)
        self.assertIn("contract name", error_text.lower())

    def test_auto_detects_contract_name(self):
        """Contract name is auto-detected from source."""
        # This will fail compilation (missing AppIntentBase import),
        # but we can verify the contract name was detected
        sol = 'pragma solidity ^0.8.24; contract MyTestApp { function x() public {} }'
        result = validate_solidity_code(sol)
        self.assertEqual(result.solidity_contract_name, "MyTestApp")


if __name__ == "__main__":
    unittest.main()
