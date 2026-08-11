#!/usr/bin/env python3
"""Generate FastAPI's mechanical Prometheus source registry."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from source_registry_client_python import ast_fingerprint, parse_created_emitters


FASTAPI_SOURCE = Path(
    "upstreams/fastapi_instrumentator/"
    "src/prometheus_fastapi_instrumentator/metrics.py"
)
MIDDLEWARE_SOURCE = Path(
    "upstreams/fastapi_instrumentator/"
    "src/prometheus_fastapi_instrumentator/middleware.py"
)
CLIENT_SOURCE = Path("upstreams/prometheus_client_python/prometheus_client/metrics.py")

FASTAPI_METRICS_AST_FINGERPRINT = "ac64e70eeea8ccd77c118997571bc86c1fc0214a0827b099a68448a0b7d6e69e"
FASTAPI_MIDDLEWARE_AST_FINGERPRINT = "68a4fa3ca22ffb170328fe1c757a14ba1cb6d51f243627c2cf6a8f845f1b5e4e"


@dataclass(frozen=True)
class MetricRegistration:
    family: str
    prometheus_type: str
    shape: str
    components: tuple[tuple[str, str], ...]
    line_start: int
    line_end: int
    emits_created: bool
    source_path: str


CONSTRUCTORS = {
    "Counter": ("counter", "scalar", (("value", "scalar"),), True),
    "Gauge": ("gauge", "scalar", (("value", "scalar"),), False),
    "Summary": (
        "summary",
        "summary",
        (("count", "summary_count"), ("sum", "summary_sum")),
        True,
    ),
    "Histogram": (
        "histogram",
        "histogram",
        (
            ("bucket", "histogram_bucket"),
            ("count", "histogram_count"),
            ("sum", "histogram_sum"),
        ),
        True,
    ),
    "Info": ("gauge", "info", (("value", "scalar"),), False),
    "Enum": ("gauge", "stateset", (("value", "scalar"),), False),
}


def parse_default_metrics(source: str) -> list[MetricRegistration]:
    _require_reviewed_source_shape(
        source,
        FASTAPI_METRICS_AST_FINGERPRINT,
        "FastAPI metrics module",
    )
    tree = ast.parse(source)
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "default"
    ]
    if len(functions) != 1:
        raise ValueError(f"expected exactly one default function, found {len(functions)}")

    registrations: list[MetricRegistration] = []
    for node in ast.walk(functions[0]):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in CONSTRUCTORS:
            continue
        name_keywords = [keyword for keyword in node.keywords if keyword.arg == "name"]
        if len(name_keywords) != 1:
            raise ValueError(f"{node.func.id} at line {node.lineno} must have one name keyword")
        family = _literal_string(name_keywords[0].value, f"{node.func.id} name at line {node.lineno}")
        if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", family):
            raise ValueError(f"unsupported metric family {family!r} at line {node.lineno}")
        prometheus_type, shape, components, emits_created = CONSTRUCTORS[node.func.id]
        registrations.append(
            MetricRegistration(
                family=family,
                prometheus_type=prometheus_type,
                shape=shape,
                components=components,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                emits_created=emits_created,
                source_path="src/prometheus_fastapi_instrumentator/metrics.py",
            )
        )

    if not registrations:
        raise ValueError("default function registers no supported metrics")
    families = [registration.family for registration in registrations]
    if len(families) != len(set(families)):
        raise ValueError("default function contains duplicate metric family registrations")
    return sorted(registrations, key=lambda registration: registration.family)


def parse_inprogress_metric(source: str) -> MetricRegistration:
    _require_reviewed_source_shape(
        source,
        FASTAPI_MIDDLEWARE_AST_FINGERPRINT,
        "FastAPI middleware module",
    )
    tree = ast.parse(source)
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "PrometheusInstrumentatorMiddleware"
    ]
    if len(classes) != 1:
        raise ValueError(f"expected exactly one middleware class, found {len(classes)}")
    initializers = [
        node
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__init__"
    ]
    if len(initializers) != 1:
        raise ValueError(f"expected exactly one middleware initializer, found {len(initializers)}")
    initializer = initializers[0]
    defaults = {
        argument.arg: default
        for argument, default in zip(initializer.args.kwonlyargs, initializer.args.kw_defaults)
        if default is not None
    }
    if _literal_value(defaults.get("should_instrument_requests_inprogress")) is not False:
        raise ValueError("in-progress instrumentation must default to disabled")
    if _literal_value(defaults.get("inprogress_labels")) is not False:
        raise ValueError("in-progress labels must default to disabled")
    family = _literal_string(defaults.get("inprogress_name"), "inprogress_name default")
    if family != "http_requests_inprogress":
        raise ValueError(f"unexpected inprogress_name default {family!r}")

    registrations = []
    for node in ast.walk(initializer):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "Gauge":
            continue
        name_keywords = [keyword for keyword in node.keywords if keyword.arg == "name"]
        if len(name_keywords) != 1:
            continue
        name = name_keywords[0].value
        if (
            isinstance(name, ast.Attribute)
            and isinstance(name.value, ast.Name)
            and name.value.id == "self"
            and name.attr == "inprogress_name"
        ):
            registrations.append(node)
    if len(registrations) != 1:
        raise ValueError(f"expected exactly one in-progress Gauge registration, found {len(registrations)}")
    registration = registrations[0]
    return MetricRegistration(
        family=family,
        prometheus_type="gauge",
        shape="scalar",
        components=(("value", "scalar"),),
        line_start=registration.lineno,
        line_end=registration.end_lineno or registration.lineno,
        emits_created=False,
        source_path="src/prometheus_fastapi_instrumentator/middleware.py",
    )


def _require_reviewed_source_shape(
    source: str,
    expected: str,
    description: str,
) -> None:
    actual = ast_fingerprint(source)
    if actual != expected:
        raise ValueError(
            f"{description} source shape fingerprint {actual} does not match "
            f"reviewed fingerprint {expected}"
        )


def generate_registry(fastapi_source: str, middleware_source: str, client_source: str) -> str:
    registrations = parse_default_metrics(fastapi_source)
    inprogress = parse_inprogress_metric(middleware_source)
    created_emitters = parse_created_emitters(client_source)
    created = [registration for registration in registrations if registration.emits_created]
    if not created:
        raise ValueError("default function has no registrations with generated _created samples")

    namespace_prefix = _single_token_namespace([registration.family for registration in created])
    created_families = [
        _created_family(registration.family, registration.prometheus_type)
        for registration in created
    ]
    canonical = min(created_families, key=lambda family: (len(family), family))
    canonical_prefix = canonical[: -len("_created")]

    lines = [
        "# SPDX-License-Identifier: GPL-3.0-or-later",
        "",
        "version: v1",
        "profile: fastapi",
        "generated: true",
        "family_grammars:",
        "  python_created_family:",
        "    forms:",
        "      generated:",
        f"        canonical: {{prefix: {canonical_prefix}, suffix: _created}}",
        "        embedded:",
        f"          prefix: {namespace_prefix}",
        "          suffix: created",
        "          separator: _",
        "          identity_slot: {name: instrument, nonempty: true}",
        "groups:",
        "  default_http:",
        "    registrations:",
    ]
    for registration in registrations:
        lines.extend(_render_registration(registration))
    lines.extend(_render_created_registration(created, created_emitters))
    lines.extend(
        [
            "  middleware_http:",
            "    registrations:",
        ]
    )
    lines.extend(_render_registration(inprogress, when=("inprogress", ("unlabeled", "labeled"))))
    return "\n".join(lines) + "\n"


def _render_registration(
    registration: MetricRegistration,
    when: tuple[str, tuple[str, ...]] | None = None,
) -> list[str]:
    registration_id = _registration_id(registration.family)
    result = [
        f"      {registration_id}:",
        f"        family: {{exact: {registration.family}}}",
    ]
    if when:
        axis, values = when
        result.extend(
            [
                "        when:",
                "          any:",
                "            - all:",
                f"                - {{axis: {axis}, op: in, values: [{', '.join(values)}]}}",
            ]
        )
    result.extend(
        [
            "        prometheus: "
            f"{{type: {registration.prometheus_type}, shape: {registration.shape}}}",
            "        components:",
        ]
    )
    for component_id, wire_role in registration.components:
        result.append(f"          {component_id}: {{wire_role: {wire_role}}}")
    result.extend(
        [
            "        source_locations:",
            "          - upstream: fastapi_instrumentator",
            f"            path: {registration.source_path}",
            "            range: "
            f"{{start: {registration.line_start}, end: {registration.line_end}}}",
        ]
    )
    return result


def _render_created_registration(
    registrations: list[MetricRegistration],
    emitters: dict[str, tuple[int, int]],
) -> list[str]:
    result = [
        "      python_created_component:",
        "        family: {grammar: python_created_family, form: generated}",
        "        raw_branches: {canonical: {}, embedded: {}}",
        "        prometheus: {type: gauge, shape: scalar}",
        "        components:",
        "          value: {wire_role: scalar}",
        "        source_locations:",
    ]
    for registration in registrations:
        result.extend(
            [
                "          - upstream: fastapi_instrumentator",
                "            path: src/prometheus_fastapi_instrumentator/metrics.py",
                "            range: "
                f"{{start: {registration.line_start}, end: {registration.line_end}}}",
            ]
        )
    for class_name in ("Counter", "Summary", "Histogram"):
        line_start, line_end = emitters[class_name]
        result.extend(
            [
                "          - upstream: prometheus_client_python",
                "            path: prometheus_client/metrics.py",
                f"            range: {{start: {line_start}, end: {line_end}}}",
            ]
        )
    return result


def _literal_string(node: ast.AST, field: str) -> str:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or not node.value:
        raise ValueError(f"{field} must be a nonempty string literal")
    return node.value


def _literal_value(node: ast.AST | None) -> object:
    if not isinstance(node, ast.Constant):
        raise ValueError("middleware default must be a literal")
    return node.value


def _single_token_namespace(families: list[str]) -> str:
    first_tokens = {family.split("_", 1)[0] for family in families}
    if len(first_tokens) != 1 or any("_" not in family for family in families):
        raise ValueError(f"created families do not share one token namespace: {families}")
    return first_tokens.pop() + "_"


def _created_family(family: str, prometheus_type: str) -> str:
    if prometheus_type == "counter" and family.endswith("_total"):
        family = family[: -len("_total")]
    return family + "_created"


def _registration_id(family: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", family.lower()).strip("_")
    if not value or not value[0].isalpha():
        raise ValueError(f"family {family!r} cannot form a registration ID")
    return value


def main() -> None:
    print(
        generate_registry(
            FASTAPI_SOURCE.read_text(encoding="utf-8"),
            MIDDLEWARE_SOURCE.read_text(encoding="utf-8"),
            CLIENT_SOURCE.read_text(encoding="utf-8"),
        ),
        end="",
    )


if __name__ == "__main__":
    main()
