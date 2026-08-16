#!/usr/bin/env python3
"""Generate Ceph's mechanical Prometheus source registry from pinned releases."""

from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path


RELEASES = ("reef", "squid", "tentacle")
SOURCE_VARIANTS = (*RELEASES, "nvmeof")
SOURCE_ROOTS = {
    **{release: Path("upstreams") / f"ceph_{release}" for release in RELEASES},
    "nvmeof": Path("upstreams/ceph_nvmeof"),
    "prometheus_client_python": Path("upstreams/prometheus_client_python"),
}
AVERAGE_METHODS = {"time_avg", "u64_avg"}
IGNORED_METHODS = {"u64_counter_histogram"}
SUPPORTED_METHODS = {"time", "time_avg", "u64", "u64_avg", "u64_counter"}
CONFIG_PRIORITY_SOURCES = {
    "cephfs_mirror_perf_stats_prio": "src/common/options/cephfs-mirror.yaml.in",
    "rbd_mirror_perf_stats_prio": "src/common/options/rbd-mirror.yaml.in",
    "rbd_mirror_image_perf_stats_prio": "src/common/options/rbd-mirror.yaml.in",
}


@dataclass(frozen=True)
class Grammar:
    canonical_prefix: str
    embedded_prefix: str
    identity: str
    excluded_prefixes: tuple[str, ...] = ()
    terminal_identity: bool = False


@dataclass(frozen=True)
class Registration:
    source_variant: str
    source_path: str
    line_start: int
    line_end: int
    group: str
    grammar: str | None
    form: str
    prometheus_type: str
    priority: int
    shape: str = "scalar"
    endpoint: str = "daemon_perf"
    classification: str | None = None
    exact_family_override: str | None = None
    extra_locations: tuple[tuple[str, int, int], ...] = ()
    dependency_locations: tuple[tuple[str, str, int, int], ...] = ()


@dataclass(frozen=True)
class MergedRegistration:
    exact_family: str | None
    grammar: str | None
    form: str
    prometheus_type: str
    shape: str
    priority: int
    endpoint: str
    classification: str | None
    source_variants: tuple[str, ...]
    locations: tuple[tuple[str, str, int, int], ...]


GRAMMARS = {
    "librbd_image": Grammar(
        "ceph_rbd_librbd_image_",
        "ceph_librbd_",
        "librbd_image_key",
        excluded_prefixes=("ceph_librbd_pwl_",),
    ),
    "librbd_pwl": Grammar("ceph_rbd_librbd_pwl_", "ceph_librbd_pwl_", "librbd_pwl_key"),
    "throttle": Grammar("ceph_throttle_", "ceph_throttle_", "throttle_key"),
    "finisher": Grammar("ceph_finisher_", "ceph_finisher_", "finisher_key"),
    "objectcacher": Grammar("ceph_objectcacher_", "ceph_objectcacher_", "objectcacher_key"),
    "kernel_device": Grammar("ceph_kernel_device_", "ceph_blk_kernel_device_", "kernel_device_key"),
    "mclock_shard": Grammar("ceph_mclock_shard_", "ceph_mclock_shard_queue_", "mclock_shard"),
    "messenger_worker": Grammar("ceph_async_messenger_worker_", "ceph_AsyncMessenger_Worker_", "messenger_worker"),
    "rdma_worker": Grammar("ceph_async_messenger_rdma_worker_", "ceph_AsyncMessenger_RDMAWorker_", "rdma_worker"),
    "dpdk_queue": Grammar("ceph_dpdk_queue_", "ceph_queue", "dpdk_queue"),
    "dpdk_port": Grammar("ceph_dpdk_port_", "ceph_port", "dpdk_port"),
    "mds_client": Grammar("ceph_mds_per_client_", "ceph_mds_client_metrics_", "mds_filesystem_key"),
    "rocksdb_cache": Grammar("ceph_rocksdb_binned_cache_", "ceph_rocksdb_cache_", "rocksdb_cache_key"),
    "objecter": Grammar("ceph_objecter_", "ceph_objecter_", "objecter_handle"),
    "priority_cache": Grammar("ceph_bluestore_priority_cache_", "ceph_bluestore_pricache:", "priority_cache"),
    "rgw_dmclock": Grammar("ceph_rgw_dmclock_", "ceph_dmclock_", "dmclock_queue"),
    "rgw_sync": Grammar("ceph_data_sync_from_zone_", "ceph_data_sync_from_", "source_zone_fragment"),
    "osd_scrub": Grammar("ceph_osd_scrub_", "ceph_osd_scrub_", "scrub_phase"),
    "striper": Grammar("ceph_rados_striper_", "ceph_", "striper"),
    "service_unique_id": Grammar(
        "ceph_service_unique_id",
        "ceph_service_unique_id_",
        "service_unique_id",
        terminal_identity=True,
    ),
}


DYNAMIC_BUILDERS = {
    "src/librbd/ImageCtx.cc": "librbd_image",
    "src/librbd/cache/pwl/AbstractWriteLog.cc": "librbd_pwl",
    "src/common/Throttle.cc": "throttle",
    "src/common/Finisher.cc": "finisher",
    "src/common/Finisher.h": "finisher",
    "src/osdc/ObjectCacher.cc": "objectcacher",
    "src/blk/kernel/KernelDevice.cc": "kernel_device",
    "src/osd/scheduler/mClockScheduler.cc": "mclock_shard",
    "src/mds/MetricAggregator.cc": "mds_client",
    "src/kv/rocksdb_cache/BinnedLRUCache.cc": "rocksdb_cache",
    "src/common/PriorityCache.cc": "priority_cache",
    "src/rgw/rgw_dmclock_scheduler_ctx.cc": "rgw_dmclock",
    "src/rgw/driver/rados/rgw_sync_counters.cc": "rgw_sync",
    "src/osd/osd_perf_counters.cc": "osd_scrub",
    "src/SimpleRADOSStriper.cc": "striper",
}


@dataclass(frozen=True)
class Builder:
    offset: int
    variable: str
    expression: str
    static_group: str | None


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def string_literals(value: str) -> list[str]:
    return re.findall(r'"((?:\\.|[^"\\])*)"', value)


def static_group(expression: str) -> str | None:
    match = re.fullmatch(r'"([^"\\]+)"', expression.strip())
    return match.group(1) if match else None


def local_string_value(text: str, offset: int, expression: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expression):
        return None
    patterns = (
        rf'(?:std::string|std::string_view|const\s+std::string|char)\s+{re.escape(expression)}(?:\[[^]]*\])?\s*=\s*"([^"\\]+)"',
        rf'{re.escape(expression)}\s*=\s*"([^"\\]+)"',
    )
    matches = [match for pattern in patterns for match in re.finditer(pattern, text[:offset])]
    if not matches:
        return None
    return max(matches, key=lambda item: item.start()).group(1)


def resolve_key_created_group(text: str, builder: Builder) -> tuple[str | None, str | None]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", builder.expression):
        return None, None
    assignments = list(
        re.finditer(
            rf"(?:std::string|auto)\s+{re.escape(builder.expression)}\s*=\s*"
            r"(?:ceph::perf_counters::)?key_create\s*\((.*?)\);",
            text[: builder.offset],
            re.DOTALL,
        )
    )
    if not assignments:
        return None, None
    argument = assignments[-1].group(1).split(",", 1)[0].strip()
    exact = static_group(argument)
    if exact is None:
        exact = local_string_value(text, assignments[-1].start(), argument)
    if exact is not None:
        return exact, None
    if "mds_client_metrics" in argument:
        return "mds_client", "mds_client"
    return None, None


def prometheus_name(value: str) -> str:
    # Keep this byte-for-byte equivalent to MGR Metric.promethize() and
    # ceph-exporter's src/exporter/util.cc promethize(). A single colon is a
    # legal Prometheus name character and is intentionally preserved.
    value = re.sub(r"[./\s]|::", "_", value).replace("+", "_plus")
    if value.endswith("-"):
        value = value[:-1] + "_minus"
    else:
        value = value.replace("-", "_")
    return "ceph_" + value


def _option_body(text: str, option: str, source_path: str) -> str:
    declaration = re.search(
        rf"(?m)^- name: {re.escape(option)}\n(?P<body>(?:^(?!- name: ).*\n?)*)",
        text,
    )
    if declaration is None:
        raise ValueError(f"{source_path} has no declaration for {option}")
    return declaration.group("body")


