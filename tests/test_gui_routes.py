"""Unit tests for domain-owned GUI API routing and strict payload decoding."""

from __future__ import annotations

import pytest

from execraft.gui.errors import GuiError
from execraft.gui.routes import GuiApiRouter, RouteNotFound


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def home_snapshot(self):
        return {"projects": [{"id": "sample"}]}

    def open_catalog(self, *, acknowledged: bool):
        self.calls.append(("open_catalog", acknowledged))
        return {"mode": "home", "focused_project_id": ""}

    def open_project(self, project_id: str, *, acknowledged: bool):
        self.calls.append(("open_project", (project_id, acknowledged)))
        return {"mode": "home", "focused_project_id": project_id}

    def archive_catalog(self):
        self.calls.append(("archive_catalog", None))
        return {"archived_tasks": []}

    def archive_catalog_entry(
        self, kind: str, *, project_id: str, item_id: str, reason: str
    ):
        self.calls.append(
            ("archive", (kind, project_id, item_id, reason))
        )
        return {"archived": True}

    def delete_catalog_entry(
        self,
        kind: str,
        *,
        project_id: str,
        item_id: str,
        confirmation: str,
        delete_branches: bool,
    ):
        self.calls.append(
            (
                "delete",
                (kind, project_id, item_id, confirmation, delete_branches),
            )
        )
        return {"deleted": True}


    def roadmap_list(self, project_id: str):
        self.calls.append(("roadmap_list", project_id))
        return {"project_id": project_id, "roadmaps": []}

    def roadmap_get(self, project_id: str, roadmap_id: str):
        self.calls.append(("roadmap_get", (project_id, roadmap_id)))
        return {"project_id": project_id, "id": roadmap_id}

    def roadmap_create(self, project_id: str, **kwargs):
        self.calls.append(("roadmap_create", (project_id, kwargs)))
        return {"id": kwargs.get("roadmap_id") or "roadmap"}

    def roadmap_update_metadata(self, project_id: str, roadmap_id: str, **kwargs):
        self.calls.append(("roadmap_metadata", (project_id, roadmap_id, kwargs)))
        return {"id": roadmap_id}

    def roadmap_upsert_item(self, project_id: str, roadmap_id: str, **kwargs):
        self.calls.append(("roadmap_item", (project_id, roadmap_id, kwargs)))
        return {"id": roadmap_id}

    def roadmap_move_lane(self, project_id: str, roadmap_id: str, **kwargs):
        self.calls.append(("roadmap_lane_move", (project_id, roadmap_id, kwargs)))
        return {"id": roadmap_id}

    def roadmap_delete_item(self, project_id: str, roadmap_id: str, **kwargs):
        self.calls.append(("roadmap_item_delete", (project_id, roadmap_id, kwargs)))
        return {"id": roadmap_id}

    def roadmap_move_item(self, project_id: str, roadmap_id: str, **kwargs):
        self.calls.append(("roadmap_move", (project_id, roadmap_id, kwargs)))
        return {"id": roadmap_id}

    def roadmap_link_task(self, project_id: str, roadmap_id: str, **kwargs):
        self.calls.append(("roadmap_link", (project_id, roadmap_id, kwargs)))
        return {"id": roadmap_id}

    def roadmap_upsert_relation(self, project_id: str, roadmap_id: str, **kwargs):
        self.calls.append(("roadmap_relation", (project_id, roadmap_id, kwargs)))
        return {"id": roadmap_id}

    def roadmap_delete_relation(self, project_id: str, roadmap_id: str, **kwargs):
        self.calls.append(("roadmap_relation_delete", (project_id, roadmap_id, kwargs)))
        return {"id": roadmap_id}

    def roadmap_delete(self, project_id: str, roadmap_id: str, **kwargs):
        self.calls.append(("roadmap_delete", (project_id, roadmap_id, kwargs)))
        return {"id": roadmap_id, "deleted": True}

    def start_run(self, *, no_wait_for_agents: bool):
        self.calls.append(("start_run", no_wait_for_agents))
        return {"started": True}

    def set_package_pause(self, package_id: str, *, paused: bool, reason: str, apply_to_shards: bool):
        self.calls.append(("pause", (package_id, paused, reason, apply_to_shards)))
        return {"paused": paused}

    def commit_workspace_changes(self, **kwargs):
        self.calls.append(("commit", kwargs))
        return {"committed": True}

    def scope_approval_preview(self, package_id: str):
        self.calls.append(("scope_preview", package_id))
        return {"package_id": package_id, "candidate_paths": ["core:.github/workflows/ci.yml"]}

    def approve_protected_scope(self, package_id: str, *, expected_candidates: list[str]):
        self.calls.append(("scope_accept", (package_id, expected_candidates)))
        return {"approved": True}

    def operator_acceptance_preview(self, package_id: str):
        self.calls.append(("operator_acceptance_preview", package_id))
        return {"available": True, "package_id": package_id, "sequence": 17}

    def accept_operator_risk(self, package_id: str, *, reason: str, expected_sequence: int | None, acknowledged: bool):
        self.calls.append(("operator_acceptance", (package_id, reason, expected_sequence, acknowledged)))
        return {"accepted": True}


