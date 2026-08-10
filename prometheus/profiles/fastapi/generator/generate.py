#!/usr/bin/env python3
"""Generate FastAPI's mechanical Prometheus source registry."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from source_registry_client_python import parse_created_emitters


FASTAPI_SOURCE = Path(
    "upstreams/fastapi_instrumentator/"
    "src/prometheus_fastapi_instrumentator/metrics.py"
)
CLIENT_SOURCE = Path("upstreams/prometheus_client_python/prometheus_client/metrics.py")


@dataclass(frozen=True)
class MetricRegistration:
    family: str
    prometheus_type: str
    shape: str
    components: tuple[tuple[str, str], ...]
    line_start: int
    line_end: int
    emits_created: bool


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
            )
        )

    if not registrations:
        raise ValueError("default function registers no supported metrics")
    families = [registration.family for registration in registrations]
    if len(families) != len(set(families)):
        raise ValueError("default function contains duplicate metric family registrations")
    return sorted(registrations, key=lambda registration: registration.family)


def generate_registry(fastapi_source: str, client_source: str) -> str:
    registrations = parse_default_metrics(fastapi_source)
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
    return "\n".join(lines) + "\n"


def _render_registration(registration: MetricRegistration) -> list[str]:
    registration_id = _registration_id(registration.family)
    result = [
        f"      {registration_id}:",
        f"        family: {{exact: {registration.family}}}",
        "        prometheus: "
        f"{{type: {registration.prometheus_type}, shape: {registration.shape}}}",
        "        components:",
    ]
    for component_id, wire_role in registration.components:
        result.append(f"          {component_id}: {{wire_role: {wire_role}}}")
    result.extend(
        [
            "        source_locations:",
            "          - upstream: fastapi_instrumentator",
            "            path: src/prometheus_fastapi_instrumentator/metrics.py",
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
            CLIENT_SOURCE.read_text(encoding="utf-8"),
        ),
        end="",
    )


if __name__ == "__main__":
    main()