@cache
def validate_endpoint_contracts(root: Path) -> None:
    exporter_options_path = "src/common/options/ceph-exporter.yaml.in"
    exporter_options = (root / exporter_options_path).read_text(encoding="utf-8")
    exporter_priority = _option_body(exporter_options, "exporter_prio_limit", exporter_options_path)
    if not re.search(r"(?m)^  default: 5$", exporter_priority):
        raise ValueError(f"{exporter_options_path} exporter_prio_limit default is not 5")

    mgr_options_path = "src/common/options/mgr.yaml.in"
    mgr_options = (root / mgr_options_path).read_text(encoding="utf-8")
    mgr_priority = _option_body(mgr_options, "mgr_stats_threshold", mgr_options_path)
    for expected in (r"(?m)^  default: 5$", r"(?m)^  min: 0$", r"(?m)^  max: 11$"):
        if not re.search(expected, mgr_priority):
            raise ValueError(f"{mgr_options_path} mgr_stats_threshold contract changed")
    daemon_server_path = "src/mgr/DaemonServer.cc"
    daemon_server = (root / daemon_server_path).read_text(encoding="utf-8")
    if not re.search(
        r'configure->stats_threshold\s*=\s*g_conf\(\)\.get_val<int64_t>\("mgr_stats_threshold"\)',
        daemon_server,
    ):
        raise ValueError(f"{daemon_server_path} no longer propagates the MGR perf-priority threshold")

    exporter_path = "src/exporter/DaemonMetricCollector.cc"
    exporter = (root / exporter_path).read_text(encoding="utf-8")
    if not re.search(r'priority"\]\.as_int64\(\)\s*<\s*prio_limit', exporter):
        raise ValueError(f"{exporter_path} no longer uses an inclusive perf-priority threshold")

    mgr_module_path = "src/pybind/mgr/mgr_module.py"
    mgr_module = (root / mgr_module_path).read_text(encoding="utf-8")
    unlabeled = re.search(
        r"def get_unlabeled_perf_counters\(.*?prio_limit:\s*int\s*=\s*PRIO_USEFUL.*?"
        r"if priority < prio_limit:",
        mgr_module,
        re.DOTALL,
    )
    if unlabeled is None:
        raise ValueError(f"{mgr_module_path} MGR perf-priority filter changed")

    prometheus_path = "src/pybind/mgr/prometheus/module.py"
    prometheus = (root / prometheus_path).read_text(encoding="utf-8")
    exclude = re.search(
        r"Option\(\s*name='exclude_perf_counters'.*?default=True.*?\)",
        prometheus,
        re.DOTALL,
    )
    collect = re.search(
        r"if not self\.get_module_option\('exclude_perf_counters'\):\s*self\.get_perf_counters\(\)",
        prometheus,
    )
    if exclude is None or collect is None:
        raise ValueError(f"{prometheus_path} MGR perf-counter activation contract changed")

    util_path = "src/exporter/util.cc"
    util = (root / util_path).read_text(encoding="utf-8")
    util_checks = (
        'name[name.size() - 1] == \'-\'',
        'name += "minus"',
        "ch == '.' || ch == '/' || ch == ' ' || ch == '-'",
        'boost::replace_all(name, "::", "_")',
        'boost::replace_all(name, "+", "_plus")',
        'name = "ceph_" + name',
    )
    mgr_checks = (
        "re.sub(r'[./\\s]|::', '_', path).replace('+', '_plus')",
        'result.endswith("-")',
        'result = result.replace("-", "_")',
        'return "ceph_{0}".format(result)',
    )
    if not all(check in util for check in util_checks) or not all(check in prometheus for check in mgr_checks):
        raise ValueError("MGR and ceph-exporter metric-name normalization changed")

    perf_counters_path = "src/common/perf_counters.cc"
    perf_counters = (root / perf_counters_path).read_text(encoding="utf-8")
    constructor_contracts = {
        "add_u64_counter": r"PERFCOUNTER_U64\s*\|\s*PERFCOUNTER_COUNTER",
        "add_u64": r"PERFCOUNTER_U64",
        "add_u64_avg": r"PERFCOUNTER_U64\s*\|\s*PERFCOUNTER_LONGRUNAVG",
        "add_time": r"PERFCOUNTER_TIME",
        "add_time_avg": r"PERFCOUNTER_TIME\s*\|\s*PERFCOUNTER_LONGRUNAVG",
    }
    for method, flags in constructor_contracts.items():
        definition = re.search(
            rf"void PerfCountersBuilder::{method}\s*\(.*?\n\}}",
            perf_counters,
            re.DOTALL,
        )
        if definition is None or re.search(flags, definition.group(0)) is None:
            raise ValueError(f"{perf_counters_path} {method} type contract changed")
    schema_type = re.search(
        r'if \(d->type & PERFCOUNTER_COUNTER\)\s*\{\s*'
        r'f->dump_string\("metric_type", "counter"\);\s*\}\s*else\s*\{\s*'
        r'f->dump_string\("metric_type", "gauge"\);',
        perf_counters,
        re.DOTALL,
    )
    if schema_type is None:
        raise ValueError(f"{perf_counters_path} ceph-exporter schema type contract changed")

    exporter_average = re.search(
        r"if \(type & PERFCOUNTER_LONGRUNAVG\)\s*\{.*?"
        r'add_metric\(builder, count, name \+ "_count".*?"counter",\s*labels\);.*?'
        r'add_double_or_int_metric\(builder, sum_value, name \+ "_sum".*?metric_type, labels\);',
        exporter,
        re.DOTALL,
    )
    if exporter_average is None:
        raise ValueError(f"{exporter_path} long-running-average type contract changed")

    mgr_type = re.search(
        r"def _stattype_to_str\(self, stattype: int\) -> str:.*?"
        r"if typeonly == 0:\s*return 'gauge'.*?"
        r"if typeonly == self\.PERFCOUNTER_LONGRUNAVG:.*?return 'counter'.*?"
        r"if typeonly == self\.PERFCOUNTER_COUNTER:\s*return 'counter'",
        mgr_module,
        re.DOTALL,
    )
    mgr_average = re.search(
        r"if counter_info\['type'\] & self\.PERFCOUNTER_LONGRUNAVG:.*?"
        r"_path = path \+ '_sum'.*?Metric\(\s*stattype,.*?"
        r"_path = path \+ '_count'.*?Metric\(\s*'counter',",
        prometheus,
        re.DOTALL,
    )
    if mgr_type is None or mgr_average is None:
        raise ValueError(f"{mgr_module_path} MGR long-running-average type contract changed")


