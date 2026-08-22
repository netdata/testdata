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

    def test_parses_literal_sensor_metric_mapping(self):
        expression = ast.parse(
            "{'fans': sensor_metric('hardware_fan_rpm', 'Hardware fan speed in RPM', HW_FAN_LABELS)}",
            mode="eval",
        ).body
        self.assertEqual(
            GENERATOR._mgr_expression(expression, {"HW_FAN_LABELS": ("hostname", "fan_name")}),
            {
                "fans": {
                    "metric": "hardware_fan_rpm",
                    "description": "Hardware fan speed in RPM",
                    "labels": ("hostname", "fan_name"),
                },
            },
        )

    def test_accepts_only_literal_mapping_values_loop(self):
        statement = ast.parse("for sensor in SENSOR_METRICS.values():\n    pass").body[0]
        assert isinstance(statement, ast.For)
        sensor = {"metric": "hardware_fan_rpm"}
        self.assertEqual(
            GENERATOR._mgr_loop_items(
                statement,
                {"SENSOR_METRICS": {"fans": sensor}},
                {"SENSOR_METRICS": ("module.py", 1, 4)},
            ),
            ("sensor", (sensor,), ("module.py", 1, 4)),
        )

    def test_rejects_other_mapping_loop_methods(self):
        statement = ast.parse("for sensor in SENSOR_METRICS.items():\n    pass").body[0]
        assert isinstance(statement, ast.For)
        with self.assertRaisesRegex(ValueError, "unsupported MGR source loop"):
            GENERATOR._mgr_loop_items(
                statement,
                {"SENSOR_METRICS": {"fans": {"metric": "hardware_fan_rpm"}}},
                {"SENSOR_METRICS": ("module.py", 1, 4)},
            )

    def test_classifies_gauge_info_families(self):
        self.assertEqual(
            GENERATOR.metric_family_shape("ceph_hardware_firmware_info", "gauge", "scalar"),
            "info",
        )
        self.assertEqual(
            GENERATOR.metric_family_shape("ceph_hardware_firmware_info", "counter", "scalar"),
            "scalar",
        )

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

    def test_duplicate_objecter_identity_remains_source_dynamic(self):
        grammar = GENERATOR.GRAMMARS["objecter"]
        self.assertEqual(grammar.identity, "objecter_identity")
        self.assertEqual(grammar.canonical_prefix, grammar.embedded_prefix)
        # Pointer-suffixed loggers are runtime identities generated by Ceph's
        # PerfCountersCollection duplicate-name rewrite, not a second grammar.
        self.assertNotIn("ceph_objecter_0x", grammar.excluded_prefixes)

    def test_info_suffix_requires_gauge_type(self):
        self.assertEqual(GENERATOR.metric_family_shape("example_info", "gauge", "scalar"), "info")
        self.assertEqual(GENERATOR.metric_family_shape("example_info", "counter", "scalar"), "scalar")

    def test_parses_builder_pointer_helper(self):
        text = """void register_metric(PerfCountersBuilder *pcb) {
  pcb->set_prio_default(PerfCountersBuilder::PRIO_USEFUL);
  pcb->add_u64_counter(l_example, "example", "Example");
}

void init(CephContext *cct) {
  PerfCountersBuilder pcb(cct, "example", 1, 2);
  register_metric(&pcb);
}
"""
        helpers = GENERATOR.parse_builder_helpers(text)
        self.assertEqual(set(helpers), {"register_metric"})
        helper = helpers["register_metric"]
        self.assertEqual(helper.default_priority, "PRIO_USEFUL")
        self.assertEqual(len(helper.calls), 1)
        self.assertEqual(helper.calls[0][0], "u64_counter")
        self.assertIn('"example"', helper.calls[0][1])

    def test_parses_builder_reference_helper(self):
        text = """void register_metric(PerfCountersBuilder &pcb) {
  pcb.add_u64(l_x, "x", "X");
}
"""
        helpers = GENERATOR.parse_builder_helpers(text)
        self.assertEqual(helpers["register_metric"].calls[0][0], "u64")

    def test_rejects_conflicting_helper_default_priorities(self):
        text = """void register_metric(PerfCountersBuilder *pcb) {
  pcb->set_prio_default(PerfCountersBuilder::PRIO_USEFUL);
  pcb->add_u64(l_a, "a", "A");
  pcb->set_prio_default(PerfCountersBuilder::PRIO_CRITICAL);
  pcb->add_u64(l_b, "b", "B");
}
"""
        with self.assertRaisesRegex(ValueError, "conflicting default priorities"):
            GENERATOR.parse_builder_helpers(text)

    def test_rejects_unknown_helper_priority_expression(self):
        with mock.patch.object(GENERATOR, "priority_values", return_value={"PRIO_USEFUL": 5}), \
             self.assertRaisesRegex(ValueError, "unsupported builder-helper priority expression"):
            GENERATOR._helper_priority(
                pathlib.Path("/unused"),
                "src/rgw/rgw_perf_counters.cc",
                "",
                GENERATOR.Builder(0, "pcb", "rgw", "rgw"),
                0,
                "l_example, \"example\", \"Example\", NULL, \"future_priority\"",
                None,
            )

    def test_effective_priority_clamps_and_applies_runtime_adjustments(self):
        with mock.patch.object(
            GENERATOR,
            "runtime_priority_adjustments",
            return_value={"rocksdb": 5, "objecter": -3},
        ):
            self.assertEqual(GENERATOR.effective_priority(pathlib.Path("/unused"), "rocksdb", 0), 5)
            self.assertEqual(GENERATOR.effective_priority(pathlib.Path("/unused"), "objecter", 7), 4)
            self.assertEqual(GENERATOR.effective_priority(pathlib.Path("/unused"), "example", 11), 11)

    def test_rejects_runtime_priority_contract_drift(self):
        target = "src/osd/OSD.cc"

        def altered_read(path, *args, **kwargs):
            source = ORIGINAL_READ_TEXT(path, *args, **kwargs)
            if path.as_posix().endswith(target):
                source = source.replace("set_prio_adjust(-3)", "set_prio_adjust(-4)")
            return source

        with mock.patch.object(GENERATOR, "priority_values", return_value={
            "PRIO_USEFUL": 5, "PRIO_DEBUGONLY": 0
        }), mock.patch.object(pathlib.Path, "read_text", altered_read):
            with self.assertRaisesRegex(ValueError, "OSD Objecter priority adjustment changed"):
                GENERATOR.runtime_priority_adjustments(GENERATOR.SOURCE_ROOTS["reef"])

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