def test_router_delegates_project_home_reads() -> None:
    router = GuiApiRouter(_Service())
    assert router.get("/api/projects", {}) == {"projects": [{"id": "sample"}]}


def test_router_rejects_unknown_routes() -> None:
    router = GuiApiRouter(_Service())
    with pytest.raises(RouteNotFound):
        router.get("/api/not-real", {})
    with pytest.raises(RouteNotFound):
        router.post("/api/not-real", {})



def test_router_exposes_explicit_project_catalog_transition() -> None:
    service = _Service()
    router = GuiApiRouter(service)
    result = router.post("/api/session/catalog", {"acknowledged": True})
    assert result == {"mode": "home", "focused_project_id": ""}
    assert service.calls == [("open_catalog", True)]



def test_router_exposes_safe_project_workspace_transition() -> None:
    service = _Service()
    router = GuiApiRouter(service)
    result = router.post(
        "/api/session/project",
        {"project_id": "sample", "acknowledged": True},
    )
    assert result == {"mode": "home", "focused_project_id": "sample"}
    assert service.calls == [("open_project", ("sample", True))]

def test_router_exposes_archive_from_project_home_routes() -> None:
    service = _Service()
    router = GuiApiRouter(service)

    assert router.get("/api/archive", {}) == {"archived_tasks": []}
    result = router.post(
        "/api/archive/archive",
        {
            "kind": "task",
            "project_id": "sample",
            "id": "old-task",
            "reason": "superseded",
        },
    )

    assert result == {"archived": True}
    assert service.calls == [
        ("archive_catalog", None),
        ("archive", ("task", "sample", "old-task", "superseded")),
    ]


def test_catalog_transition_acknowledgement_is_strict() -> None:
    router = GuiApiRouter(_Service())
    with pytest.raises(GuiError, match="acknowledged must be a JSON boolean"):
        router.post("/api/session/catalog", {"acknowledged": "true"})

def test_mutation_booleans_are_strict_across_task_routes() -> None:
    router = GuiApiRouter(_Service())
    with pytest.raises(GuiError, match="no_wait_for_agents must be a JSON boolean"):
        router.post("/api/run/start", {"no_wait_for_agents": "false"})
    with pytest.raises(GuiError, match="paused must be a JSON boolean"):
        router.post(
            "/api/package/pause",
            {"package_id": "WP1", "paused": "false"},
        )
    with pytest.raises(GuiError, match="reviewed must be a JSON boolean"):
        router.post(
            "/api/workspace/commit",
            {
                "selections": {},
                "expected_digests": {},
                "subject": "test",
                "reviewed": "false",
            },
        )


def test_router_preserves_valid_task_mutations() -> None:
    service = _Service()
    router = GuiApiRouter(service)
    assert router.post("/api/run/start", {"no_wait_for_agents": True}) == {"started": True}
    assert service.calls == [("start_run", True)]
    assert "/api/agent/console" in router.protected_get_paths


