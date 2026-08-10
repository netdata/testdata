#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("generate.py")
SPEC = importlib.util.spec_from_file_location("haproxy_registry_generator", MODULE_PATH)
GENERATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = GENERATOR
SPEC.loader.exec_module(GENERATOR)


class FailClosedParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stats = GENERATOR.STATS_SOURCE.read_text(encoding="utf-8")
        cls.proxy = GENERATOR.PROXY_SOURCE.read_text(encoding="utf-8")
        cls.resolver = GENERATOR.RESOLVER_SOURCE.read_text(encoding="utf-8")
        cls.sticktable = GENERATOR.STICKTABLE_SOURCE.read_text(encoding="utf-8")
        cls.promex = GENERATOR.PROMEX_SOURCE.read_text(encoding="utf-8")

    def test_rejects_process_registration_without_population(self):
        mutated = self.stats.replace(
            "line[ST_I_INF_NBTHREAD]                       =",
            "line[ST_I_INF_REMOVED]                        =",
            1,
        )
        with self.assertRaisesRegex(ValueError, "has no stats_fill_info assignment"):
            GENERATOR.parse_process_registrations(mutated, self.promex)

    def test_rejects_unknown_proxy_capability(self):
        mutated = self.proxy.replace("STATS_PX_CAP___BS", "STATS_PX_CAP___BX", 1)
        with self.assertRaisesRegex(ValueError, "partially declared proxy PromEx field"):
            GENERATOR.parse_proxy_registrations(mutated, self.promex)

    def test_rejects_non_generic_proxy_registration_without_population(self):
        mutated = self.proxy.replace("case ST_I_PX_DOWNTIME:", "case ST_I_PX_REMOVED:")
        with self.assertRaisesRegex(ValueError, "has no supported fill case"):
            GENERATOR.parse_proxy_registrations(mutated, self.promex)

    def test_rejects_resolver_wire_type_drift(self):
        mutated = self.resolver.replace("PROMEX_MT_GAUGE, .flags = PROMEX_FL_MODULE_METRIC", "PROMEX_MT_COUNTER, .flags = PROMEX_FL_MODULE_METRIC", 1)
        with self.assertRaisesRegex(ValueError, "must match exactly one line"):
            GENERATOR.parse_resolver_registrations(mutated)

    def test_rejects_sticktable_wire_type_drift(self):
        mutated = self.sticktable.replace(".type = PROMEX_MT_GAUGE", ".type = PROMEX_MT_COUNTER", 1)
        with self.assertRaisesRegex(ValueError, "unsupported stick-table PromEx type"):
            GENERATOR.parse_sticktable_registrations(mutated)

    def test_rejects_duplicate_generated_family(self):
        mutated = self.stats.replace('alt_name = "nbproc"', 'alt_name = "nbthread"', 1)
        with self.assertRaisesRegex(ValueError, "duplicate families"):
            GENERATOR.generate_registry(mutated, self.proxy, self.resolver, self.sticktable, self.promex)


if __name__ == "__main__":
    unittest.main()
