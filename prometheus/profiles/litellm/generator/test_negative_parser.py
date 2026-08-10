#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("generate.py")
SPEC = importlib.util.spec_from_file_location("litellm_registry_generator", MODULE_PATH)
GENERATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = GENERATOR
SPEC.loader.exec_module(GENERATOR)


class FailClosedParserTest(unittest.TestCase):
    def test_rejects_dynamic_callback_metric_name(self):
        source = """
class PrometheusLogger:
    def __init__(self, name):
        self.metric = self._counter_factory(name, "requests")
"""
        with self.assertRaisesRegex(ValueError, "nonempty string literal"):
            GENERATOR.parse_callback_metrics(source)

    def test_rejects_unknown_service_metric(self):
        node = __import__("ast").parse("ServiceMetrics.SUMMARY").body[0].value
        with self.assertRaisesRegex(ValueError, "supported ServiceMetrics"):
            GENERATOR._service_metric_reference(node)

    def test_rejects_divergent_in_flight_families(self):
        source = """
class InFlightRequestsMiddleware:
    def _get_gauge(self):
        if enabled:
            return Gauge("litellm_in_flight_requests", "active", multiprocess_mode="livesum")
        return Gauge("litellm_other_requests", "active")
"""
        with self.assertRaisesRegex(ValueError, "branches disagree"):
            GENERATOR.parse_in_flight_metric(source)

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


if __name__ == "__main__":
    unittest.main()
