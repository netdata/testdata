#!/usr/bin/env python3

import ast
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("generate.py")
SPEC = importlib.util.spec_from_file_location("ceph_registry_generator", MODULE_PATH)
GENERATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = GENERATOR
SPEC.loader.exec_module(GENERATOR)

ORIGINAL_READ_TEXT = pathlib.Path.read_text


class FailClosedParserTest(unittest.TestCase):
    def tearDown(self):
        GENERATOR.validate_endpoint_contracts.cache_clear()
        GENERATOR.priority_values.cache_clear()
        GENERATOR.priority_cache_groups.cache_clear()
        GENERATOR.dmclock_groups.cache_clear()
        GENERATOR.striper_groups.cache_clear()
        GENERATOR.grammar_locations.cache_clear()

    def test_rejects_unbalanced_cpp_arguments(self):
        with self.assertRaisesRegex(ValueError, "unterminated C\\+\\+ metric arguments"):
            GENERATOR.split_cpp_arguments('counter, "name", call(value')

    def test_rejects_unknown_mgr_expression(self):
        expression = ast.parse("dynamic_metric_name()", mode="eval").body
        with self.assertRaisesRegex(ValueError, "unsupported MGR source expression"):
            GENERATOR._mgr_expression(expression, {})

    def test_rejects_new_cpp_metric_constructor(self):
        target = "src/client/Client.cc"

        def altered_read(path, *args, **kwargs):
            source = ORIGINAL_READ_TEXT(path, *args, **kwargs)
            if path.as_posix().endswith(target):
                source = source.replace(".add_u64(", ".add_future(", 1)
            return source

        GENERATOR.validate_endpoint_contracts.cache_clear()
        with mock.patch.object(pathlib.Path, "read_text", altered_read):
            with self.assertRaisesRegex(ValueError, "unclassified metric constructors"):
                GENERATOR.generate_registry(GENERATOR.SOURCE_ROOTS)

    def test_rejects_promethize_contract_drift(self):
        target = "src/exporter/util.cc"

        def altered_read(path, *args, **kwargs):
            source = ORIGINAL_READ_TEXT(path, *args, **kwargs)
            if path.as_posix().endswith(target):
                source = source.replace('name = "ceph_" + name', 'name = "changed_" + name')
            return source

        GENERATOR.validate_endpoint_contracts.cache_clear()
        with mock.patch.object(pathlib.Path, "read_text", altered_read):
            with self.assertRaisesRegex(ValueError, "metric-name normalization changed"):
                GENERATOR.validate_endpoint_contracts(GENERATOR.SOURCE_ROOTS["reef"])

    def test_rejects_unknown_nvmeof_metric_family(self):
        target = "control/prometheus.py"

        def altered_read(path, *args, **kwargs):
            source = ORIGINAL_READ_TEXT(path, *args, **kwargs)
            if path.as_posix().endswith(target):
                source = source.replace(
                    "bdev_metadata = GaugeMetricFamily(",
                    "bdev_metadata = FutureMetricFamily(",
                    1,
                )
            return source

        with mock.patch.object(pathlib.Path, "read_text", altered_read):
            with self.assertRaisesRegex(ValueError, "unsupported metric-family constructors"):
                GENERATOR.generate_registry(GENERATOR.SOURCE_ROOTS)

    def test_rejects_dynamic_nvmeof_metric_name(self):
        target = "control/prometheus.py"

        def altered_read(path, *args, **kwargs):
            source = ORIGINAL_READ_TEXT(path, *args, **kwargs)
            if path.as_posix().endswith(target):
                source = source.replace(
                    'f"{self.metric_prefix}_gateway"',
                    "dynamic_metric_name()",
                    1,
                )
            return source

        with mock.patch.object(pathlib.Path, "read_text", altered_read):
            with self.assertRaisesRegex(ValueError, "bounded prefix f-string"):
                GENERATOR.generate_registry(GENERATOR.SOURCE_ROOTS)

    def test_rejects_nvmeof_client_wire_contract_drift(self):
        target = "prometheus_client/metrics_core.py"

        def altered_read(path, *args, **kwargs):
            source = ORIGINAL_READ_TEXT(path, *args, **kwargs)
            if path.as_posix().endswith(target):
                source = source.replace("self.name + '_info'", "self.name + '_metadata'", 1)
            return source

        with mock.patch.object(pathlib.Path, "read_text", altered_read):
            with self.assertRaisesRegex(ValueError, "InfoMetricFamily wire contract changed"):
                GENERATOR.generate_registry(GENERATOR.SOURCE_ROOTS)


if __name__ == "__main__":
    unittest.main()
