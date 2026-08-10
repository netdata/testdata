#!/usr/bin/env python3
"""Generate vLLM's native and Ray Prometheus registration registry."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from source_registry_client_python import parse_created_emitters as parse_client_created_emitters


VLLM_ROOT = Path("upstreams/vllm")
CLIENT_SOURCE = Path("upstreams/prometheus_client_python/prometheus_client/metrics.py")
RAY_AGENT_SOURCE = Path("upstreams/ray/python/ray/_private/metrics_agent.py")
RAY_TAG_SOURCE = Path("upstreams/ray/src/ray/stats/tag_defs.cc")
RAY_WRAPPER_PATH = "vllm/v1/metrics/ray_wrappers.py"

REGISTRATION_PATHS = (
    "vllm/v1/metrics/loggers.py",
    "vllm/v1/metrics/perf.py",
    "vllm/v1/spec_decode/metrics.py",
    "vllm/distributed/kv_transfer/kv_connector/v1/nixl/stats.py",
    "vllm/distributed/kv_transfer/kv_connector/v1/hf3fs/hf3fs_connector.py",
    "vllm/distributed/kv_transfer/kv_connector/v1/mooncake/store/metrics.py",
    "vllm/distributed/kv_transfer/kv_connector/v1/offloading/metrics.py",
    "vllm/entrypoints/speech_to_text/realtime/metrics.py",
    "vllm/parser/metrics.py",
    "vllm/v1/kv_offload/cpu/common.py",
    "vllm/v1/kv_offload/cpu/spec.py",
    "vllm/v1/kv_offload/tiering/base.py",
    "vllm/v1/kv_offload/tiering/spec.py",
)

RAY_UNSUPPORTED_PATH_PREFIXES = (
    "vllm/entrypoints/speech_to_text/realtime/",
    "vllm/parser/",
)

CORE_REGISTRATION_PATHS = {
    "vllm/v1/metrics/loggers.py",
    "vllm/v1/metrics/perf.py",
}

CAPABILITY_BY_REGISTRATION_PATH = {
    "vllm/distributed/kv_transfer/kv_connector/v1/offloading/metrics.py": "kv_offload",
    "vllm/v1/kv_offload/cpu/spec.py": "kv_offload",
    "vllm/v1/kv_offload/tiering/spec.py": "kv_offload",
    "vllm/distributed/kv_transfer/kv_connector/v1/nixl/stats.py": "nixl",
    "vllm/distributed/kv_transfer/kv_connector/v1/hf3fs/hf3fs_connector.py": "hf3fs",
    "vllm/distributed/kv_transfer/kv_connector/v1/mooncake/store/metrics.py": "mooncake",
    "vllm/entrypoints/speech_to_text/realtime/metrics.py": "realtime",
    "vllm/parser/metrics.py": "tool_parser",
}

COMPONENTS = {
    "counter": (("value", "scalar"),),
    "gauge": (("value", "scalar"),),
    "histogram": (
        ("bucket", "histogram_bucket"),
        ("count", "histogram_count"),
        ("sum", "histogram_sum"),
    ),
}

SHAPES = {"counter": "scalar", "gauge": "scalar", "histogram": "histogram"}

CONSTRUCTORS = {
    "Counter": "counter",
    "_counter_cls": "counter",
    "Gauge": "gauge",
    "_gauge_cls": "gauge",
    "Histogram": "histogram",
    "_histogram_cls": "histogram",
}

METADATA_CONSTRUCTORS = {
    "OffloadingCounterMetadata": "counter",
    "OffloadingGaugeMetadata": "gauge",
    "OffloadingHistogramMetadata": "histogram",
}

METRIC_NAME = re.compile(r"vllm:[a-zA-Z0-9_:]+\Z")


@dataclass(frozen=True)
class SourceLocation:
    path: str
    start: int
    end: int


@dataclass(frozen=True)
class Registration:
    declared_name: str
    family: str
    prometheus_type: str
    location: SourceLocation

    @property
    def shape(self) -> str:
        if self.prometheus_type == "gauge" and self.family.endswith("_info"):
            return "info"
        return SHAPES[self.prometheus_type]

    @property
    def components(self) -> tuple[tuple[str, str], ...]:
        return COMPONENTS[self.prometheus_type]


def parse_registrations(sources: dict[str, str]) -> list[Registration]:
    trees = {path: ast.parse(source, filename=path) for path, source in sources.items()}
    symbols = _collect_metric_symbols(trees)
    registrations: list[Registration] = []

    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                kind = CONSTRUCTORS.get(_terminal_name(node.func))
                if kind is not None:
                    name = _call_metric_name(node, symbols)
                    if name is not None:
                        registrations.append(_registration(name, kind, path, node))
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    kind = _metadata_kind(value)
                    name = _resolve_metric_name(key, symbols) if key is not None else None
                    if kind is not None and name is not None:
                        registrations.append(_registration(name, kind, path, value))
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if not isinstance(value, ast.Call):
                    continue
                kind = _metadata_kind(value)
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                if kind is not None and isinstance(target, ast.Subscript):
                    name = _resolve_metric_name(target.slice, symbols)
                    if name is not None:
                        registrations.append(_registration(name, kind, path, value))

        registrations.extend(_counter_spec_registrations(path, tree))
        registrations.extend(_local_named_constructor_registrations(path, tree))

    result = _deduplicate_registrations(registrations)
    declared = {
        value
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and METRIC_NAME.fullmatch(node.value)
        for value in [node.value]
    }
    registered_names = {registration.family for registration in result}
    registered_names.update(
        registration.family[: -len("_total")]
        for registration in result
        if registration.prometheus_type == "counter" and registration.family.endswith("_total")
    )
    missing = sorted(declared - registered_names)
    if missing:
        raise ValueError(f"metric-name declarations have no typed registration: {missing}")
    if not result:
        raise ValueError("vLLM sources contain no supported metric registrations")
    return result


def _collect_metric_symbols(trees: dict[str, ast.AST]) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                if not METRIC_NAME.fullmatch(value.value):
                    continue
                for target in targets:
                    name = _terminal_name(target)
                    if name:
                        candidates.setdefault(name, set()).add(value.value)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                value = statement.value
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str) or not METRIC_NAME.fullmatch(value.value):
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        candidates.setdefault(f"{node.name}.{target.id}", set()).add(value.value)
    return {name: next(iter(values)) for name, values in candidates.items() if len(values) == 1}


def _call_metric_name(node: ast.Call, symbols: dict[str, str]) -> str | None:
    values = [keyword.value for keyword in node.keywords if keyword.arg == "name"]
    if len(values) > 1:
        raise ValueError(f"metric constructor at line {node.lineno} has multiple name keywords")
    if not values and node.args and _terminal_name(node.func) in {"Counter", "Gauge", "Histogram"}:
        values = [node.args[0]]
    if not values:
        return None
    if isinstance(values[0], ast.Name) and values[0].id == "name":
        return None
    return _resolve_metric_name(values[0], symbols)


def _resolve_metric_name(node: ast.AST, symbols: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if METRIC_NAME.fullmatch(node.value) else None
    qualified = _qualified_name(node)
    if qualified in symbols:
        return symbols[qualified]
    return symbols.get(_terminal_name(node))


def _metadata_kind(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    return METADATA_CONSTRUCTORS.get(_terminal_name(node.func))


def _counter_spec_registrations(path: str, tree: ast.AST) -> list[Registration]:
    result: list[Registration] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(_terminal_name(target) == "counter_specs" for target in targets):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            raise ValueError(f"counter_specs at {path}:{node.lineno} is not a literal sequence")
        for item in node.value.elts:
            if not isinstance(item, (ast.List, ast.Tuple)) or not item.elts:
                raise ValueError(f"counter_specs at {path}:{node.lineno} has a non-tuple member")
            name_node = item.elts[0]
            if not isinstance(name_node, ast.Constant) or not isinstance(name_node.value, str) or not METRIC_NAME.fullmatch(name_node.value):
                raise ValueError(f"counter_specs at {path}:{name_node.lineno} has a dynamic metric name")
            result.append(_registration(name_node.value, "counter", path, item))
    return result


def _local_named_constructor_registrations(path: str, tree: ast.AST) -> list[Registration]:
    result: list[Registration] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local_names = {
            value.value
            for node in ast.walk(function)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            for value in [node.value]
            if isinstance(target, ast.Name)
            and target.id == "name"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and METRIC_NAME.fullmatch(value.value)
        }
        if len(local_names) > 1:
            raise ValueError(f"function {function.name} at {path}:{function.lineno} assigns multiple metric names")
        if not local_names:
            continue
        name = next(iter(local_names))
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            kind = CONSTRUCTORS.get(_terminal_name(call.func))
            name_values = [keyword.value for keyword in call.keywords if keyword.arg == "name"]
            if kind is not None and len(name_values) == 1 and isinstance(name_values[0], ast.Name) and name_values[0].id == "name":
                result.append(_registration(name, kind, path, call))
    return result


def _registration(name: str, kind: str, path: str, node: ast.AST) -> Registration:
    family = name + "_total" if kind == "counter" and not name.endswith("_total") else name
    return Registration(
        declared_name=name,
        family=family,
        prometheus_type=kind,
        location=SourceLocation(path, node.lineno, node.end_lineno or node.lineno),
    )


def _deduplicate_registrations(registrations: list[Registration]) -> list[Registration]:
    by_family: dict[str, Registration] = {}
    for registration in registrations:
        previous = by_family.get(registration.family)
        if previous is not None:
            if previous.prometheus_type != registration.prometheus_type:
                raise ValueError(
                    f"family {registration.family!r} has both {previous.prometheus_type} and {registration.prometheus_type} registrations"
                )
            continue
        by_family[registration.family] = registration
    return [by_family[family] for family in sorted(by_family)]


def parse_created_emitters(source: str) -> dict[str, tuple[int, int]]:
    return parse_client_created_emitters(source, ("Counter", "Histogram"))


def validate_ray_transport(wrapper: str, agent: str, tags: str) -> None:
    wrapper_tree = ast.parse(wrapper)
    required_classes = {"RayGaugeWrapper", "RayCounterWrapper", "RayHistogramWrapper", "RayPrometheusStatLogger"}
    classes = {node.name for node in wrapper_tree.body if isinstance(node, ast.ClassDef)}
    if not required_classes <= classes:
        raise ValueError(f"Ray wrapper is missing required classes: {sorted(required_classes - classes)}")
    for token in ("_get_sanitized_opentelemetry_name", 'labels.append("ReplicaId")'):
        if token not in wrapper:
            raise ValueError(f"Ray wrapper no longer proves {token}")
    for token in ("RAY_EXPORT_COUNTER_AS_GAUGE", "CounterMetricFamily", "GaugeMetricFamily"):
        if token not in agent:
            raise ValueError(f"Ray metrics agent no longer proves {token}")
    for token in ("Component", "Version", "WorkerId", "SessionName"):
        if token not in tags:
            raise ValueError(f"Ray tag registry no longer defines {token}")


def generate_registry(
    sources: dict[str, str],
    client_source: str,
    ray_wrapper: str,
    ray_agent: str,
    ray_tags: str,
) -> str:
    native = parse_registrations(sources)
    created_emitters = parse_created_emitters(client_source)
    validate_ray_transport(ray_wrapper, ray_agent, ray_tags)
    ray_native = [registration for registration in native if _ray_supported(registration)]
    ray_counters = [
        registration
        for registration in ray_native
        if registration.prometheus_type == "counter" and not registration.declared_name.endswith("_total")
    ]
    created = [registration for registration in native if registration.prometheus_type in {"counter", "histogram"}]
    if not ray_native or not ray_counters or not created:
        raise ValueError("derived vLLM registry classes must all be nonempty")

    canonical_created = min((_created_family(registration) for registration in created), key=lambda value: (len(value), value))
    canonical_prefix = canonical_created[: -len("_created")]
    lines = [
        "# SPDX-License-Identifier: GPL-3.0-or-later",
        "",
        "version: v1",
        "profile: vllm",
        "generated: true",
        "family_grammars:",
        "  python_created_family:",
        "    forms:",
        "      generated:",
        f"        canonical: {{prefix: {canonical_prefix}, suffix: _created}}",
        "        embedded:",
        "          prefix: 'vllm:'",
        "          suffix: created",
        "          separator: _",
        "          identity_slot: {name: instrument, nonempty: true}",
        "groups:",
        "  native:",
        "    registrations:",
    ]
    for registration in native:
        lines.extend(
            _render_registration(
                "native",
                registration.family,
                registration,
                [registration.location],
                transport="native",
            )
        )
    lines.extend(["  ray_canonical:", "    registrations:"])
    for registration in ray_native:
        ray_family = "ray_" + registration.family.replace(":", "_")
        lines.extend(
            _render_registration(
                "ray",
                ray_family,
                registration,
                [
                    registration.location,
                    SourceLocation(RAY_WRAPPER_PATH, 31, 216),
                    SourceLocation("python/ray/_private/metrics_agent.py", 382, 491),
                ],
                transport="ray",
            )
        )
    lines.extend(["  ray_compatibility_aliases:", "    registrations:"])
    for registration in ray_counters:
        canonical = "ray_" + registration.family.replace(":", "_")
        alias = canonical[: -len("_total")]
        compatibility = _ray_compatibility_registration(registration)
        lines.extend(
            _render_registration(
                "ray_alias",
                alias,
                compatibility,
                [SourceLocation("python/ray/_private/metrics_agent.py", 403, 446)],
                transport="ray",
            )
        )
    lines.extend(_render_created_registration(created, created_emitters))
    return "\n".join(lines) + "\n"


def _ray_compatibility_registration(registration: Registration) -> Registration:
    return Registration(
        registration.declared_name,
        registration.family,
        "gauge",
        registration.location,
    )


def _render_registration(
    id_prefix: str,
    family: str,
    registration: Registration,
    locations: list[SourceLocation],
    *,
    transport: str,
) -> list[str]:
    result = [
        f"      {id_prefix}_{_registration_id(family)}:",
        f"        family: {{exact: {family}}}",
        f"        prometheus: {{type: {registration.prometheus_type}, shape: {registration.shape}}}",
    ]
    result.extend(_render_when(transport, _registration_capability(registration)))
    result.append("        components:")
    for component, wire_role in registration.components:
        result.append(f"          {component}: {{wire_role: {wire_role}}}")
    result.append("        source_locations:")
    for location in locations:
        upstream = "ray" if location.path.startswith(("python/ray/", "src/ray/")) else "vllm"
        result.extend(
            [
                f"          - upstream: {upstream}",
                f"            path: {location.path}",
                f"            range: {{start: {location.start}, end: {location.end}}}",
            ]
        )
    return result


def _render_when(transport: str, capability: str | None) -> list[str]:
    result = [
        "        when:",
        "          any:",
        "            - all:",
        f"                - {{axis: transport, op: eq, value: {transport}}}",
    ]
    if capability is not None:
        result.append(f"                - {{axis: capabilities, op: contains, value: {capability}}}")
    return result


def _registration_capability(registration: Registration) -> str | None:
    path = registration.location.path
    if path in CAPABILITY_BY_REGISTRATION_PATH:
        return CAPABILITY_BY_REGISTRATION_PATH[path]
    if path == "vllm/v1/spec_decode/metrics.py":
        return "diffusion_decoding" if registration.family.startswith("vllm:diffusion_") else "speculative_decoding"
    if path == "vllm/v1/metrics/loggers.py" and registration.family.startswith("vllm:kv_block_"):
        return "kv_residency"
    if path in CORE_REGISTRATION_PATHS:
        return None
    raise ValueError(f"registration {registration.family!r} has no mechanical availability classification for {path}")


def _render_created_registration(
    registrations: list[Registration],
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
        "            - all:",
        "                - {axis: transport, op: eq, value: native}",
        "        components:",
        "          value: {wire_role: scalar}",
        "        source_locations:",
    ]
    for registration in registrations:
        result.extend(
            [
                "          - upstream: vllm",
                f"            path: {registration.location.path}",
                f"            range: {{start: {registration.location.start}, end: {registration.location.end}}}",
            ]
        )
    for class_name in ("Counter", "Histogram"):
        start, end = emitters[class_name]
        result.extend(
            [
                "          - upstream: prometheus_client_python",
                "            path: prometheus_client/metrics.py",
                f"            range: {{start: {start}, end: {end}}}",
            ]
        )
    return result


def _terminal_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return ""


def _ray_supported(registration: Registration) -> bool:
    return not registration.location.path.startswith(RAY_UNSUPPORTED_PATH_PREFIXES)


def _created_family(registration: Registration) -> str:
    family = registration.family
    if registration.prometheus_type == "counter" and family.endswith("_total"):
        family = family[: -len("_total")]
    return family + "_created"


def _registration_id(family: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", family.lower()).strip("_")
    if not value or not value[0].isalpha():
        raise ValueError(f"family {family!r} cannot form a registration ID")
    return value


def main() -> None:
    sources = {path: (VLLM_ROOT / path).read_text(encoding="utf-8") for path in REGISTRATION_PATHS}
    print(
        generate_registry(
            sources,
            CLIENT_SOURCE.read_text(encoding="utf-8"),
            (VLLM_ROOT / RAY_WRAPPER_PATH).read_text(encoding="utf-8"),
            RAY_AGENT_SOURCE.read_text(encoding="utf-8"),
            RAY_TAG_SOURCE.read_text(encoding="utf-8"),
        ),
        end="",
    )


if __name__ == "__main__":
    main()
