#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("generate.py")
SPEC = importlib.util.spec_from_file_location("vllm_registry_generator", MODULE_PATH)
GENERATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = GENERATOR
SPEC.loader.exec_module(GENERATOR)


class FailClosedParserTest(unittest.TestCase):
    def test_rejects_untyped_metric_declaration(self):
        sources = {"metrics.py": 'MISSING = "vllm:missing"\n'}
        with self.assertRaisesRegex(ValueError, "no typed registration"):
            GENERATOR.parse_registrations(sources)

    def test_rejects_conflicting_metric_types(self):
        sources = {
            "metrics.py": """
Counter(name="vllm:requests", documentation="requests")
Gauge(name="vllm:requests_total", documentation="requests")
"""
        }
        with self.assertRaisesRegex(ValueError, "both counter and gauge"):
            GENERATOR.parse_registrations(sources)

    def test_rejects_missing_ray_transport_contract(self):
        with self.assertRaisesRegex(ValueError, "missing required classes"):
            GENERATOR.validate_ray_transport("", "", "")

    def test_rejects_unclassified_registration_availability(self):
        registration = GENERATOR.Registration(
            declared_name="vllm:future",
            family="vllm:future",
            prometheus_type="gauge",
            location=GENERATOR.SourceLocation("vllm/future/metrics.py", 1, 1),
        )
        with self.assertRaisesRegex(ValueError, "no mechanical availability classification"):
            GENERATOR._registration_capability(registration)

    def test_classifies_terminal_info_gauge_as_writer_ineligible_shape(self):
        registration = GENERATOR.Registration(
            declared_name="vllm:build_info",
            family="vllm:build_info",
            prometheus_type="gauge",
            location=GENERATOR.SourceLocation("vllm/v1/metrics/loggers.py", 1, 1),
        )
        self.assertEqual(registration.shape, "info")

    def test_ray_compatibility_alias_keeps_canonical_capability(self):
        canonical = GENERATOR.Registration(
            declared_name="diffusion_num_canvas_positions",
            family="vllm:diffusion_num_canvas_positions_total",
            prometheus_type="counter",
            location=GENERATOR.SourceLocation("vllm/v1/spec_decode/metrics.py", 1, 1),
        )
        compatibility = GENERATOR._ray_compatibility_registration(canonical)

        self.assertEqual(GENERATOR._registration_capability(compatibility), "diffusion_decoding")


if __name__ == "__main__":
    unittest.main()
