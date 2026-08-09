#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("generate.py")
SPEC = importlib.util.spec_from_file_location("fastapi_registry_generator", MODULE_PATH)
GENERATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = GENERATOR
SPEC.loader.exec_module(GENERATOR)


class FailClosedParserTest(unittest.TestCase):
    def test_rejects_dynamic_metric_name(self):
        source = """
def default(name):
    Counter(name=name, documentation="requests")
"""
        with self.assertRaisesRegex(ValueError, "nonempty string literal"):
            GENERATOR.parse_default_metrics(source)

    def test_rejects_missing_created_emitter(self):
        source = """
class Counter:
    def _child_samples(self):
        return ()
class Summary:
    def _child_samples(self):
        return ()
class Histogram:
    def _child_samples(self):
        return ()
"""
        with self.assertRaisesRegex(ValueError, "gated _created sample"):
            GENERATOR.parse_created_emitters(source)

    def test_rejects_mixed_metric_namespaces(self):
        with self.assertRaisesRegex(ValueError, "one token namespace"):
            GENERATOR._single_token_namespace(["http_requests", "rpc_requests"])


if __name__ == "__main__":
    unittest.main()