def _literal_assignments(tree: ast.Module, source_path: str) -> tuple[dict[str, object], dict[str, tuple[str, int, int]]]:
    values: dict[str, object] = {}
    locations: dict[str, tuple[str, int, int]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
        name = node.targets[0].id
        values[name] = value
        locations[name] = (source_path, node.lineno, node.end_lineno or node.lineno)
    return values, locations


def _mgr_expression(node: ast.AST, values: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise ValueError(f"unsupported MGR source name {node.id!r} at line {node.lineno}")
        return values[node.id]
    if isinstance(node, ast.Tuple):
        return tuple(_mgr_expression(item, values) for item in node.elts)
    if isinstance(node, ast.List):
        return [_mgr_expression(item, values) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _mgr_expression(key, values): _mgr_expression(value, values)
            for key, value in zip(node.keys, node.values)
        }
    if isinstance(node, ast.Attribute):
        value = _mgr_expression(node.value, values)
        if not isinstance(value, dict) or node.attr not in value:
            raise ValueError(f"unsupported MGR source attribute {node.attr!r} at line {node.lineno}")
        return value[node.attr]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _mgr_expression(node.left, values) + _mgr_expression(node.right, values)
    if isinstance(node, ast.IfExp):
        return _mgr_expression(node.body if _mgr_expression(node.test, values) else node.orelse, values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = _mgr_expression(node.left, values)
        right = _mgr_expression(node.comparators[0], values)
        if isinstance(node.ops[0], ast.In):
            return left in right
        if isinstance(node.ops[0], ast.NotIn):
            return left not in right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        arguments = [_mgr_expression(item, values) for item in node.args]
        if node.keywords:
            raise ValueError(f"unsupported keyword argument in MGR source expression at line {node.lineno}")
        if node.func.id == "sensor_metric" and len(arguments) == 3:
            return dict(zip(("metric", "description", "labels"), arguments))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        receiver = _mgr_expression(node.func.value, values)
        arguments = [_mgr_expression(item, values) for item in node.args]
        if node.keywords:
            raise ValueError(f"unsupported keyword argument in MGR source expression at line {node.lineno}")
        if node.func.attr == "format" and isinstance(receiver, str):
            return receiver.format(*arguments)
        if node.func.attr == "lower" and isinstance(receiver, str) and not arguments:
            return receiver.lower()
    raise ValueError(f"unsupported MGR source expression {ast.dump(node, include_attributes=False)}")


def _mgr_loop_items(
    statement: ast.For,
    environment: dict[str, object],
    locations: dict[str, tuple[str, int, int]],
) -> tuple[str, tuple[object, ...], tuple[str, int, int]]:
    if not isinstance(statement.target, ast.Name):
        raise ValueError(f"unsupported MGR source loop at line {statement.lineno}")
    if isinstance(statement.iter, ast.Name):
        registry_name = statement.iter.id
        registry = environment.get(registry_name)
        if registry_name not in locations or not isinstance(registry, (list, tuple)):
            raise ValueError(f"unsupported MGR source loop at line {statement.lineno}")
        return statement.target.id, tuple(registry), locations[registry_name]
    if (
        isinstance(statement.iter, ast.Call)
        and not statement.iter.args
        and not statement.iter.keywords
        and isinstance(statement.iter.func, ast.Attribute)
        and statement.iter.func.attr == "values"
        and isinstance(statement.iter.func.value, ast.Name)
    ):
        registry_name = statement.iter.func.value.id
        registry = environment.get(registry_name)
        if registry_name not in locations or not isinstance(registry, dict):
            raise ValueError(f"unsupported MGR source loop at line {statement.lineno}")
        return statement.target.id, tuple(registry.values()), locations[registry_name]
    raise ValueError(f"unsupported MGR source loop at line {statement.lineno}")


def parse_mgr_static_registrations(root: Path, release: str) -> list[Registration]:
    source_path = "src/pybind/mgr/prometheus/module.py"
    mgr_module_path = "src/pybind/mgr/mgr_module.py"
    source = root / source_path
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=source_path)
    values, locations = _literal_assignments(tree, source_path)

    mgr_tree = ast.parse((root / mgr_module_path).read_text(encoding="utf-8"), filename=mgr_module_path)
    mgr_values, mgr_locations = _literal_assignments(mgr_tree, mgr_module_path)
    if "PG_STATES" not in mgr_values:
        raise ValueError(f"{mgr_module_path} has no literal PG_STATES registry")
    values["PG_STATES"] = mgr_values["PG_STATES"]
    locations["PG_STATES"] = mgr_locations["PG_STATES"]

    health_assignment = next((
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "HEALTH_CHECKS"
    ), None)
    if health_assignment is None or not isinstance(health_assignment.value, (ast.List, ast.Tuple)):
        raise ValueError(f"{source_path} has no bounded HEALTH_CHECKS registry")
    health_checks = []
    for item in health_assignment.value.elts:
        if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Name) or item.func.id != "alert_metric" or len(item.args) != 2:
            raise ValueError(f"{source_path} HEALTH_CHECKS contains an unsupported entry at line {item.lineno}")
        health_checks.append({"name": _mgr_expression(item.args[0], values), "description": _mgr_expression(item.args[1], values)})
    values["HEALTH_CHECKS"] = health_checks
    locations["HEALTH_CHECKS"] = (source_path, health_assignment.lineno, health_assignment.end_lineno or health_assignment.lineno)

    sensor_assignment = next((
        node for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "SENSOR_METRICS"
    ), None)
    if sensor_assignment is not None:
        if not isinstance(sensor_assignment.value, ast.Dict):
            raise ValueError(f"{source_path} SENSOR_METRICS is not a literal mapping")
        sensors = _mgr_expression(sensor_assignment.value, values)
        if not isinstance(sensors, dict) or not all(
            isinstance(sensor, dict) and set(sensor) == {"metric", "description", "labels"}
            for sensor in sensors.values()
        ):
            raise ValueError(f"{source_path} SENSOR_METRICS has an unsupported entry")
        values["SENSOR_METRICS"] = sensors
        locations["SENSOR_METRICS"] = (
            source_path,
            sensor_assignment.lineno,
            sensor_assignment.end_lineno or sensor_assignment.lineno,
        )

    module_class = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Module"), None)
    if module_class is None:
        raise ValueError(f"{source_path} has no Module class")
    setup = next((
        node for node in module_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_setup_static_metrics"
    ), None)
    if setup is None:
        raise ValueError(f"{source_path} has no _setup_static_metrics")

    registrations: list[Registration] = []

    def execute(statements: list[ast.stmt], environment: dict[str, object], inherited: tuple[tuple[str, int, int], ...] = ()) -> None:
        for statement in statements:
            if isinstance(statement, ast.Return):
                continue
            if isinstance(statement, ast.For):
                target_name, items, location = _mgr_loop_items(statement, environment, locations)
                for item in items:
                    nested = dict(environment)
                    nested[target_name] = item
                    execute(statement.body, nested, inherited + (location,))
                continue
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                raise ValueError(f"unsupported MGR source statement {type(statement).__name__} at line {statement.lineno}")
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                environment[target.id] = _mgr_expression(statement.value, environment)
                continue
            if not (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "metrics"
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
                and statement.value.func.id == "Metric"
            ):
                raise ValueError(f"unsupported MGR metric assignment at line {statement.lineno}")
            arguments = statement.value.args
            if len(arguments) not in {3, 4}:
                raise ValueError(f"unsupported MGR Metric constructor at line {statement.value.lineno}")
            metric_type_value = _mgr_expression(arguments[0], environment)
            metric_name = _mgr_expression(arguments[1], environment)
            if not isinstance(metric_type_value, str) or not isinstance(metric_name, str):
                raise ValueError(f"non-string MGR metric identity at line {statement.value.lineno}")
            registrations.append(Registration(
                source_variant=release,
                source_path=source_path,
                line_start=statement.value.lineno,
                line_end=statement.value.end_lineno or statement.value.lineno,
                group="mgr_synthetic",
                grammar=None,
                form=metric_name,
                prometheus_type=metric_type_value,
                priority=0,
                shape=metric_family_shape(prometheus_name(metric_name), metric_type_value, "scalar"),
                endpoint="mgr_synthetic",
                classification="gauge" if metric_type_value == "untyped" else None,
                exact_family_override=prometheus_name(metric_name),
                extra_locations=inherited,
            ))

    execute(setup.body, dict(values))
    return registrations + parse_mgr_dynamic_registrations(module_class, source_path, release)


def parse_mgr_dynamic_registrations(module_class: ast.ClassDef, source_path: str, release: str) -> list[Registration]:
    init = next((node for node in module_class.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"), None)
    if init is None:
        raise ValueError(f"{source_path} has no Module.__init__")
    rbd_assignment = next((
        node for node in init.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "self"
        and node.targets[0].attr == "rbd_stats"
    ), None)
    if rbd_assignment is None or not isinstance(rbd_assignment.value, ast.Dict):
        raise ValueError(f"{source_path} has no literal rbd_stats registry")
    top = {key.value: value for key, value in zip(rbd_assignment.value.keys, rbd_assignment.value.values) if isinstance(key, ast.Constant)}
    counters = top.get("counters_info")
    if not isinstance(counters, ast.Dict):
        raise ValueError(f"{source_path} rbd_stats has no literal counters_info registry")

    result: list[Registration] = []
    for key_node, value_node in zip(counters.keys, counters.values):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str) or not isinstance(value_node, ast.Dict):
            raise ValueError(f"{source_path} has unsupported rbd_stats counter at line {value_node.lineno}")
        fields = {key.value: value for key, value in zip(value_node.keys, value_node.values) if isinstance(key, ast.Constant)}
        type_node = fields.get("type")
        if not isinstance(type_node, ast.Attribute) or type_node.attr not in {"PERFCOUNTER_COUNTER", "PERFCOUNTER_LONGRUNAVG"}:
            raise ValueError(f"{source_path} has unsupported rbd_stats counter type at line {value_node.lineno}")
        forms = (key_node.value,) if type_node.attr == "PERFCOUNTER_COUNTER" else (
            key_node.value + "_sum", key_node.value + "_count"
        )
        for form in forms:
            result.append(Registration(
                source_variant=release,
                source_path=source_path,
                line_start=value_node.lineno,
                line_end=value_node.end_lineno or value_node.lineno,
                group="mgr_rbd_stats",
                grammar=None,
                form=form,
                prometheus_type="counter",
                priority=0,
                endpoint="mgr_rbd_stats",
                exact_family_override=prometheus_name("rbd_" + form),
            ))

    collect = next((node for node in module_class.body if isinstance(node, ast.FunctionDef) and node.name == "get_collect_time_metrics"), None)
    if collect is None:
        raise ValueError(f"{source_path} has no get_collect_time_metrics")
    counter_calls = [
        node for node in ast.walk(collect)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "MetricCounter"
    ]
    if len(counter_calls) != 2:
        raise ValueError(f"{source_path} must register exactly two collection counters")
    for call in counter_calls:
        name = _mgr_expression(call.args[0], {})
        if not isinstance(name, str):
            raise ValueError(f"{source_path} has non-string collection counter at line {call.lineno}")
        result.append(Registration(
            source_variant=release,
            source_path=source_path,
            line_start=call.lineno,
            line_end=call.end_lineno or call.lineno,
            group="mgr_synthetic",
            grammar=None,
            form=name,
            prometheus_type="counter",
            priority=0,
            endpoint="mgr_synthetic",
            exact_family_override=prometheus_name(name),
        ))
    return result


def metric_type(method: str) -> str:
    if method in AVERAGE_METHODS:
        raise AssertionError("average methods expand before type classification")
    return "counter" if method == "u64_counter" else "gauge"


def split_cpp_arguments(arguments: str) -> list[str]:
    result: list[str] = []
    start = 0
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(arguments):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != pairs[char]:
                raise ValueError("unbalanced C++ metric arguments")
        elif char == "," and not stack:
            result.append(arguments[start:index].strip())
            start = index + 1
    if quote is not None or stack:
        raise ValueError("unterminated C++ metric arguments")
    result.append(arguments[start:].strip())
    return result


def configured_priority(root: Path, text: str, call_offset: int, expression: str) -> int | None:
    assignments = list(
        re.finditer(
            rf"(?:auto|int64_t|uint8_t)\s+{re.escape(expression)}\s*=\s*"
            r".*?_conf\.get_val<int64_t>\(\s*\"([a-z0-9_]+)\"\s*\)\s*;",
            text[:call_offset],
            re.DOTALL,
        )
    )
    if not assignments:
        return None
    option = assignments[-1].group(1)
    option_path = CONFIG_PRIORITY_SOURCES.get(option)
    if option_path is None:
        raise ValueError(f"unsupported configurable metric priority {option}")
    option_text = (root / option_path).read_text(encoding="utf-8")
    declaration = re.search(
        rf"(?m)^- name: {re.escape(option)}\n(?P<body>(?:^(?!- name: ).*\n?)*)",
        option_text,
    )
    if declaration is None:
        raise ValueError(f"{option_path} has no declaration for {option}")
    default = re.search(r"(?m)^  default: ([0-9]+)$", declaration.group("body"))
    if default is None:
        raise ValueError(f"{option_path} has no integer default for {option}")
    return int(default.group(1))


@cache
def priority_values(root: Path) -> dict[str, int]:
    path = "src/common/perf_counters.h"
    text = (root / path).read_text(encoding="utf-8")
    block = re.search(
        r"enum\s*\{(?P<body>.*?PRIO_DEBUGONLY\s*=\s*[0-9]+\s*,.*?)\};",
        text,
        re.DOTALL,
    )
    if block is None:
        raise ValueError(f"{path} has no bounded priority registry")
    values = {
        name: int(value)
        for name, value in re.findall(r"\b(PRIO_[A-Z]+)\s*=\s*([0-9]+)", block.group("body"))
    }
    expected = {
        "PRIO_CRITICAL": 10,
        "PRIO_INTERESTING": 8,
        "PRIO_USEFUL": 5,
        "PRIO_UNINTERESTING": 2,
        "PRIO_DEBUGONLY": 0,
    }
    if values != expected:
        raise ValueError(f"{path} priority registry changed: {values}")
    return values


def priority(root: Path, path: str, text: str, builder: Builder, call_offset: int, arguments: str) -> int:
    known_priorities = priority_values(root)
    call_arguments = split_cpp_arguments(arguments)
    expression = call_arguments[4] if len(call_arguments) > 4 else "0"
    default = 0
    defaults = list(
        re.finditer(
            rf"\b{re.escape(builder.variable)}\.set_prio_default\s*\(\s*"
            r"PerfCountersBuilder::(PRIO_[A-Z]+)\s*\)",
            text[builder.offset:call_offset],
        )
    )
    if defaults:
        token = defaults[-1].group(1)
        if token not in known_priorities:
            raise ValueError(f"unsupported default priority {token}")
        default = known_priorities[token]
    token = expression.removeprefix("PerfCountersBuilder::")
    if token in known_priorities:
        explicit = known_priorities[token]
        return explicit or default
    if expression in {"0", "NULL", "nullptr"}:
        return default
    if re.fullmatch(r"[0-9]+", expression):
        value = int(expression)
        if value > 11:
            raise ValueError(f"metric priority {value} is outside Ceph's supported 0..11 range")
        return value or default
    if path == "src/librbd/ImageCtx.cc" and expression == "perf_prio":
        # One family language is reachable at priority 5 for top-level images
        # and gains child-image contributors at priority 0. The registry owns
        # family reachability; contributor topology belongs to source semantics.
        return known_priorities["PRIO_USEFUL"]
    symbolic = list(
        re.finditer(
            rf"(?:auto\s+constexpr|constexpr\s+auto|auto|int64_t|uint8_t)\s+{re.escape(expression)}\s*=\s*"
            r"PerfCountersBuilder::(PRIO_[A-Z]+)\s*;",
            text[:call_offset],
        )
    )
    if symbolic:
        token = symbolic[-1].group(1)
        if token not in known_priorities:
            raise ValueError(f"unsupported symbolic priority {token}")
        return known_priorities[token]
    configured = configured_priority(root, text, call_offset, expression)
    if configured is not None:
        return configured
    raise ValueError(f"unsupported metric priority expression {expression!r} in {path}")


@cache
def priority_cache_groups(root: Path) -> dict[str, tuple[tuple[str, int, int], ...]]:
    result: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    callers = (
        "src/mon/OSDMonitor.cc",
        "src/os/bluestore/BlueStore.cc",
        "src/tools/rbd_mirror/Mirror.cc",
    )
    for path in callers:
        text = (root / path).read_text(encoding="utf-8")
        constructors = list(re.finditer(
            r"(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
            r"std::make_(?:shared|unique)<PriorityCache::Manager>\s*\((?P<arguments>.*?)\);",
            text,
            re.DOTALL,
        ))
        if len(constructors) != 1:
            raise ValueError(f"{path} must contain exactly one PriorityCache::Manager construction")
        constructor = constructors[0]
        arguments = split_cpp_arguments(constructor.group("arguments"))
        if len(arguments) not in {5, 6}:
            raise ValueError(f"{path} has unsupported PriorityCache::Manager arguments")
        base = "prioritycache" if len(arguments) == 5 else static_group(arguments[5])
        if base is None:
            raise ValueError(f"{path} has nonliteral PriorityCache::Manager name")
        result[base].append((path, line_number(text, constructor.start()), line_number(text, constructor.end())))

        variable = constructor.group("variable")
        for insertion in re.finditer(
            rf"\b{re.escape(variable)}->insert\s*\((.*?)\);",
            text[constructor.end():],
            re.DOTALL,
        ):
            insertion_arguments = split_cpp_arguments(insertion.group(1))
            if len(insertion_arguments) != 3:
                raise ValueError(f"{path} has unsupported PriorityCache::Manager insert arguments")
            if insertion_arguments[2] == "false":
                continue
            if insertion_arguments[2] != "true":
                raise ValueError(f"{path} has nonliteral PriorityCache::Manager perf-counter mode")
            name = static_group(insertion_arguments[0])
            if name is None:
                raise ValueError(f"{path} has nonliteral enabled PriorityCache::Manager cache name")
            absolute_start = constructor.end() + insertion.start()
            absolute_end = constructor.end() + insertion.end()
            result[f"{base}:{name}"].append((
                path,
                line_number(text, absolute_start),
                line_number(text, absolute_end),
            ))
    return {name: tuple(locations) for name, locations in result.items()}


@cache
def dmclock_groups(root: Path) -> dict[str, tuple[str, int, int, str]]:
    path = "src/rgw/rgw_dmclock_scheduler_ctx.cc"
    text = (root / path).read_text(encoding="utf-8")
    result: dict[str, tuple[str, int, int, str]] = {}
    for match in re.finditer(r"\b(queue_counters|throttle_counters)::build\s*\(\s*cct\s*,\s*\"([^\"]+)\"\s*\)", text):
        kind, name = match.groups()
        if name in result:
            raise ValueError(f"{path} constructs duplicate dmclock group {name!r}")
        result[name] = (path, line_number(text, match.start()), line_number(text, match.end()), kind)
    queue_count = sum(kind == "queue_counters" for _, _, _, kind in result.values())
    throttle_count = sum(kind == "throttle_counters" for _, _, _, kind in result.values())
    if queue_count != 4 or throttle_count != 1:
        raise ValueError(f"{path} must construct four queue and one throttle dmclock groups")
    return result


@cache
def striper_groups(root: Path) -> dict[str, tuple[str, int, int]]:
    path = "src/libcephsqlite.cc"
    text = (root / path).read_text(encoding="utf-8")
    calls = list(re.finditer(r"SimpleRADOSStriper::config_logger\s*\([^,]+,\s*\"([^\"]+)\"", text))
    if len(calls) != 1:
        raise ValueError(f"{path} must contain exactly one SimpleRADOSStriper logger construction")
    call = calls[0]
    return {call.group(1): (path, line_number(text, call.start()), line_number(text, call.end()))}


def special_groups(root: Path, path: str, text: str, builder: Builder) -> tuple[str, ...] | None:
    if path == "src/common/PriorityCache.cc":
        groups = priority_cache_groups(root)
        if builder.expression == "this->name":
            return tuple(sorted(name for name in groups if ":" not in name))
        if builder.expression == 'this->name + ":" + name':
            return tuple(sorted(name for name in groups if ":" in name))
    if path == "src/rgw/rgw_dmclock_scheduler_ctx.cc" and builder.expression == "name":
        namespaces = list(re.finditer(r"namespace\s+(queue_counters|throttle_counters)\s*\{", text[:builder.offset]))
        if not namespaces:
            raise ValueError(f"{path} dynamic dmclock builder is outside a supported namespace")
        kind = namespaces[-1].group(1)
        return tuple(sorted(name for name, (_, _, _, owner) in dmclock_groups(root).items() if owner == kind))
    if path == "src/SimpleRADOSStriper.cc" and builder.expression == "name.data()":
        return tuple(striper_groups(root))
    if path == "src/osd/osd_perf_counters.cc" and builder.expression == "label":
        header_path = root / "src/osd/scrubber/osd_scrub.h"
        if not header_path.exists():
            raise ValueError(f"{path} dynamic scrub groups require {header_path}")
        header = header_path.read_text(encoding="utf-8")
        declaration = re.search(r"perf_labels\s*=\s*\{(?P<body>.*?)\};", header, re.DOTALL)
        if declaration is None:
            raise ValueError(f"{header_path} has no bounded perf_labels registry")
        groups = tuple(re.findall(r'key_create\s*\(\s*"([a-z0-9_]+)"', declaration.group("body")))
        if len(groups) != 4 or len(groups) != len(set(groups)):
            raise ValueError(f"{header_path} must define four unique scrub groups")
        return groups
    return None


def special_group_locations(root: Path, path: str, group: str) -> tuple[tuple[str, int, int], ...]:
    if path == "src/common/PriorityCache.cc":
        return priority_cache_groups(root)[group]
    if path == "src/rgw/rgw_dmclock_scheduler_ctx.cc":
        source_path, line_start, line_end, _ = dmclock_groups(root)[group]
        return ((source_path, line_start, line_end),)
    if path == "src/SimpleRADOSStriper.cc":
        return (striper_groups(root)[group],)
    if path == "src/osd/osd_perf_counters.cc" and group.startswith("osd_scrub_"):
        header_path = "src/osd/scrubber/osd_scrub.h"
        header = (root / header_path).read_text(encoding="utf-8")
        match = re.search(rf'key_create\s*\(\s*"{re.escape(group)}"', header)
        if match is None:
            raise ValueError(f"{header_path} has no source entry for scrub group {group!r}")
        line = line_number(header, match.start())
        return ((header_path, line, line),)
    return ()


@cache
def grammar_locations(root: Path, grammar: str | None) -> tuple[tuple[str, int, int], ...]:
    if grammar == "objecter":
        path = "src/common/perf_counters.cc"
        text = (root / path).read_text(encoding="utf-8")
        match = re.search(
            r"void\s+PerfCountersCollectionImpl::add\s*\([^)]*\)\s*\{(?P<body>.*?)m_loggers\.insert",
            text,
            re.DOTALL,
        )
        if match is None or "set_name" not in match.group("body"):
            raise ValueError(f"{path} has no duplicate logger identity rewrite")
        return ((path, line_number(text, match.start()), line_number(text, match.end())),)
    if grammar == "rgw_sync":
        locations = []
        for path, pattern in (
            ("src/pybind/mgr/prometheus/module.py", r"def add_fixed_name_metrics\(.*?self\.metrics\.update\(new_metrics\)"),
            ("src/exporter/DaemonMetricCollector.cc", r"DaemonMetricCollector::add_fixed_name_metrics\(.*?\n\}"),
        ):
            text = (root / path).read_text(encoding="utf-8")
            match = re.search(pattern, text, re.DOTALL)
            if match is None or not any(token in match.group(0) for token in ("data_sync_from", "data-sync-from")):
                raise ValueError(f"{path} has no RGW source-zone family rewrite")
            locations.append((path, line_number(text, match.start()), line_number(text, match.end())))
        return tuple(locations)
    return ()


def dynamic_builder(path: str, expression: str) -> str | None:
    if path in DYNAMIC_BUILDERS:
        return DYNAMIC_BUILDERS[path]
    if path == "src/msg/async/Stack.h":
        return "messenger_worker"
    if path == "src/msg/async/rdma/RDMAStack.cc":
        return "rdma_worker"
    if path == "src/msg/async/dpdk/DPDK.cc":
        return "dpdk_queue"
    if path == "src/msg/async/dpdk/DPDK.h":
        return "dpdk_port"
    return None


def terminal_identity_form(
    path: str,
    builder: Builder,
    method: str,
    arguments: str,
) -> str | None:
    if path != "src/common/ceph_context.cc" or builder.static_group != "service_unique_id":
        return None
    parsed = split_cpp_arguments(arguments)
    expected = (
        method == "u64"
        and len(parsed) == 3
        and parsed[0].strip() == "l_service_unique_id"
        and parsed[1].strip() == "service_unique_id.c_str()"
        and string_literals(parsed[2]) == ["Unique ID for this service"]
    )
    if not expected:
        raise ValueError(f"{path} service_unique_id construction changed")
    return "identity"


def parse_release(root: Path, release: str) -> tuple[list[Registration], Counter[tuple[str, str]]]:
    validate_endpoint_contracts(root)
    registrations: list[Registration] = []
    unclassified: Counter[tuple[str, str]] = Counter()
    for source in sorted(root.rglob("*")):
        if not source.is_file() or source.suffix not in {".cc", ".h"} or "/test/" in source.as_posix():
            continue
        relative = source.relative_to(root).as_posix()
        text = source.read_text(encoding="utf-8", errors="replace")
        builders: list[Builder] = []
        for match in re.finditer(
            r"PerfCountersBuilder\s+(\w+)\s*\(\s*[^,]+,\s*([^,]+),",
            text,
            re.DOTALL,
        ):
            expression = " ".join(match.group(2).split())
            builders.append(Builder(match.start(), match.group(1), expression, static_group(expression)))
        for match in re.finditer(r"\b(\w+)\.add_([a-z0-9_]+)\s*\((.*?)\);", text, re.DOTALL):
            variable, method, arguments = match.groups()
            if method in IGNORED_METHODS:
                continue
            if method not in SUPPORTED_METHODS:
                candidates = [item for item in builders if item.offset < match.start() and item.variable == variable]
                if candidates:
                    unclassified[(relative, f"{variable}.add_{method} unsupported method")] += 1
                continue
            names = string_literals(arguments)
            if not names:
                candidates = [item for item in builders if item.offset < match.start() and item.variable == variable]
                generated_name = (
                    relative == "src/common/ceph_context.cc" and variable == "plb2" or
                    relative == "src/kv/rocksdb_cache/BinnedLRUCache.cc" and variable == "b"
                )
                if candidates and not generated_name:
                    unclassified[(relative, f"{variable}.add_{method} without a literal name")] += 1
                continue
            candidates = [item for item in builders if item.offset < match.start() and item.variable == variable]
            if not candidates:
                continue
            builder = max(candidates, key=lambda item: item.offset)
            grammar = None
            terminal_form = terminal_identity_form(relative, builder, method, arguments)
            if terminal_form is not None:
                grammar = "service_unique_id"
                groups = (grammar,)
            else:
                groups = (builder.static_group,) if builder.static_group is not None else special_groups(root, relative, text, builder)
                if groups is None:
                    group, grammar = resolve_key_created_group(text, builder)
                    groups = (group,) if group is not None else None
                if groups is None:
                    grammar = dynamic_builder(relative, builder.expression)
                    if grammar is None:
                        unclassified[(relative, builder.expression)] += 1
                        continue
                    groups = (grammar,)
                if relative == "src/osdc/Objecter.cc" and groups == ("objecter",):
                    grammar = "objecter"
                    groups = ("objecter",)
            forms = [terminal_form] if terminal_form is not None else [names[0]]
            if method in AVERAGE_METHODS:
                forms = [names[0] + "_count", names[0] + "_sum"]
            registration_priority = priority(root, relative, text, builder, match.start(), arguments)
            for group in groups:
                for form in forms:
                    if method in AVERAGE_METHODS:
                        variants = (("counter", "daemon_perf"),) if form.endswith("_count") else (
                            ("gauge", "exporter_perf"),
                            ("counter", "mgr_perf"),
                        )
                    else:
                        variants = ((metric_type(method), "daemon_perf"),)
                    for output_type, endpoint in variants:
                        registrations.append(Registration(
                            source_variant=release,
                            source_path=relative,
                            line_start=line_number(text, match.start()),
                            line_end=line_number(text, match.end()),
                            group=group,
                            grammar=grammar,
                            form=form,
                            prometheus_type=output_type,
                            priority=registration_priority,
                            endpoint=endpoint,
                            extra_locations=(
                                special_group_locations(root, relative, group)
                                + grammar_locations(root, grammar)
                            ),
                        ))
        if relative == "src/common/ceph_context.cc":
            registrations.extend(parse_mempool_registrations(root, release, relative, text))
        if relative == "src/kv/rocksdb_cache/BinnedLRUCache.cc":
            registrations.extend(parse_binned_cache_registrations(root, release, relative, text))
        if relative == "src/exporter/DaemonMetricCollector.cc":
            registrations.extend(parse_exporter_registrations(release, relative, text))
    registrations.extend(parse_mgr_static_registrations(root, release))
    return registrations, unclassified


def parse_mempool_registrations(root: Path, release: str, source_path: str, text: str) -> list[Registration]:
    header = (root / "src/include/mempool.h").read_text(encoding="utf-8")
    definition = re.search(
        r"#define DEFINE_MEMORY_POOLS_HELPER\(f\)\s*\\\n(?P<body>.*?)(?:\n\s*\n|\n// give them integer ids)",
        header,
        re.DOTALL,
    )
    if definition is None:
        raise ValueError("src/include/mempool.h has no bounded memory-pool registry")
    pools = re.findall(r"\bf\(([a-z][a-z0-9_]*)\)", definition.group("body"))
    if not pools or len(pools) != len(set(pools)):
        raise ValueError("src/include/mempool.h memory-pool registry is empty or duplicated")
    calls = list(re.finditer(r"\bplb2\.add_u64\s*\((.*?)\);", text, re.DOTALL))
    if len(calls) != 2:
        raise ValueError(f"{source_path} must register exactly two generated mempool forms")
    forms = ("bytes", "items")
    result = []
    for pool in pools:
        for form, call in zip(forms, calls):
            result.append(
                Registration(
                    source_variant=release,
                    source_path=source_path,
                    line_start=line_number(text, call.start()),
                    line_end=line_number(text, call.end()),
                    group="mempool",
                    grammar=None,
                    form=f"{pool}_{form}",
                    prometheus_type="gauge",
                    priority=0,
                    extra_locations=((
                        "src/include/mempool.h",
                        line_number(header, definition.start()),
                        line_number(header, definition.end()),
                    ),),
                )
            )
    return result


def parse_binned_cache_registrations(root: Path, release: str, source_path: str, text: str) -> list[Registration]:
    header_path = root / "src/kv/rocksdb_cache/BinnedLRUCache.h"
    if not header_path.exists():
        return []
    header = header_path.read_text(encoding="utf-8")
    names_match = re.search(
        r"stat_name\[stat_cnt\]\s*=\s*\{(?P<body>.*?)\};",
        header,
        re.DOTALL,
    )
    if names_match is None:
        raise ValueError(f"{header_path} has no bounded ShardStats::stat_name registry")
    forms = tuple(string_literals(names_match.group("body")))
    if not forms or len(forms) != len(set(forms)):
        raise ValueError(f"{header_path} ShardStats::stat_name is empty or duplicated")
    calls = list(re.finditer(r"\bb\.add_u64\s*\((.*?)\);", text, re.DOTALL))
    generated = [call for call in calls if not string_literals(call.group(1))]
    if len(generated) != 1:
        raise ValueError(f"{source_path} must have one generated binned-cache registration loop")
    call = generated[0]
    return [
        Registration(
            source_variant=release,
            source_path=source_path,
            line_start=line_number(text, call.start()),
            line_end=line_number(text, call.end()),
            group="rocksdb_cache",
            grammar="rocksdb_cache",
            form=form,
            prometheus_type="gauge",
            priority=0,
            extra_locations=((
                "src/kv/rocksdb_cache/BinnedLRUCache.h",
                line_number(header, names_match.start()),
                line_number(header, names_match.end()),
            ),),
        )
        for form in forms
    ]


def parse_exporter_registrations(release: str, source_path: str, text: str) -> list[Registration]:
    result = []
    for match in re.finditer(r"\badd_metric\s*\(\s*builder\s*,(.*?)\);", text, re.DOTALL):
        literals = string_literals(match.group(1))
        families = [value for value in literals if value.startswith("ceph_")]
        types = [value for value in literals if value in {"counter", "gauge"}]
        if len(families) != 1 or len(types) != 1:
            continue
        result.append(
            Registration(
                source_variant=release,
                source_path=source_path,
                line_start=line_number(text, match.start()),
                line_end=line_number(text, match.end()),
                group="ceph_exporter",
                grammar=None,
                form=families[0],
                prometheus_type=types[0],
                priority=0,
                endpoint="exporter_self",
            )
        )
    return result


def _one_class(tree: ast.Module, name: str, source_path: str) -> ast.ClassDef:
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    if len(matches) != 1:
        raise ValueError(f"{source_path} must define exactly one {name} class")
    return matches[0]


def _one_method(owner: ast.ClassDef, name: str, source_path: str) -> ast.FunctionDef:
    matches = [node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(matches) != 1:
        raise ValueError(f"{source_path} {owner.name} must define exactly one {name} method")
    return matches[0]


def _nvmeof_metric_name(node: ast.AST, prefix: str) -> str:
    if not isinstance(node, ast.JoinedStr):
        raise ValueError(f"NVMe-oF metric name at line {node.lineno} must be a bounded prefix f-string")
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
            continue
        if (
            isinstance(value, ast.FormattedValue)
            and isinstance(value.value, ast.Attribute)
            and isinstance(value.value.value, ast.Name)
            and value.value.value.id == "self"
            and value.value.attr == "metric_prefix"
            and value.conversion == -1
            and value.format_spec is None
        ):
            parts.append(prefix)
            continue
        raise ValueError(f"unsupported NVMe-oF metric-name expression at line {node.lineno}")
    result = "".join(parts)
    if not re.fullmatch(r"ceph_nvmeof_[a-z0-9_]+", result):
        raise ValueError(f"invalid NVMe-oF metric name {result!r} at line {node.lineno}")
    return result


def metric_family_shape(family: str, prometheus_type: str, declared_shape: str) -> str:
    if declared_shape == "info":
        return declared_shape
    if prometheus_type == "gauge" and family.endswith("_info"):
        return "info"
    return declared_shape


def _literal_label_names(node: ast.AST, field: str) -> tuple[str, ...]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        raise ValueError(f"{field} must be a literal label-name list")
    labels = []
    for item in node.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            raise ValueError(f"{field} contains a non-literal label name")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", item.value):
            raise ValueError(f"{field} contains invalid label name {item.value!r}")
        labels.append(item.value)
    if len(labels) != len(set(labels)):
        raise ValueError(f"{field} contains duplicate label names")
    return tuple(labels)


def _client_metric_family_locations(client_root: Path) -> dict[str, tuple[tuple[str, str, int, int], ...]]:
    core_path = "prometheus_client/metrics_core.py"
    exposition_path = "prometheus_client/exposition.py"
    core_text = (client_root / core_path).read_text(encoding="utf-8")
    exposition_text = (client_root / exposition_path).read_text(encoding="utf-8")
    core_tree = ast.parse(core_text, filename=core_path)
    exposition_tree = ast.parse(exposition_text, filename=exposition_path)

    classes = {
        name: _one_class(core_tree, name, core_path)
        for name in ("CounterMetricFamily", "GaugeMetricFamily", "InfoMetricFamily")
    }
    class_text = {name: ast.get_source_segment(core_text, node) or "" for name, node in classes.items()}
    required = {
        "CounterMetricFamily": ("name.endswith('_total')", "name = name[:-6]", "self.name + '_total'"),
        "GaugeMetricFamily": ("Metric.__init__(self, name, documentation, 'gauge', unit)", "Sample(self.name,"),
        "InfoMetricFamily": ("Metric.__init__(self, name, documentation, 'info')", "self.name + '_info'"),
    }
    for name, snippets in required.items():
        if not all(snippet in class_text[name] for snippet in snippets):
            raise ValueError(f"{core_path} {name} wire contract changed")

    generate_latest = next((
        node for node in exposition_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "generate_latest"
    ), None)
    if generate_latest is None:
        raise ValueError(f"{exposition_path} has no generate_latest function")
    exposition_contract = ast.get_source_segment(exposition_text, generate_latest) or ""
    for snippet in (
        "if mtype == 'counter':\n                mname = mname + '_total'",
        "elif mtype == 'info':\n                mname = mname + '_info'\n                mtype = 'gauge'",
    ):
        if snippet not in exposition_contract:
            raise ValueError(f"{exposition_path} Prometheus text-family contract changed")

    exposition_location = (
        "prometheus_client_python",
        exposition_path,
        generate_latest.lineno,
        generate_latest.end_lineno or generate_latest.lineno,
    )
    return {
        name: (
            (
                "prometheus_client_python",
                core_path,
                node.lineno,
                node.end_lineno or node.lineno,
            ),
            exposition_location,
        )
        for name, node in classes.items()
    }


def parse_nvmeof_registrations(root: Path, client_root: Path) -> list[Registration]:
    source_path = "control/prometheus.py"
    project_path = "pyproject.toml"
    source_text = (root / source_path).read_text(encoding="utf-8")
    project_text = (root / project_path).read_text(encoding="utf-8")
    if '"prometheus_client ~= 0.19.0"' not in project_text:
        raise ValueError(f"{project_path} prometheus_client dependency contract changed")

    tree = ast.parse(source_text, filename=source_path)
    collector = _one_class(tree, "NVMeOFCollector", source_path)
    initializer = _one_method(collector, "__init__", source_path)
    collect = _one_method(collector, "collect", source_path)

    prefix_assignments = [
        node for node in ast.walk(initializer)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "self"
        and node.targets[0].attr == "metric_prefix"
    ]
    if len(prefix_assignments) != 1:
        raise ValueError(f"{source_path} must assign self.metric_prefix exactly once")
    try:
        prefix = ast.literal_eval(prefix_assignments[0].value)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{source_path} self.metric_prefix must be a literal string") from error
    if prefix != "ceph_nvmeof":
        raise ValueError(f"{source_path} metric prefix changed to {prefix!r}")

    supported = {"GaugeMetricFamily", "CounterMetricFamily", "InfoMetricFamily"}
    all_family_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.endswith("MetricFamily")
    ]
    collect_family_calls = [
        node for node in ast.walk(collect)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.endswith("MetricFamily")
    ]
    if {id(node) for node in all_family_calls} != {id(node) for node in collect_family_calls}:
        raise ValueError(f"{source_path} metric-family construction outside NVMeOFCollector.collect is unsupported")
    unknown = sorted({node.func.id for node in collect_family_calls if node.func.id not in supported})
    if unknown:
        raise ValueError(f"{source_path} has unsupported metric-family constructors: {unknown}")
    if not collect_family_calls:
        raise ValueError(f"{source_path} NVMeOFCollector.collect registers no metric families")

    family_call_ids = {id(call) for call in collect_family_calls}
    assignments: dict[int, tuple[str, ast.Call, ast.Assign]] = {}
    for node in collect.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and id(node.value) in family_call_ids
        ):
            continue
        assignments[id(node.value)] = (node.targets[0].id, node.value, node)
    if set(assignments) != family_call_ids:
        raise ValueError(f"{source_path} metric families must use unconditional direct single-name assignments")

    yielded = Counter(
        node.value.value.id for node in collect.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Yield)
        and isinstance(node.value.value, ast.Name)
    )
    client_locations = _client_metric_family_locations(client_root)
    result = []
    variables = set()
    families = set()
    for variable, call, assignment in sorted(assignments.values(), key=lambda item: item[1].lineno):
        if variable in variables:
            raise ValueError(f"{source_path} reuses metric-family variable {variable!r}")
        variables.add(variable)
        if yielded[variable] != 1:
            raise ValueError(
                f"{source_path} metric-family variable {variable!r} must be yielded unconditionally exactly once"
            )
        if len(call.args) != 2 or not isinstance(call.args[1], ast.Constant) or not isinstance(call.args[1].value, str):
            raise ValueError(f"{source_path}:{call.lineno} metric family must have literal name and documentation arguments")
        keywords = {item.arg: item.value for item in call.keywords if item.arg is not None}
        if len(keywords) != len(call.keywords):
            raise ValueError(f"{source_path}:{call.lineno} metric family uses keyword expansion")

        constructor = call.func.id
        raw_family = _nvmeof_metric_name(call.args[0], prefix)
        if constructor == "InfoMetricFamily":
            if set(keywords) != {"value"} or not isinstance(keywords["value"], ast.Dict):
                raise ValueError(f"{source_path}:{call.lineno} InfoMetricFamily must have one literal value mapping")
            for key in keywords["value"].keys:
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    raise ValueError(f"{source_path}:{call.lineno} InfoMetricFamily has a dynamic label name")
            family = raw_family + "_info"
            prometheus_type = "gauge"
            shape = "info"
        else:
            if set(keywords) not in (set(), {"labels"}):
                raise ValueError(f"{source_path}:{call.lineno} {constructor} has unsupported keyword arguments")
            if "labels" in keywords:
                _literal_label_names(keywords["labels"], f"{source_path}:{call.lineno} labels")
            family = raw_family
            prometheus_type = "counter" if constructor == "CounterMetricFamily" else "gauge"
            shape = "scalar"
            if constructor == "CounterMetricFamily" and not family.endswith("_total"):
                raise ValueError(f"{source_path}:{call.lineno} counter family must use its Prometheus _total name")
        if family in families:
            raise ValueError(f"{source_path} registers duplicate family {family!r}")
        families.add(family)
        shape = metric_family_shape(family, prometheus_type, shape)
        result.append(Registration(
            source_variant="nvmeof",
            source_path=source_path,
            line_start=assignment.lineno,
            line_end=assignment.end_lineno or assignment.lineno,
            group="nvmeof",
            grammar=None,
            form=family,
            prometheus_type=prometheus_type,
            priority=0,
            shape=shape,
            endpoint="nvmeof",
            exact_family_override=family,
            dependency_locations=client_locations[constructor],
        ))
    return result


