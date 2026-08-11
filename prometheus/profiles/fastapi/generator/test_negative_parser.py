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
    def test_rejects_rebound_default_metric_constructor(self):
        if not GENERATOR.FASTAPI_SOURCE.is_file():
            self.skipTest("pinned upstream source is staged only by the hermetic runner")
        source = GENERATOR.FASTAPI_SOURCE.read_text(encoding="utf-8")
        source = source.replace(
            "from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram, Summary",
            "from prometheus_client import REGISTRY, CollectorRegistry, Gauge as Counter, Histogram, Summary",
            1,
        )
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_default_metrics(source)

    def test_rejects_rebound_inprogress_metric_constructor(self):
        if not GENERATOR.MIDDLEWARE_SOURCE.is_file():
            self.skipTest("pinned upstream source is staged only by the hermetic runner")
        source = GENERATOR.MIDDLEWARE_SOURCE.read_text(encoding="utf-8")
        source = source.replace(
            "from prometheus_client import REGISTRY, CollectorRegistry, Gauge",
            "from prometheus_client import REGISTRY, CollectorRegistry, Counter as Gauge",
            1,
        )
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_inprogress_metric(source)

    def test_rejects_dynamic_metric_name(self):
        source = """
def default(name):
    try:
        TOTAL = Counter(name=name, documentation="requests")
    except ValueError:
        pass
"""
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
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
            GENERATOR.parse_created_emitters(
                source,
                expected_ast_fingerprint=GENERATOR.ast_fingerprint(source),
            )

    def test_rejects_mixed_metric_namespaces(self):
        with self.assertRaisesRegex(ValueError, "one token namespace"):
            GENERATOR._single_token_namespace(["http_requests", "rpc_requests"])

    def test_rejects_changed_inprogress_default(self):
        source = """
class PrometheusInstrumentatorMiddleware:
    def __init__(self, *, should_instrument_requests_inprogress=False, inprogress_name="custom", inprogress_labels=False):
        self.inprogress_name = inprogress_name
        Gauge(name=self.inprogress_name)
"""
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_inprogress_metric(source)

    def test_rejects_unguarded_inprogress_registration(self):
        source = """
class PrometheusInstrumentatorMiddleware:
    def __init__(self, *, should_instrument_requests_inprogress=False, inprogress_name="http_requests_inprogress", inprogress_labels=False):
        self.should_instrument_requests_inprogress = should_instrument_requests_inprogress
        self.inprogress_name = inprogress_name
        self.inprogress_labels = inprogress_labels
        Gauge(
            name=self.inprogress_name,
            labelnames=("method", "handler") if self.inprogress_labels else (),
        )
"""
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_inprogress_metric(source)

    def test_rejects_changed_inprogress_label_modes(self):
        source = """
class PrometheusInstrumentatorMiddleware:
    def __init__(self, *, should_instrument_requests_inprogress=False, inprogress_name="http_requests_inprogress", inprogress_labels=False):
        self.should_instrument_requests_inprogress = should_instrument_requests_inprogress
        self.inprogress_name = inprogress_name
        self.inprogress_labels = inprogress_labels
        if self.should_instrument_requests_inprogress:
            labels = ("method",) if self.inprogress_labels else ()
            Gauge(
                name=self.inprogress_name,
                labelnames=labels,
            )
"""
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_inprogress_metric(source)

    def test_rejects_rebound_inprogress_configuration(self):
        source = """
class PrometheusInstrumentatorMiddleware:
    def __init__(self, *, should_instrument_requests_inprogress=False, inprogress_name="http_requests_inprogress", inprogress_labels=False):
        self.should_instrument_requests_inprogress = True
        self.inprogress_name = "different_runtime_family"
        self.inprogress_labels = True
        if self.should_instrument_requests_inprogress:
            labels = ("method", "handler") if self.inprogress_labels else ()
            Gauge(name=self.inprogress_name, labelnames=labels)
"""
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_inprogress_metric(source)

    def test_rejects_inprogress_registration_in_negative_guard_branch(self):
        source = """
class PrometheusInstrumentatorMiddleware:
    def __init__(self, *, should_instrument_requests_inprogress=False, inprogress_name="http_requests_inprogress", inprogress_labels=False):
        self.should_instrument_requests_inprogress = should_instrument_requests_inprogress
        self.inprogress_name = inprogress_name
        self.inprogress_labels = inprogress_labels
        if self.should_instrument_requests_inprogress:
            pass
        else:
            labels = ("method", "handler") if self.inprogress_labels else ()
            Gauge(name=self.inprogress_name, labelnames=labels)
"""
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_inprogress_metric(source)

    def test_rejects_alternate_inprogress_attribute_writes(self):
        mutations = (
            'self.inprogress_name += "_changed"',
            'self.inprogress_name: str = "other"',
            'setattr(self, "inprogress_name", "other")',
            'self.__setattr__("inprogress_name", "other")',
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                source = f'''\
class PrometheusInstrumentatorMiddleware:
    def __init__(self, *, should_instrument_requests_inprogress=False, inprogress_name="http_requests_inprogress", inprogress_labels=False):
        self.should_instrument_requests_inprogress = should_instrument_requests_inprogress
        self.inprogress_name = inprogress_name
        self.inprogress_labels = inprogress_labels
        {mutation}
        if self.should_instrument_requests_inprogress:
            labels = ("method", "handler") if self.inprogress_labels else ()
            Gauge(name=self.inprogress_name, labelnames=labels)
'''
                with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
                    GENERATOR.parse_inprogress_metric(source)

    def test_rejects_alternate_inprogress_labelnames_write(self):
        source = '''
class PrometheusInstrumentatorMiddleware:
    def __init__(self, *, should_instrument_requests_inprogress=False, inprogress_name="http_requests_inprogress", inprogress_labels=False):
        self.should_instrument_requests_inprogress = should_instrument_requests_inprogress
        self.inprogress_name = inprogress_name
        self.inprogress_labels = inprogress_labels
        if self.should_instrument_requests_inprogress:
            labels = ("method", "handler") if self.inprogress_labels else ()
            labels += ("extra",)
            Gauge(name=self.inprogress_name, labelnames=labels)
'''
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_inprogress_metric(source)

    def test_rejects_conditional_default_registration(self):
        source = '''
def default():
    if False:
        Counter(name="http_never_registered", documentation="never")
'''
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_default_metrics(source)

    def test_rejects_unreachable_direct_default_registration(self):
        source = '''
def default():
    try:
        return
        TOTAL = Counter(name="http_never_registered", documentation="never")
    except ValueError:
        pass
'''
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_default_metrics(source)

    def test_rejects_changed_default_metric_naming(self):
        source = '''
def default(metric_namespace="custom", metric_subsystem=""):
    try:
        TOTAL = Counter(
            name="http_requests_total",
            documentation="requests",
            namespace=metric_namespace,
            subsystem=metric_subsystem,
        )
    except ValueError:
        pass
'''
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_default_metrics(source)

    def test_rejects_unreachable_nested_inprogress_registration(self):
        source = '''
class PrometheusInstrumentatorMiddleware:
    def __init__(self, *, should_instrument_requests_inprogress=False, inprogress_name="http_requests_inprogress", inprogress_labels=False):
        self.should_instrument_requests_inprogress = should_instrument_requests_inprogress
        self.inprogress_name = inprogress_name
        self.inprogress_labels = inprogress_labels
        if self.should_instrument_requests_inprogress:
            labels = ("method", "handler") if self.inprogress_labels else ()
            if False:
                Gauge(name=self.inprogress_name, labelnames=labels)
'''
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_inprogress_metric(source)

    def test_rejects_mapping_inprogress_mutation(self):
        source = '''
class PrometheusInstrumentatorMiddleware:
    def __init__(self, *, should_instrument_requests_inprogress=False, inprogress_name="http_requests_inprogress", inprogress_labels=False):
        self.should_instrument_requests_inprogress = should_instrument_requests_inprogress
        self.inprogress_name = inprogress_name
        self.inprogress_labels = inprogress_labels
        self.__dict__["inprogress_name"] = "other"
        if self.should_instrument_requests_inprogress:
            labels = ("method", "handler") if self.inprogress_labels else ()
            Gauge(name=self.inprogress_name, labelnames=labels)
'''
        with self.assertRaisesRegex(ValueError, "source shape fingerprint"):
            GENERATOR.parse_inprogress_metric(source)


if __name__ == "__main__":
    unittest.main()