def test_router_exposes_protected_scope_preview_and_acceptance() -> None:
    service = _Service()
    router = GuiApiRouter(service)

    preview = router.get("/api/scope/approval", {"package_id": ["WP22__WP22-S4"]})
    accepted = router.post(
        "/api/scope/accept",
        {
            "package_id": "WP22__WP22-S4",
            "expected_candidates": [
                "worker_a:.github/workflows/ci.yml",
                "worker_b:.github/workflows/ci.yml",
            ],
        },
    )

    assert preview["package_id"] == "WP22__WP22-S4"
    assert accepted == {"approved": True}
    assert service.calls == [
        ("scope_preview", "WP22__WP22-S4"),
        (
            "scope_accept",
            (
                "WP22__WP22-S4",
                [
                    "worker_a:.github/workflows/ci.yml",
                    "worker_b:.github/workflows/ci.yml",
                ],
            ),
        ),
    ]


def test_router_rejects_scalar_expected_scope_candidates() -> None:
    router = GuiApiRouter(_Service())
    with pytest.raises(GuiError, match="expected_candidates must be a JSON array of strings"):
        router.post(
            "/api/scope/accept",
            {"package_id": "WP22__WP22-S4", "expected_candidates": "core:file"},
        )


def test_router_exposes_permanent_catalog_delete_with_strict_branch_flag() -> None:
    service = _Service()
    router = GuiApiRouter(service)

    result = router.post(
        "/api/archive/delete",
        {
            "kind": "task",
            "project_id": "sample",
            "id": "old-task",
            "confirmation": "old-task",
            "delete_branches": False,
        },
    )

    assert result == {"deleted": True}
    assert service.calls == [
        ("delete", ("task", "sample", "old-task", "old-task", False))
    ]
    with pytest.raises(GuiError, match="delete_branches must be a JSON boolean"):
        router.post(
            "/api/archive/delete",
            {
                "kind": "task",
                "project_id": "sample",
                "id": "old-task",
                "confirmation": "old-task",
                "delete_branches": "false",
            },
        )


def test_router_exposes_operator_acceptance_preview_and_mutation() -> None:
    service = _Service()
    router = GuiApiRouter(service)

    preview = router.get("/api/operator-acceptance", {"package_id": ["WP24"]})
    accepted = router.post(
        "/api/operator-acceptance/accept",
        {
            "package_id": "WP24",
            "reason": "Defer real simulator evidence.",
            "expected_sequence": 17,
            "acknowledged": True,
        },
    )

    assert preview["sequence"] == 17
    assert accepted == {"accepted": True}
    assert service.calls == [
        ("operator_acceptance_preview", "WP24"),
        (
            "operator_acceptance",
            ("WP24", "Defer real simulator evidence.", 17, True),
        ),
    ]


def test_router_operator_acceptance_requires_boolean_acknowledgement() -> None:
    router = GuiApiRouter(_Service())
    with pytest.raises(GuiError, match="acknowledged must be a JSON boolean"):
        router.post(
            "/api/operator-acceptance/accept",
            {
                "package_id": "WP24",
                "reason": "Defer host evidence.",
                "expected_sequence": 17,
                "acknowledged": "true",
            },
        )


