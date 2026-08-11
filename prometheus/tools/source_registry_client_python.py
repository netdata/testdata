#!/usr/bin/env python3
"""Mechanical prometheus_client Python source facts shared by registry generators."""

from __future__ import annotations

import ast
import hashlib
import json


CLIENT_METRICS_AST_FINGERPRINT = "da91514df6b25f19f34ae9e99ca85a47a49201e9d8187f6422a0571397e56744"


def ast_fingerprint(source: str) -> str:
    """Return a line-independent fingerprint of one complete Python module."""
    canonical = json.dumps(
        _canonical_ast(ast.parse(source)),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def require_ast_fingerprints(
    sources: dict[str, str],
    expected: dict[str, str],
    description: str,
) -> None:
    """Require the exact reviewed executable shapes for a Python source closure."""
    if set(sources) != set(expected):
        missing = sorted(set(expected) - set(sources))
        unexpected = sorted(set(sources) - set(expected))
        raise ValueError(
            f"{description} source closure differs: missing={missing}, unexpected={unexpected}"
        )
    for path in sorted(expected):
        actual = ast_fingerprint(sources[path])
        if actual != expected[path]:
            raise ValueError(
                f"{description} source shape fingerprint for {path} is {actual}; "
                f"reviewed fingerprint is {expected[path]}"
            )


def parse_created_emitters(
    source: str,
    class_names: tuple[str, ...] = ("Counter", "Summary", "Histogram"),
    *,
    expected_ast_fingerprint: str = CLIENT_METRICS_AST_FINGERPRINT,
) -> dict[str, tuple[int, int]]:
    """Return source ranges for classes that emit one gated _created sample."""
    if not class_names or len(class_names) != len(set(class_names)):
        raise ValueError("created-emitter class names must be nonempty and unique")
    actual_fingerprint = ast_fingerprint(source)
    if actual_fingerprint != expected_ast_fingerprint:
        raise ValueError(
            "prometheus_client metrics source shape fingerprint "
            f"{actual_fingerprint} does not match reviewed fingerprint "
            f"{expected_ast_fingerprint}"
        )

    tree = ast.parse(source)
    result: dict[str, tuple[int, int]] = {}
    for class_name in class_names:
        classes = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ]
        if len(classes) != 1:
            raise ValueError(f"expected exactly one {class_name} class, found {len(classes)}")
        methods = [
            node
            for node in classes[0].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_child_samples"
        ]
        if len(methods) != 1:
            raise ValueError(f"{class_name} must define exactly one _child_samples method")
        method = methods[0]
        created_samples = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and _terminal_name(node.func) == "Sample"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "_created"
        ]
        if len(created_samples) != 1 or not _guarded_by_created_gate(created_samples[0], method):
            raise ValueError(f"{class_name} does not have one gated _created sample")
        result[class_name] = (method.lineno, method.end_lineno or method.lineno)
    return result


def _terminal_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _guarded_by_created_gate(node: ast.AST, method: ast.AST) -> bool:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(method):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    current = node
    while current is not method:
        parent = parents.get(current)
        if parent is None:
            return False
        if (
            isinstance(parent, ast.If)
            and current in parent.body
            and isinstance(parent.test, ast.Name)
            and parent.test.id == "_use_created"
        ):
            return True
        current = parent
    return False


def _canonical_ast(value: object) -> object:
    if isinstance(value, ast.AST):
        fields = []
        for name, field in ast.iter_fields(value):
            normalized = _canonical_ast(field)
            if normalized is None or normalized == []:
                continue
            fields.append([name, normalized])
        return [type(value).__name__, fields]
    if isinstance(value, list):
        return [_canonical_ast(item) for item in value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if value is Ellipsis:
        return ["ellipsis"]
    if isinstance(value, complex):
        return ["complex", value.real, value.imag]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported AST field value {type(value).__name__}")
