#!/usr/bin/env python3

import unittest

from source_registry_client_python import (
    ast_fingerprint,
    parse_created_emitters,
    require_ast_fingerprints,
)


VALID_SOURCE = """
class Counter:
    def _child_samples(self):
        if _use_created:
            return [samples.Sample("_created", {}, 1)]
class Summary:
    def _child_samples(self):
        if _use_created:
            return [Sample("_created", {}, 1)]
class Histogram:
    async def _child_samples(self):
        if _use_created:
            return [Sample("_created", {}, 1)]
"""


class CreatedEmitterParserTest(unittest.TestCase):
    def test_requires_the_complete_reviewed_source_closure(self):
        expected = {"one.py": ast_fingerprint("value = 1\n")}
        require_ast_fingerprints({"one.py": "value = 1\n"}, expected, "test modules")
        with self.assertRaisesRegex(ValueError, "source closure differs"):
            require_ast_fingerprints({}, expected, "test modules")
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            require_ast_fingerprints({"one.py": "value = 2\n"}, expected, "test modules")

    def test_returns_exact_method_ranges(self):
        self.assertEqual(
            parse_created_emitters(
                VALID_SOURCE,
                expected_ast_fingerprint=ast_fingerprint(VALID_SOURCE),
            ),
            {"Counter": (3, 5), "Summary": (7, 9), "Histogram": (11, 13)},
        )

    def test_supports_an_explicit_class_subset(self):
        self.assertEqual(
            set(
                parse_created_emitters(
                    VALID_SOURCE,
                    ("Counter", "Histogram"),
                    expected_ast_fingerprint=ast_fingerprint(VALID_SOURCE),
                )
            ),
            {"Counter", "Histogram"},
        )

    def test_rejects_missing_gate(self):
        source = VALID_SOURCE.replace("if _use_created:\n            return [samples.Sample", "return [samples.Sample", 1)
        with self.assertRaisesRegex(ValueError, "gated _created sample"):
            parse_created_emitters(
                source,
                expected_ast_fingerprint=ast_fingerprint(source),
            )

    def test_rejects_created_samples_behind_unrelated_false_guards(self):
        source = VALID_SOURCE.replace(
            "if _use_created:\n            return",
            "enabled = _use_created\n        if False:\n            return",
        )
        with self.assertRaisesRegex(ValueError, "gated _created sample"):
            parse_created_emitters(
                source,
                expected_ast_fingerprint=ast_fingerprint(source),
            )

    def test_rejects_duplicate_class_contract(self):
        with self.assertRaisesRegex(ValueError, "nonempty and unique"):
            parse_created_emitters(VALID_SOURCE, ("Counter", "Counter"))


if __name__ == "__main__":
    unittest.main()
