"""Small deterministic directed-graph helpers for Project Execution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .models import ProjectExecutionError


def topological_order(
    adjacency: Mapping[str, Iterable[str]],
    *,
    label: str,
) -> tuple[str, ...]:
    """Return dependency-first order and raise with a useful cycle path."""

    visiting: set[str] = set()
    visited: set[str] = set()
    result: list[str] = []

    def visit(node: str, trail: tuple[str, ...]) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle = " -> ".join((*trail, node))
            raise ProjectExecutionError(f"{label} must be acyclic: {cycle}")

        visiting.add(node)
        for dependency in adjacency.get(node, ()):
            visit(dependency, (*trail, node))
        visiting.remove(node)
        visited.add(node)
        result.append(node)

    for node in adjacency:
        visit(node, ())
    return tuple(result)


def assert_acyclic(
    adjacency: Mapping[str, Iterable[str]],
    *,
    label: str,
) -> None:
    """Raise when a dependency graph is cyclic."""

    topological_order(adjacency, label=label)
