#!/usr/bin/env python3
"""Mechanical prometheus_client Python source facts shared by registry generators."""

from __future__ import annotations

import ast


def parse_created_emitters(
    source: str,
    class_names: tuple[str, ...] = ("Counter", "Summary", "Histogram"),
) -> dict[str, tuple[int, int]]:
    """Return source ranges for classes that emit one gated _created sample."""
    if not class_names or len(class_names) != len(set(class_names)):
        raise ValueError("created-emitter class names must be nonempty and unique")

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
        uses_gate = any(
            isinstance(node, ast.Name) and node.id == "_use_created"
            for node in ast.walk(method)
        )
        if len(created_samples) != 1 or not uses_gate:
            raise ValueError(f"{class_name} does not have one gated _created sample")
        result[class_name] = (method.lineno, method.end_lineno or method.lineno)
    return result


def _terminal_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None
