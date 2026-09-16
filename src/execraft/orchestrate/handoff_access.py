"""Filesystem roots a handoff must reach outside its working directory.

Sandboxed providers derive filesystem access from the agent working directory
plus a list of explicitly declared roots.  A task workspace whose repositories
are checked out elsewhere -- the common layout, where an ``ai-workspaces`` task
root sits next to the product tree -- therefore exposes the repositories to the
agent as unwritable.  That is indistinguishable from a genuinely read-only mount
and escalates as an unrecoverable environment incident instead of a scope
problem, so every handoff declares the repositories it already authorizes.

Declaring the roots does not widen the durable scope: read-only stages still run
under a read-only sandbox policy, and the commit and scope checks remain the
authority on what a package may change.  The one handoff that must not receive
them is an isolated parallel shard, whose working directory is a private
worktree copy: reaching the source repository would defeat the isolation, so
that call site clears the roots when it rewrites the working directory.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import OrchestrateError, WorkPackage
from .scheduler import StructuredHandoff

# Stages whose contract already spans the whole project rather than the
# package's declared repositories: supervision and its delegations may repair
# any configured repository, and scope recovery must classify candidates in
# undeclared ones.
PROJECT_WIDE_STAGES = frozenset({"supervise", "scope_recovery"})
DELEGATION_STAGE_PREFIX = "supervisor_delegate_"


def stage_is_project_wide(stage: str) -> bool:
    """Return whether ``stage`` is authorized across every repository."""

    return stage in PROJECT_WIDE_STAGES or stage.startswith(DELEGATION_STAGE_PREFIX)


def declare_access_roots(
    host: Any, handoff: StructuredHandoff, package: WorkPackage | None
) -> StructuredHandoff:
    """Return ``handoff`` with the roots its own scope already authorizes.

    Roots the caller declared explicitly are carried through; this only adds the
    repositories (and, for project-wide stages, the durable task dossier) that
    the handoff is entitled to reach but that fall outside its working
    directory.
    """

    project_wide = stage_is_project_wide(handoff.stage)
    dossier = getattr(host, "_task_dossier_dir", None)
    roots = access_roots(
        getattr(host, "_repository_paths", {}),
        [] if package is None else list(dict.fromkeys(package.affected_repositories)),
        handoff.working_directory,
        resolve=getattr(host, "_resolve_repo_path_if_available", None),
        include_all_repositories=project_wide or package is None,
        extra_roots=(
            [*handoff.additional_writable_roots]
            + ([dossier.parents[1]] if project_wide and dossier is not None else [])
        ),
    )
    if roots == list(handoff.additional_writable_roots):
        return handoff
    return replace(handoff, additional_writable_roots=roots)


def access_roots(
    repository_paths: Mapping[str, Path],
    repositories: Sequence[str],
    working_directory: str,
    *,
    resolve: Callable[[str], Path | None] | None = None,
    include_all_repositories: bool = False,
    extra_roots: Iterable[Path | str] = (),
) -> list[str]:
    """Return the sorted roots to declare alongside ``working_directory``.

    ``repositories`` is the package repository scope.  An empty scope means
    "unknown" and is treated as project-wide, matching how the scheduler reads
    it everywhere else.  ``resolve`` maps one repository id to its worktree and
    may return ``None`` or raise ``OrchestrateError`` for a repository that is
    absent; such a repository is skipped, because the stage checks -- not the
    sandbox hint -- are responsible for reporting it.
    """

    repository_candidates: list[Path] = []
    if include_all_repositories or not repositories:
        repository_candidates.extend(repository_paths.values())
    else:
        for repository_id in repositories:
            try:
                path = (
                    resolve(repository_id)
                    if resolve is not None
                    else repository_paths.get(repository_id)
                )
            except OrchestrateError:
                continue
            if path is not None:
                repository_candidates.append(path)
    # A stale repository entry must not be handed to a provider as a directory
    # to mount, and one already inside the working directory is redundant.
    # Roots the caller declared itself are its own contract and are carried
    # through as given.
    workdir = Path(working_directory).resolve() if working_directory else None
    resolved = list(dict.fromkeys(Path(item).resolve() for item in extra_roots))
    for candidate in repository_candidates:
        path = Path(candidate).resolve()
        if path in resolved or not path.is_dir():
            continue
        if workdir is not None and (path == workdir or workdir in path.parents):
            continue
        resolved.append(path)
    # Nested checkouts (a deployment repository that contains its sources) only
    # need their outermost root declared.
    return sorted(
        str(path)
        for path in resolved
        if not any(other in path.parents for other in resolved)
    )
