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

    def test_service_unique_id_is_terminal_identity(self):
        builder = GENERATOR.Builder(0, "plb", '"service_unique_id"', "service_unique_id")
        arguments = (
            'l_service_unique_id, service_unique_id.c_str(), '
            '"Unique ID for this service"'
        )
        self.assertEqual(
            GENERATOR.terminal_identity_form(
                "src/common/ceph_context.cc", builder, "u64", arguments
            ),
            "identity",
        )
        with self.assertRaisesRegex(ValueError, "service_unique_id construction changed"):
            GENERATOR.terminal_identity_form(
                "src/common/ceph_context.cc",
                builder,
                "u64",
                arguments.replace("service_unique_id.c_str()", "description.c_str()"),
            )

    def test_perf_priority_uses_producer_exclusive_limit(self):
        registration = GENERATOR.MergedRegistration(
            exact_family="ceph_example_total",
            grammar=None,
            form="example_total",
            prometheus_type="counter",
            shape="scalar",
            priority=5,
            endpoint="daemon_perf",
            classification=None,
            source_variants=("reef",),
            locations=(("ceph_reef", "metrics.cc", 1, 1),),
        )
        availability = "\n".join(GENERATOR._render_availability(registration))
        self.assertIn("axis: perf_priority_limit, op: min, value: 6", availability)
        self.assertNotIn("axis: perf_priority, op: max", availability)

    def test_dynamic_grammar_declares_embedded_raw_source_only(self):
        registration = GENERATOR.MergedRegistration(
            exact_family=None,
            grammar="librbd_image",
            form="rd",
            prometheus_type="counter",
            shape="scalar",
            priority=0,
            endpoint="daemon_perf",
            classification=None,
            source_variants=("reef",),
            locations=(("ceph_reef", "metrics.cc", 1, 1),),
        )
        self.assertEqual(
            GENERATOR._render_raw_branches(registration),
            ["        raw_branches: {embedded: {}}"],
        )

    def test_rgw_sync_raw_source_branch_depends_on_producer(self):
        base = dict(
            exact_family=None,
            grammar="rgw_sync",
            form="fetch_errors",
            prometheus_type="counter",
            shape="scalar",
            priority=0,
            classification=None,
            source_variants=("reef",),
            locations=(("ceph_reef", "metrics.cc", 1, 1),),
        )
        self.assertEqual(
            GENERATOR._render_raw_branches(
                GENERATOR.MergedRegistration(endpoint="exporter_perf", **base)
            ),
            ["        raw_branches: {canonical: {}}"],
        )
        self.assertEqual(
            GENERATOR._render_raw_branches(
                GENERATOR.MergedRegistration(endpoint="mgr_perf", **base)
            ),
            ["        raw_branches: {embedded: {}}"],
        )
        self.assertEqual(
            GENERATOR._render_raw_branches(
                GENERATOR.MergedRegistration(endpoint="daemon_perf", **base)
            ),
            [
                "        raw_branches:",
                "          canonical:",
                "            when: {any: [{all: [{axis: source, op: eq, value: ceph_exporter}]}]}",
                "          embedded:",
                "            when: {any: [{all: [{axis: source, op: eq, value: mgr}]}]}",
            ],
        )

    def test_objecter_raw_source_branches_may_coexist(self):
        registration = GENERATOR.MergedRegistration(
            exact_family=None,
            grammar="objecter",
            form="op_r",
            prometheus_type="counter",
            shape="scalar",
            priority=5,
            endpoint="daemon_perf",
            classification=None,
            source_variants=("reef",),
            locations=(("ceph_reef", "src/osdc/Objecter.cc", 1, 1),),
        )
        self.assertEqual(
            GENERATOR._render_raw_branches(registration),
            ["        raw_branches: {canonical: {}, embedded: {}}"],
        )

    def test_info_suffix_requires_gauge_type(self):
        self.assertEqual(GENERATOR.metric_family_shape("example_info", "gauge", "scalar"), "info")
        self.assertEqual(GENERATOR.metric_family_shape("example_info", "counter", "scalar"), "scalar")

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