def exact_family(registration: Registration) -> str | None:
    if registration.exact_family_override is not None:
        return registration.exact_family_override
    if registration.grammar is not None:
        return None
    if registration.group == "ceph_exporter":
        return registration.form
    return prometheus_name(registration.group + "_" + registration.form)


def merge_registrations(by_variant: dict[str, list[Registration]]) -> list[MergedRegistration]:
    variants: dict[tuple[str | None, str | None, str, str, str, int, str, str | None], list[Registration]] = defaultdict(list)
    seen_variant: dict[tuple[str | None, str | None, str, str, str], tuple[int, str, str, str | None]] = {}
    for source_variant, registrations in by_variant.items():
        for registration in registrations:
            if registration.source_variant != source_variant:
                raise ValueError(
                    f"registration variant {registration.source_variant!r} is stored under {source_variant!r}"
                )
            family = exact_family(registration)
            previous = seen_variant.get((
                family,
                registration.grammar,
                registration.form,
                registration.endpoint,
                source_variant,
            ))
            current = (
                registration.priority,
                registration.prometheus_type,
                registration.shape,
                registration.classification,
            )
            if previous is not None and previous != current:
                raise ValueError(
                    f"{source_variant} selector {(family, registration.grammar, registration.form)} "
                    f"has conflicting priority/type {previous} and {current}"
                )
            seen_variant[(
                family,
                registration.grammar,
                registration.form,
                registration.endpoint,
                source_variant,
            )] = current
            variants[(
                family,
                registration.grammar,
                registration.form,
                registration.prometheus_type,
                registration.shape,
                registration.priority,
                registration.endpoint,
                registration.classification,
            )].append(registration)

    result = []
    for (family, grammar, form, prometheus_type, shape, priority_value, endpoint, classification), registrations in sorted(
        variants.items(), key=lambda item: tuple("" if value is None else str(value) for value in item[0])
    ):
        source_variants = tuple(sorted({item.source_variant for item in registrations}, key=SOURCE_VARIANTS.index))
        source_locations = {
            (
                f"ceph_{item.source_variant}",
                path,
                line_start,
                line_end,
            )
            for item in registrations
            for path, line_start, line_end in (
                ((item.source_path, item.line_start, item.line_end),) + item.extra_locations
            )
        }
        dependency_locations = {
            location for item in registrations for location in item.dependency_locations
        }
        locations = tuple(sorted(source_locations | dependency_locations))
        result.append(
            MergedRegistration(
                exact_family=family,
                grammar=grammar,
                form=form,
                prometheus_type=prometheus_type,
                shape=shape,
                priority=priority_value,
                endpoint=endpoint,
                classification=classification,
                source_variants=source_variants,
                locations=locations,
            )
        )
    return result


