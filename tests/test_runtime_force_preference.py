from execraft.orchestrate.models import WorkPackage
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.scheduler import AgentAdapter, AgentCapability, Availability


def test_work_package_force_binding_round_trips():
    package = WorkPackage(
        id="WP1",
        title="Work Package",
        agent_preferences={"implement": ["oc"]},
        agent_preference_binding_roles=["implement"],
    )
    restored = WorkPackage.from_mapping(package.as_mapping())
    assert restored.agent_preferences == {"implement": ["oc"]}
    assert restored.agent_preference_binding_roles == ["implement"]


class _Adapter(AgentAdapter):
    def __init__(self, provider_id: str):
        self._provider_id = provider_id

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {AgentCapability.IMPLEMENT}

    def execute(self, handoff):  # pragma: no cover - selection only
        return {"ok": True, "work_package_id": handoff.work_package_id}


def test_force_binding_excludes_unselected_scheduler_candidates(tmp_path):
    orchestrator = ProjectOrchestrator(
        "runtime-force",
        config=OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        ),
    )
    package = WorkPackage(
        id="WP1",
        title="Work Package",
        agent_preferences={"implement": ["openclaw"]},
        agent_preference_binding_roles=["implement"],
    )
    orchestrator.register_agent(_Adapter("openclaw"))
    orchestrator.register_agent(_Adapter("native"))

    assert orchestrator._binding_role_exclusions(package, "implement") == {"native"}
    assert (
        orchestrator._select_agent_for_capability(
            AgentCapability.IMPLEMENT,
            package=package,
            preference_role="implement",
        )
        == "openclaw"
    )
