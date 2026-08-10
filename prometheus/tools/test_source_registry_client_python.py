#!/usr/bin/env python3

import unittest

from source_registry_client_python import parse_created_emitters


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
    def test_returns_exact_method_ranges(self):
        self.assertEqual(
            parse_created_emitters(VALID_SOURCE),
            {"Counter": (3, 5), "Summary": (7, 9), "Histogram": (11, 13)},
        )

    def test_supports_an_explicit_class_subset(self):
        self.assertEqual(
            set(parse_created_emitters(VALID_SOURCE, ("Counter", "Histogram"))),
            {"Counter", "Histogram"},
        )

    def test_rejects_missing_gate(self):
        source = VALID_SOURCE.replace("if _use_created:\n            return [samples.Sample", "return [samples.Sample", 1)
        with self.assertRaisesRegex(ValueError, "gated _created sample"):
            parse_created_emitters(source)

    def test_rejects_duplicate_class_contract(self):
        with self.assertRaisesRegex(ValueError, "nonempty and unique"):
            parse_created_emitters(VALID_SOURCE, ("Counter", "Counter"))


if __name__ == "__main__":
    unittest.main()