def generate_registry(source_roots: dict[str, Path]) -> str:
    by_variant: dict[str, list[Registration]] = {}
    for release in RELEASES:
        if release not in source_roots:
            raise ValueError(f"missing source root for release {release}")
        registrations, unclassified = parse_release(source_roots[release], release)
        if unclassified:
            detail = ", ".join(f"{count}x {path}: {expr}" for (path, expr), count in sorted(unclassified.items()))
            raise ValueError(f"{release} has unclassified metric constructors: {detail}")
        by_variant[release] = registrations
    for source in ("nvmeof", "prometheus_client_python"):
        if source not in source_roots:
            raise ValueError(f"missing source root for {source}")
    by_variant["nvmeof"] = parse_nvmeof_registrations(
        source_roots["nvmeof"],
        source_roots["prometheus_client_python"],
    )
    merged = merge_registrations(by_variant)
    used_grammars: dict[str, set[str]] = defaultdict(set)
    for registration in merged:
        if registration.grammar is not None:
            used_grammars[registration.grammar].add(registration.form)

    lines = [
        "# SPDX-License-Identifier: GPL-3.0-or-later",
        "",
        "version: v1",
        "profile: ceph",
        "generated: true",
        "family_grammars:",
    ]
    for grammar_id in sorted(used_grammars):
        grammar = GRAMMARS[grammar_id]
        lines.append(f"  {grammar_id}:")
        if grammar_id == "librbd_pwl":
            lines.append("    interpretation: longest_known_suffix")
        lines.append("    forms:")
        for form in sorted(used_grammars[grammar_id]):
            suffix = "" if grammar.terminal_identity else form
            separator = "" if grammar.terminal_identity else "_"
            lines.extend([
                f"      {form}:",
                f"        canonical: {{prefix: {_yaml(grammar.canonical_prefix)}, suffix: {_yaml(suffix)}}}",
                "        embedded:",
                f"          prefix: {_yaml(grammar.embedded_prefix)}",
            ])
            if grammar.excluded_prefixes:
                excluded = ", ".join(_yaml(prefix) for prefix in grammar.excluded_prefixes)
                lines.append(f"          excluded_prefixes: [{excluded}]")
            lines.extend([
                f"          suffix: {_yaml(suffix)}",
                f"          separator: {_yaml(separator)}",
                f"          identity_slot: {{name: {grammar.identity}, nonempty: true}}",
            ])
    lines.append("groups:")
    used_ids: dict[str, tuple[str | None, str | None, str, str, str, int, str, str | None]] = {}
    for endpoint in (
        "daemon_perf",
        "exporter_perf",
        "mgr_perf",
        "mgr_synthetic",
        "mgr_rbd_stats",
        "exporter_self",
        "nvmeof",
    ):
        registrations = [item for item in merged if item.endpoint == endpoint]
        if not registrations:
            continue
        lines.extend([f"  {endpoint}:", "    registrations:"])
        for registration in registrations:
            key = (
                registration.exact_family,
                registration.grammar,
                registration.form,
                registration.prometheus_type,
                registration.shape,
                registration.priority,
                registration.endpoint,
                registration.classification,
            )
            registration_id = _registration_id(registration, used_ids, key)
            lines.append(f"      {registration_id}:")
            if registration.exact_family is not None:
                lines.append(f"        family: {{exact: {_yaml(registration.exact_family)}}}")
            else:
                lines.append(f"        family: {{grammar: {registration.grammar}, form: {registration.form}}}")
                lines.extend(_render_raw_branches(registration))
            prometheus = f"type: {registration.prometheus_type}, shape: {registration.shape}"
            if registration.classification is not None:
                prometheus += f", classification: {registration.classification}"
            lines.append(f"        prometheus: {{{prometheus}}}")
            lines.extend([
                "        when:",
                "          any:",
            ])
            lines.extend(_render_availability(registration))
            lines.extend([
                "        components:",
                "          value: {wire_role: scalar}",
                "        source_locations:",
            ])
            for upstream, path, line_start, line_end in registration.locations:
                lines.extend([
                    f"          - upstream: {upstream}",
                    f"            path: {_yaml(path)}",
                    f"            range: {{start: {line_start}, end: {line_end}}}",
                ])
    return "\n".join(lines) + "\n"