def test_router_exposes_roadmap_reads_and_mutations_with_strict_types() -> None:
    service = _Service()
    router = GuiApiRouter(service)

    assert router.get("/api/roadmaps", {"project_id": ["sample"]})["roadmaps"] == []
    assert router.get(
        "/api/roadmap",
        {"project_id": ["sample"], "roadmap_id": ["platform"]},
    )["id"] == "platform"

    created = router.post(
        "/api/roadmap/create",
        {
            "project_id": "sample",
            "roadmap_id": "platform",
            "title": "Platform",
            "description": "Plan",
        },
    )
    assert created["id"] == "platform"

    router.post(
        "/api/roadmap/item/upsert",
        {
            "project_id": "sample",
            "roadmap_id": "platform",
            "expected_revision": 2,
            "item": {"kind": "milestone", "title": "MVP"},
        },
    )
    router.post(
        "/api/roadmap/item/link-task",
        {
            "project_id": "sample",
            "roadmap_id": "platform",
            "expected_revision": 3,
            "task_id": "alpha",
            "order": 20,
        },
    )
    router.post(
        "/api/roadmap/item/move",
        {
            "project_id": "sample",
            "roadmap_id": "platform",
            "expected_revision": 4,
            "item_id": "task-alpha",
            "lane": "Platform",
            "target": "2026-10-01",
            "target_item_id": "mvp",
            "placement": "after",
        },
    )
    router.post(
        "/api/roadmap/lane/move",
        {
            "project_id": "sample",
            "roadmap_id": "platform",
            "expected_revision": 5,
            "lane": "Core",
            "target_lane": "Platform",
            "placement": "before",
        },
    )
    router.post(
        "/api/roadmap/relation/upsert",
        {
            "project_id": "sample",
            "roadmap_id": "platform",
            "expected_revision": 5,
            "from": "one",
            "to": "two",
            "kind": "blocks",
        },
    )
    deleted = router.post(
        "/api/roadmap/delete",
        {
            "project_id": "sample",
            "roadmap_id": "platform",
            "expected_revision": 6,
            "acknowledged": True,
        },
    )
    assert deleted["deleted"] is True
    assert any(call[0] == "roadmap_item" for call in service.calls)
    assert any(call[0] == "roadmap_link" for call in service.calls)
    assert any(call[0] == "roadmap_move" for call in service.calls)
    assert any(call[0] == "roadmap_relation" for call in service.calls)

    with pytest.raises(GuiError, match="expected_revision must be a JSON integer"):
        router.post(
            "/api/roadmap/item/delete",
            {
                "project_id": "sample",
                "roadmap_id": "platform",
                "expected_revision": "5",
                "item_id": "one",
            },
        )
    with pytest.raises(GuiError, match="acknowledged must be a JSON boolean"):
        router.post(
            "/api/roadmap/delete",
            {
                "project_id": "sample",
                "roadmap_id": "platform",
                "expected_revision": 5,
                "acknowledged": "true",
            },
        )
    with pytest.raises(GuiError, match="item must be a JSON object"):
        router.post(
            "/api/roadmap/item/upsert",
            {
                "project_id": "sample",
                "roadmap_id": "platform",
                "expected_revision": 5,
                "item": "milestone",
            },
        )
    with pytest.raises(GuiError, match=r"item\.order must be a JSON integer"):
        router.post(
            "/api/roadmap/item/upsert",
            {
                "project_id": "sample",
                "roadmap_id": "platform",
                "expected_revision": 5,
                "item": {"kind": "milestone", "title": "MVP", "order": "10"},
            },
        )
    with pytest.raises(GuiError, match=r"item\.schedule\.target must be a JSON string"):
        router.post(
            "/api/roadmap/item/upsert",
            {
                "project_id": "sample",
                "roadmap_id": "platform",
                "expected_revision": 5,
                "item": {
                    "kind": "milestone",
                    "title": "MVP",
                    "schedule": {"target": 20261015},
                },
            },
        )
    with pytest.raises(GuiError, match="lane must be a JSON string"):
        router.post(
            "/api/roadmap/item/move",
            {
                "project_id": "sample",
                "roadmap_id": "platform",
                "expected_revision": 5,
                "item_id": "task-alpha",
                "lane": False,
            },
        )


def test_roadmap_lane_move_route_requires_string_lanes() -> None:
    service = _Service()
    router = GuiApiRouter(service)
    with pytest.raises(GuiError, match="target_lane must be a JSON string"):
        router.post(
            "/api/roadmap/lane/move",
            {
                "project_id": "sample",
                "roadmap_id": "platform",
                "expected_revision": 5,
                "lane": "Core",
                "target_lane": False,
            },
        )
