#!/usr/bin/env python3
"""Generate LiteLLM's mechanical Prometheus source registry."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from source_registry_client_python import parse_created_emitters


PROMETHEUS_SOURCE = Path("upstreams/litellm/litellm/integrations/prometheus.py")
SERVICES_SOURCE = Path("upstreams/litellm/litellm/integrations/prometheus_services.py")
SERVICE_TYPES_SOURCE = Path("upstreams/litellm/litellm/types/services.py")
IN_FLIGHT_SOURCE = Path("upstreams/litellm/litellm/proxy/middleware/in_flight_requests_middleware.py")
CLIENT_SOURCE = Path("upstreams/prometheus_client_python/prometheus_client/metrics.py")


@dataclass(frozen=True)
class SourceLocation:
    path: str
    line_start: int
    line_end: int


@dataclass(frozen=True)
class MetricRegistration:
    family: str
    prometheus_type: str
    shape: str
    components: tuple[tuple[str, str], ...]
    locations: tuple[SourceLocation, ...]
    emits_created: bool


FACTORIES = {
    "_counter_factory": (
        "counter",
        "scalar",
        (("value", "scalar"),),
        True,
    ),
    "_gauge_factory": (
        "gauge",
        "scalar",
        (("value", "scalar"),),
        False,
    ),
    "_histogram_factory": (
        "histogram",
        "histogram",
        (
            ("bucket", "histogram_bucket"),
            ("count", "histogram_count"),
            ("sum", "histogram_sum"),
        ),
        True,
    ),
}

SERVICE_COMPONENTS = {
    "counter": ("counter", "scalar", (("value", "scalar"),), True),
    "gauge": ("gauge", "scalar", (("value", "scalar"),), False),
    "histogram": (
        "histogram",
        "histogram",
        (
            ("bucket", "histogram_bucket"),
            ("count", "histogram_count"),
            ("sum", "histogram_sum"),
        ),
        True,
    ),
}


def parse_callback_metrics(source: str) -> list[MetricRegistration]:
    tree = ast.parse(source)
    logger = _one_named(tree.body, ast.ClassDef, "PrometheusLogger")
    initializer = _one_named(logger.body, (ast.FunctionDef, ast.AsyncFunctionDef), "__init__")
    calls = [
        node
        for node in ast.walk(initializer)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FACTORIES
    ]
    all_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FACTORIES
    ]
    if {node.lineno for node in calls} != {node.lineno for node in all_calls}:
        raise ValueError("metric factory calls outside PrometheusLogger.__init__ are unsupported")
    if not calls:
        raise ValueError("PrometheusLogger.__init__ registers no supported metrics")

    result = []
    for call in calls:
        metric_type, shape, components, emits_created = FACTORIES[call.func.attr]
        raw_name = _call_string_argument(call, "name", 0)
        family = _canonical_family(raw_name, metric_type)
        result.append(
            MetricRegistration(
                family=family,
                prometheus_type=metric_type,
                shape=shape,
                components=components,
                locations=(
                    SourceLocation(
                        "litellm/integrations/prometheus.py",
                        call.lineno,
                        call.end_lineno or call.lineno,
                    ),
                ),
                emits_created=emits_created,
            )
        )
    _require_unique_families(result, "callback")
    return sorted(result, key=lambda item: item.family)


def parse_service_metrics(service_source: str, type_source: str) -> list[MetricRegistration]:
    service_tree = ast.parse(service_source)
    type_tree = ast.parse(type_source)
    service_enum = _one_named(type_tree.body, ast.ClassDef, "ServiceTypes")
    service_values: dict[str, tuple[str, int]] = {}
    for node in service_enum.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        value = _literal_string(node.value, f"ServiceTypes.{node.targets[0].id}")
        if value in {item[0] for item in service_values.values()}:
            raise ValueError(f"duplicate ServiceTypes value {value!r}")
        service_values[node.targets[0].id] = (value, node.lineno)
    if not service_values:
        raise ValueError("ServiceTypes contains no string members")

    configs = _parse_service_configs(type_tree, service_values)
    default_metrics = _parse_default_service_metrics(service_tree)
    constructor_lines = {
        "histogram": _service_constructor_line(service_tree, "create_histogram", "Histogram"),
        "gauge": _service_constructor_line(service_tree, "create_gauge", "Gauge"),
        "counter": _service_constructor_line(service_tree, "create_counter", "Counter"),
    }

    result = []
    for member, (service, enum_line) in sorted(service_values.items(), key=lambda item: item[1][0]):
        metrics = configs.get(member, default_metrics)
        for metric in sorted(metrics):
            metric_type, shape, components, emits_created = SERVICE_COMPONENTS[metric]
            suffixes = {
                "histogram": ["latency"],
                "gauge": ["size"],
                "counter": ["failed_requests_total", "total_requests_total"],
            }[metric]
            for suffix in suffixes:
                result.append(
                    MetricRegistration(
                        family=f"litellm_{service}_{suffix}",
                        prometheus_type=metric_type,
                        shape=shape,
                        components=components,
                        locations=(
                            SourceLocation("litellm/types/services.py", enum_line, enum_line),
                            SourceLocation(
                                "litellm/integrations/prometheus_services.py",
                                constructor_lines[metric][0],
                                constructor_lines[metric][1],
                            ),
                        ),
                        emits_created=emits_created,
                    )
                )
    _require_unique_families(result, "service")
    return sorted(result, key=lambda item: item.family)


def parse_in_flight_metric(source: str) -> MetricRegistration:
    tree = ast.parse(source)
    middleware = _one_named(tree.body, ast.ClassDef, "InFlightRequestsMiddleware")
    getter = _one_named(middleware.body, (ast.FunctionDef, ast.AsyncFunctionDef), "_get_gauge")
    calls = [
        node
        for node in ast.walk(getter)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Gauge"
    ]
    if len(calls) != 2:
        raise ValueError(f"_get_gauge must construct exactly two Gauge branches, found {len(calls)}")
    names = {_call_string_argument(call, "name", 0) for call in calls}
    if names != {"litellm_in_flight_requests"}:
        raise ValueError(f"_get_gauge branches disagree on family: {sorted(names)}")
    modes = []
    for call in calls:
        keywords = {item.arg: item.value for item in call.keywords if item.arg is not None}
        mode = "all"
        if "multiprocess_mode" in keywords:
            mode = _literal_string(keywords["multiprocess_mode"], "in-flight multiprocess_mode")
        modes.append(mode)
    if sorted(modes) != ["all", "livesum"]:
        raise ValueError(f"_get_gauge must expose default and livesum branches, found {sorted(modes)}")
    locations = tuple(
        SourceLocation(
            "litellm/proxy/middleware/in_flight_requests_middleware.py",
            call.lineno,
            call.end_lineno or call.lineno,
        )
        for call in sorted(calls, key=lambda item: item.lineno)
    )
    return MetricRegistration(
        family="litellm_in_flight_requests",
        prometheus_type="gauge",
        shape="scalar",
        components=(("value", "scalar"),),
        locations=locations,
        emits_created=False,
    )


def generate_registry(
    prometheus_source: str,
    services_source: str,
    service_types_source: str,
    in_flight_source: str,
    client_source: str,
) -> str:
    callback = parse_callback_metrics(prometheus_source)
    services = parse_service_metrics(services_source, service_types_source)
    in_flight = parse_in_flight_metric(in_flight_source)
    all_exact = callback + services + [in_flight]
    _require_unique_families(all_exact, "complete")
    created = [item for item in all_exact if item.emits_created]
    if not created:
        raise ValueError("LiteLLM registers no metrics with generated _created samples")
    created_emitters = parse_created_emitters(client_source)

    created_families = [_created_family(item.family, item.prometheus_type) for item in created]
    canonical = min(created_families, key=lambda family: (len(family), family))
    canonical_prefix = canonical[: -len("_created")]
    lines = [
        "# SPDX-License-Identifier: GPL-3.0-or-later",
        "",
        "version: v1",
        "profile: litellm",
        "generated: true",
        "family_grammars:",
        "  python_created_family:",
        "    forms:",
        "      generated:",
        f"        canonical: {{prefix: {canonical_prefix}, suffix: _created}}",
        "        embedded:",
        "          prefix: litellm_",
        "          suffix: created",
        "          separator: _",
        "          identity_slot: {name: instrument, nonempty: true}",
        "groups:",
    ]
    lines.extend(_render_group("callback", callback))
    lines.extend(_render_group("service_metrics", services))
    lines.extend(_render_group("in_flight", [in_flight]))
    lines.extend(_render_created_group(created, created_emitters))
    return "\n".join(lines) + "\n"


def _render_group(group: str, registrations: list[MetricRegistration]) -> list[str]:
    result = [f"  {group}:", "    registrations:"]
    for registration in sorted(registrations, key=lambda item: item.family):
        result.extend(_render_registration(registration))
    return result


def _render_registration(registration: MetricRegistration) -> list[str]:
    result = [
        f"      {_registration_id(registration.family)}:",
        f"        family: {{exact: {registration.family}}}",
        f"        prometheus: {{type: {registration.prometheus_type}, shape: {registration.shape}}}",
        "        components:",
    ]
    for component_id, wire_role in registration.components:
        result.append(f"          {component_id}: {{wire_role: {wire_role}}}")
    result.append("        source_locations:")
    result.extend(_render_locations(registration.locations, "litellm"))
    return result


def _render_created_group(
    registrations: list[MetricRegistration],
    emitters: dict[str, tuple[int, int]],
) -> list[str]:
    result = [
        "  generated_components:",
        "    registrations:",
        "      python_created_component:",
        "        family: {grammar: python_created_family, form: generated}",
        "        raw_branches: {canonical: {}, embedded: {}}",
        "        prometheus: {type: gauge, shape: scalar}",
        "        when:",
        "          any:",
        "            - all: [{axis: mode, op: eq, value: single}]",
        "        components:",
        "          value: {wire_role: scalar}",
        "        source_locations:",
    ]
    registration_locations = sorted(
        {location for registration in registrations for location in registration.locations},
        key=lambda item: (item.path, item.line_start, item.line_end),
    )
    result.extend(_render_locations(tuple(registration_locations), "litellm"))
    for class_name in ("Counter", "Summary", "Histogram"):
        line_start, line_end = emitters[class_name]
        result.extend(
            _render_locations(
                (SourceLocation("prometheus_client/metrics.py", line_start, line_end),),
                "prometheus_client_python",
            )
        )
    return result


def _render_locations(locations: tuple[SourceLocation, ...], upstream: str) -> list[str]:
    result = []
    for location in locations:
        result.extend(
            [
                f"          - upstream: {upstream}",
                f"            path: {location.path}",
                f"            range: {{start: {location.line_start}, end: {location.line_end}}}",
            ]
        )
    return result


def _parse_service_configs(tree: ast.Module, services: dict[str, tuple[str, int]]) -> dict[str, frozenset[str]]:
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "DEFAULT_SERVICE_CONFIGS"
    ]
    if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Dict):
        raise ValueError("expected exactly one literal DEFAULT_SERVICE_CONFIGS mapping")
    result: dict[str, frozenset[str]] = {}
    for key_node, value_node in zip(assignments[0].value.keys, assignments[0].value.values):
        member = _service_member_reference(key_node)
        if member not in services:
            raise ValueError(f"DEFAULT_SERVICE_CONFIGS references unknown ServiceTypes.{member}")
        if member in result:
            raise ValueError(f"DEFAULT_SERVICE_CONFIGS duplicates ServiceTypes.{member}")
        if not isinstance(value_node, ast.Dict) or len(value_node.keys) != 1:
            raise ValueError(f"ServiceTypes.{member} configuration must contain only metrics")
        key = _literal_string(value_node.keys[0], f"ServiceTypes.{member} config key")
        if key != "metrics" or not isinstance(value_node.values[0], (ast.List, ast.Tuple)):
            raise ValueError(f"ServiceTypes.{member} configuration must contain a literal metrics list")
        metrics = frozenset(_service_metric_reference(item) for item in value_node.values[0].elts)
        if not metrics:
            raise ValueError(f"ServiceTypes.{member} configuration has no metrics")
        result[member] = metrics
    return result


def _parse_default_service_metrics(tree: ast.Module) -> frozenset[str]:
    logger = _one_named(tree.body, ast.ClassDef, "PrometheusServicesLogger")
    method = _one_named(logger.body, (ast.FunctionDef, ast.AsyncFunctionDef), "_get_service_metrics_initialize")
    defaults = [
        node.value
        for node in method.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "DEFAULT_METRICS"
    ]
    if len(defaults) != 1 or not isinstance(defaults[0], (ast.List, ast.Tuple)):
        raise ValueError("_get_service_metrics_initialize must define one literal DEFAULT_METRICS")
    metrics = frozenset(_service_metric_reference(item) for item in defaults[0].elts)
    if not metrics:
        raise ValueError("DEFAULT_METRICS must not be empty")
    return metrics


def _service_constructor_line(tree: ast.Module, method_name: str, constructor: str) -> tuple[int, int]:
    logger = _one_named(tree.body, ast.ClassDef, "PrometheusServicesLogger")
    method = _one_named(logger.body, (ast.FunctionDef, ast.AsyncFunctionDef), method_name)
    calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr == constructor
    ]
    if len(calls) != 1:
        raise ValueError(f"{method_name} must call self.{constructor} exactly once")
    return method.lineno, method.end_lineno or method.lineno


def _service_member_reference(node: ast.AST | None) -> str:
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "value"
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "ServiceTypes"
    ):
        return node.value.attr
    raise ValueError("DEFAULT_SERVICE_CONFIGS keys must be ServiceTypes.<member>.value")


def _service_metric_reference(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ServiceMetrics":
        value = node.attr.lower()
        if value in SERVICE_COMPONENTS:
            return value
    raise ValueError("service metric must be a supported ServiceMetrics member")


def _one_named(nodes, node_type, name):
    matches = [node for node in nodes if isinstance(node, node_type) and node.name == name]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {name}, found {len(matches)}")
    return matches[0]


def _call_string_argument(call: ast.Call, keyword: str, position: int) -> str:
    keywords = [item.value for item in call.keywords if item.arg == keyword]
    if len(keywords) > 1:
        raise ValueError(f"call at line {call.lineno} repeats {keyword}")
    if keywords and len(call.args) > position:
        raise ValueError(f"call at line {call.lineno} supplies {keyword} positionally and by keyword")
    if keywords:
        return _literal_string(keywords[0], f"{keyword} at line {call.lineno}")
    if len(call.args) <= position:
        raise ValueError(f"call at line {call.lineno} omits {keyword}")
    return _literal_string(call.args[position], f"{keyword} at line {call.lineno}")


def _literal_string(node: ast.AST | None, field: str) -> str:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or not node.value:
        raise ValueError(f"{field} must be a nonempty string literal")
    return node.value


def _canonical_family(name: str, metric_type: str) -> str:
    if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", name):
        raise ValueError(f"unsupported metric family {name!r}")
    if metric_type == "counter" and not name.endswith("_total"):
        return name + "_total"
    return name


def _created_family(family: str, prometheus_type: str) -> str:
    if prometheus_type == "counter" and family.endswith("_total"):
        family = family[: -len("_total")]
    return family + "_created"


def _registration_id(family: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", family.lower()).strip("_")
    if not value or not value[0].isalpha():
        raise ValueError(f"family {family!r} cannot form a registration ID")
    return value


def _require_unique_families(registrations: list[MetricRegistration], field: str) -> None:
    families = [item.family for item in registrations]
    duplicates = sorted({family for family in families if families.count(family) > 1})
    if duplicates:
        raise ValueError(f"{field} registrations contain duplicate families: {duplicates}")


def main() -> None:
    print(
        generate_registry(
            PROMETHEUS_SOURCE.read_text(encoding="utf-8"),
            SERVICES_SOURCE.read_text(encoding="utf-8"),
            SERVICE_TYPES_SOURCE.read_text(encoding="utf-8"),
            IN_FLIGHT_SOURCE.read_text(encoding="utf-8"),
            CLIENT_SOURCE.read_text(encoding="utf-8"),
        ),
        end="",
    )


if __name__ == "__main__":
    main()