def _render_raw_branches(registration: MergedRegistration) -> list[str]:
    if registration.grammar is None:
        raise ValueError("raw branches require a grammar registration")
    if registration.grammar == "objecter":
        return ["        raw_branches: {canonical: {}, embedded: {}}"]
    if registration.grammar != "rgw_sync":
        return ["        raw_branches: {embedded: {}}"]
    if registration.endpoint == "exporter_perf":
        return ["        raw_branches: {canonical: {}}"]
    if registration.endpoint == "mgr_perf":
        return ["        raw_branches: {embedded: {}}"]
    if registration.endpoint != "daemon_perf":
        raise ValueError(f"RGW sync grammar has unsupported endpoint {registration.endpoint!r}")
    return [
        "        raw_branches:",
        "          canonical:",
        "            when: {any: [{all: [{axis: source, op: eq, value: ceph_exporter}]}]}",
        "          embedded:",
        "            when: {any: [{all: [{axis: source, op: eq, value: mgr}]}]}",
    ]


def _render_availability(registration: MergedRegistration) -> list[str]:
    if registration.endpoint == "nvmeof":
        if registration.source_variants != ("nvmeof",):
            raise ValueError(f"NVMe-oF registration has invalid source variants {registration.source_variants}")
        return [
            "            - all:",
            "                - {axis: source, op: eq, value: nvmeof}",
        ]
    if not registration.source_variants or any(item not in RELEASES for item in registration.source_variants):
        raise ValueError(
            f"Ceph core registration has invalid source variants {registration.source_variants}"
        )
    release_predicate = (
        f"{{axis: release, op: eq, value: {registration.source_variants[0]}}}"
        if len(registration.source_variants) == 1
        else "{axis: release, op: in, values: [" + ", ".join(registration.source_variants) + "]}"
    )
    if registration.endpoint == "exporter_self":
        return [
            "            - all:",
            "                - {axis: source, op: eq, value: ceph_exporter}",
            f"                - {release_predicate}",
        ]
    if registration.endpoint == "mgr_synthetic":
        return [
            "            - all:",
            "                - {axis: source, op: eq, value: mgr}",
            f"                - {release_predicate}",
        ]
    if registration.endpoint == "mgr_rbd_stats":
        return [
            "            - all:",
            "                - {axis: source, op: eq, value: mgr}",
            f"                - {release_predicate}",
            "                - {axis: mgr_rbd_stats, op: eq, value: enabled}",
        ]
    exporter_clause = [
        "            - all:",
        "                - {axis: source, op: eq, value: ceph_exporter}",
        f"                - {release_predicate}",
        f"                - {{axis: perf_priority_limit, op: min, value: {registration.priority + 1}}}",
    ]
    mgr_clause = [
        "            - all:",
        "                - {axis: source, op: eq, value: mgr}",
        f"                - {release_predicate}",
        "                - {axis: mgr_perf_counters, op: eq, value: enabled}",
        f"                - {{axis: perf_priority_limit, op: min, value: {registration.priority + 1}}}",
    ]
    if registration.endpoint == "exporter_perf":
        return exporter_clause
    if registration.endpoint == "mgr_perf":
        return mgr_clause
    if registration.endpoint != "daemon_perf":
        raise ValueError(f"unsupported endpoint {registration.endpoint!r}")
    return exporter_clause + mgr_clause


def _registration_id(
    registration: MergedRegistration,
    used: dict[str, tuple[str | None, str | None, str, str, str, int, str, str | None]],
    key: tuple[str | None, str | None, str, str, str, int, str, str | None],
) -> str:
    value = registration.exact_family or f"{registration.grammar}_{registration.form}"
    base = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    if not base or not base[0].isalpha():
        base = "metric_" + base
    if registration.endpoint in {"exporter_perf", "mgr_perf"}:
        base += "_" + registration.endpoint
    candidate = base
    if candidate in used and used[candidate] != key:
        qualifiers = [registration.prometheus_type, f"p{registration.priority}"]
        if registration.classification is not None:
            qualifiers.append(registration.classification)
        candidate = "_".join((base, *qualifiers))
    if candidate in used and used[candidate] != key:
        candidate += "_" + "_".join(registration.source_variants)
    if candidate in used and used[candidate] != key:
        raise ValueError(f"registration ID collision for {key}")
    used[candidate] = key
    return candidate


def _yaml(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> None:
    print(generate_registry(SOURCE_ROOTS), end="")


if __name__ == "__main__":
    main()
