#!/usr/bin/env python3
"""Generate HAProxy PromEx's default mechanical metric registry."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


UPSTREAM = Path("upstreams/haproxy")
STATS_SOURCE = UPSTREAM / "src/stats.c"
PROXY_SOURCE = UPSTREAM / "src/stats-proxy.c"
RESOLVER_SOURCE = UPSTREAM / "src/resolvers.c"
STICKTABLE_SOURCE = UPSTREAM / "src/stick_table.c"
PROMEX_SOURCE = UPSTREAM / "addons/promex/service-prometheus.c"


@dataclass(frozen=True)
class Location:
    path: str
    line: int


@dataclass(frozen=True)
class Registration:
    family: str
    prometheus_type: str
    shape: str
    locations: tuple[Location, ...]


SCOPE_CAPABILITIES = {
    "frontend": "F",
    "listener": "L",
    "backend": "B",
    "server": "S",
}

EXPECTED_GROUP_COUNTS = {
    "process": 63,
    "frontend": 29,
    "listener": 15,
    "backend": 51,
    "server": 56,
    "resolver": 15,
    "sticktable": 2,
}

def source_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def unique_line(lines: list[str], pattern: str, description: str) -> int:
    expression = re.compile(pattern)
    matches = [number for number, line in enumerate(lines, 1) if expression.search(line)]
    if len(matches) != 1:
        raise ValueError(f"{description} must match exactly one line, found {matches}")
    return matches[0]


def function_body(source: str, signature: str) -> str:
    start = source.find(signature)
    if start == -1:
        raise ValueError(f"missing function {signature!r}")
    opening = source.find("{", start)
    if opening == -1:
        raise ValueError(f"function {signature!r} has no body")
    depth = 0
    for index in range(opening, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise ValueError(f"function {signature!r} has an unterminated body")


def parse_process_registrations(stats_source: str, promex_source: str) -> list[Registration]:
    stats = stats_source.splitlines()
    promex = promex_source.splitlines()
    entries: dict[str, tuple[str, int]] = {}
    for number, line in enumerate(stats, 1):
        enum = re.search(r"\[(ST_I_INF_[A-Z0-9_]+)\]", line)
        if enum is None:
            continue
        alt_name = re.search(r'\.alt_name\s*=\s*"([^"]+)"', line)
        if alt_name is None:
            continue
        if enum.group(1) in entries:
            raise ValueError(f"duplicate process field {enum.group(1)}")
        entries[enum.group(1)] = (alt_name.group(1), number)
    if not entries:
        raise ValueError("stat_cols_info contains no PromEx fields")

    fill_lines: dict[str, tuple[str, int]] = {}
    for number, line in enumerate(stats, 1):
        match = re.search(r"line\[(ST_I_INF_[A-Z0-9_]+)\]\s*=\s*(.+);", line)
        if match:
            if match.group(1) in fill_lines:
                raise ValueError(f"duplicate process fill for {match.group(1)}")
            fill_lines[match.group(1)] = (match.group(2), number)

    type_body = function_body(promex_source, "promex_global_gettype")
    forced_counters = set(re.findall(r"case\s+(ST_I_INF_[A-Z0-9_]+)\s*:", type_body))
    if not forced_counters:
        raise ValueError("promex_global_gettype has no historical counter overrides")
    prefix_line = unique_line(promex, r'prefix\s*=\s*IST\("haproxy_process_"\)', "process prefix")

    result: list[Registration] = []
    for enum, (suffix, registration_line) in sorted(entries.items(), key=lambda item: item[1][0]):
        if enum not in fill_lines:
            raise ValueError(f"process field {enum} has no stats_fill_info assignment")
        expression, fill_line = fill_lines[enum]
        prometheus_type = "counter" if enum in forced_counters or "FN_COUNTER" in expression else "gauge"
        family = f"haproxy_process_{suffix}"
        result.append(
            Registration(
                family,
                prometheus_type,
                "info" if family.endswith("_info") and prometheus_type == "gauge" else "scalar",
                (
                    Location("src/stats.c", registration_line),
                    Location("src/stats.c", fill_line),
                    Location("addons/promex/service-prometheus.c", prefix_line),
                ),
            )
        )
    return result


def parse_proxy_overrides(promex_source: str, scope: str) -> dict[str, str]:
    array_name = {
        "frontend": "promex_st_front_metrics_names",
        "listener": "promex_st_li_metrics_names",
        "backend": "promex_st_back_metrics_names",
        "server": "promex_st_srv_metrics_names",
    }[scope]
    match = re.search(
        rf"const struct ist {array_name}\[ST_I_PX_MAX\]\s*=\s*\{{(?P<body>.*?)\n\}};",
        promex_source,
        re.DOTALL,
    )
    if match is None:
        raise ValueError(f"missing {array_name}")
    overrides: dict[str, str] = {}
    for enum, name in re.findall(r'\[(ST_I_PX_[A-Z0-9_]+)\]\s*=\s*IST\("([^"]+)"\)', match.group("body")):
        if enum in overrides:
            raise ValueError(f"duplicate {scope} override for {enum}")
        overrides[enum] = name
    return overrides


def parse_proxy_fill_types(proxy_source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(
        r"case\s+(ST_I_PX_[A-Z0-9_]+)\s*:(?P<body>.*?)(?=\n\s*(?:case\s+ST_I_PX_|default:))",
        proxy_source,
        re.DOTALL,
    ):
        enum = match.group(1)
        current = "counter" if "FN_COUNTER" in match.group("body") else "gauge"
        previous = result.get(enum)
        if previous is not None and previous != current:
            raise ValueError(f"proxy field {enum} has inconsistent fill natures {previous} and {current}")
        result[enum] = current
    return result


def parse_proxy_registrations(proxy_source: str, promex_source: str) -> dict[str, list[Registration]]:
    proxy = proxy_source.splitlines()
    promex = promex_source.splitlines()
    fill_types = parse_proxy_fill_types(proxy_source)
    overrides = {scope: parse_proxy_overrides(promex_source, scope) for scope in SCOPE_CAPABILITIES}
    prefix_lines = {
        scope: unique_line(
            promex,
            rf'prefix\s*=\s*IST\("haproxy_{scope}_"\)',
            f"{scope} prefix",
        )
        for scope in SCOPE_CAPABILITIES
    }

    grouped: dict[str, dict[str, Registration]] = {scope: {} for scope in SCOPE_CAPABILITIES}
    for number, line in enumerate(proxy, 1):
        enum_match = re.search(r"\[(ST_I_PX_[A-Z0-9_]+)\s*\]", line)
        if enum_match is None:
            continue
        enum = enum_match.group(1)
        alt_name = re.search(r'\.alt_name\s*=\s*"([^"]+)"', line)
        if alt_name is None:
            alt_name = re.search(r'ME_NEW_[A-Z]+\([^,]+,\s*"([^"]+)"', line)
        capability = re.search(r"STATS_PX_CAP_([_LFBS]{4})", line)
        explicitly_unexported = re.search(r"\.alt_name\s*=\s*NULL", line) or re.search(
            r"ME_NEW_[A-Z]+\([^,]+,\s*NULL", line
        )
        if explicitly_unexported:
            continue
        if alt_name is None and capability is None:
            continue
        if alt_name is None or capability is None:
            raise ValueError(f"partially declared proxy PromEx field on line {number}")
        generic = "ME_NEW_" in line
        if generic:
            prometheus_type = "counter" if "FN_COUNTER" in line else "gauge"
        else:
            if enum not in fill_types:
                raise ValueError(f"non-generic proxy field {enum} has no supported fill case")
            prometheus_type = fill_types[enum]

        for scope, marker in SCOPE_CAPABILITIES.items():
            if marker not in capability.group(1):
                continue
            suffix = overrides[scope].get(enum, alt_name.group(1))
            family = f"haproxy_{scope}_{suffix}"
            location = Location("src/stats-proxy.c", number)
            existing = grouped[scope].get(family)
            if existing is None:
                grouped[scope][family] = Registration(
                    family,
                    prometheus_type,
                    "scalar",
                    (location, Location("addons/promex/service-prometheus.c", prefix_lines[scope])),
                )
            else:
                if existing.prometheus_type != prometheus_type:
                    raise ValueError(f"family {family} combines incompatible Prometheus types")
                grouped[scope][family] = Registration(
                    family,
                    prometheus_type,
                    "scalar",
                    tuple(sorted(set(existing.locations + (location,)), key=lambda item: (item.path, item.line))),
                )
    return {scope: sorted(values.values(), key=lambda item: item.family) for scope, values in grouped.items()}


def parse_resolver_registrations(resolver_source: str) -> list[Registration]:
    lines = resolver_source.splitlines()
    metric_type_line = unique_line(
        lines,
        r"PROMEX_MT_GAUGE,\s*\.flags\s*=\s*PROMEX_FL_MODULE_METRIC",
        "resolver PromEx metric type",
    )
    result: list[Registration] = []
    for number, line in enumerate(lines, 1):
        match = re.search(
            r'\[(RSLV_STAT_[A-Z0-9_]+)\]\s*=\s*\{\s*\.name\s*=\s*"([^"]+)"',
            line,
        )
        if match is None or match.group(2) in {"id", "pid"}:
            continue
        result.append(
            Registration(
                f"haproxy_resolver_{match.group(2)}",
                "gauge",
                "scalar",
                (Location("src/resolvers.c", number), Location("src/resolvers.c", metric_type_line)),
            )
        )
    if not result:
        raise ValueError("resolver module exposes no metrics")
    return sorted(result, key=lambda item: item.family)


def parse_sticktable_registrations(sticktable_source: str) -> list[Registration]:
    lines = sticktable_source.splitlines()
    result: list[Registration] = []
    for index, line in enumerate(lines):
        case = re.search(r"case\s+(STICKTABLE_[A-Z0-9_]+)\s*:", line)
        if case is None:
            continue
        block = "\n".join(lines[index : index + 8])
        name = re.search(r'\.n\s*=\s*ist\("([^"]+)"\)', block)
        if name is None:
            continue
        type_match = re.search(r"\.type\s*=\s*(PROMEX_MT_[A-Z]+)", block)
        if type_match is None:
            raise ValueError(f"stick-table metric {case.group(1)} has no explicit PromEx type")
        if type_match.group(1) != "PROMEX_MT_GAUGE":
            raise ValueError(f"unsupported stick-table PromEx type {type_match.group(1)}")
        result.append(
            Registration(
                f"haproxy_sticktable_{name.group(1)}",
                "gauge",
                "scalar",
                (Location("src/stick_table.c", index + 1),),
            )
        )
    if not result:
        raise ValueError("stick-table module exposes no metrics")
    return sorted(result, key=lambda item: item.family)


def generate_registry(
    stats_source: str,
    proxy_source: str,
    resolver_source: str,
    sticktable_source: str,
    promex_source: str,
) -> str:
    groups: dict[str, list[Registration]] = {
        "process": parse_process_registrations(stats_source, promex_source),
        **parse_proxy_registrations(proxy_source, promex_source),
        "resolver": parse_resolver_registrations(resolver_source),
        "sticktable": parse_sticktable_registrations(sticktable_source),
    }
    counts = {group: len(registrations) for group, registrations in groups.items()}
    if counts != EXPECTED_GROUP_COUNTS:
        raise ValueError(f"HAProxy 3.2 default registry counts changed: {counts}")
    families = [registration.family for registrations in groups.values() for registration in registrations]
    if len(families) != len(set(families)):
        raise ValueError("HAProxy default registry contains duplicate families")

    lines = [
        "# SPDX-License-Identifier: GPL-3.0-or-later",
        "",
        "version: v1",
        "profile: haproxy",
        "generated: true",
        "family_grammars: {}",
        "groups:",
    ]
    for group in ("process", "frontend", "listener", "backend", "server", "resolver", "sticktable"):
        lines.extend(render_group(group, groups[group]))
    return "\n".join(lines) + "\n"


def render_group(group: str, registrations: list[Registration]) -> list[str]:
    lines = [f"  {group}:", "    registrations:"]
    for registration in registrations:
        lines.extend(
            [
                f"      {registration_id(registration.family)}:",
                f"        family: {{exact: {registration.family}}}",
                f"        prometheus: {{type: {registration.prometheus_type}, shape: {registration.shape}}}",
                "        components:",
                "          value: {wire_role: scalar}",
                "        source_locations:",
            ]
        )
        for location in registration.locations:
            lines.extend(
                [
                    "          - upstream: haproxy",
                    f"            path: {location.path}",
                    f"            line: {location.line}",
                ]
            )
    return lines


def registration_id(family: str) -> str:
    result = family.removeprefix("haproxy_")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", result):
        raise ValueError(f"family {family!r} does not produce a valid registration ID")
    return result


def main() -> None:
    print(
        generate_registry(
            STATS_SOURCE.read_text(encoding="utf-8"),
            PROXY_SOURCE.read_text(encoding="utf-8"),
            RESOLVER_SOURCE.read_text(encoding="utf-8"),
            STICKTABLE_SOURCE.read_text(encoding="utf-8"),
            PROMEX_SOURCE.read_text(encoding="utf-8"),
        ),
        end="",
    )


if __name__ == "__main__":
    main()
