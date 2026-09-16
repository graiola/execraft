from types import SimpleNamespace

from execraft.runtime.operator_control import compile_runtime_preference_update


class Config:
    def __init__(self):
        self.agents = (
            SimpleNamespace(id="native", enabled=True, capabilities=("implement",), runtime_id="native", model_route_id="cloud", target_id=""),
            SimpleNamespace(id="openclaw", enabled=True, capabilities=("implement",), runtime_id="oc", model_route_id="local", target_id="gpu"),
        )
        self.model_routes = (
            SimpleNamespace(id="cloud", default_target=""),
            SimpleNamespace(id="local", default_target="gpu"),
        )
    def model_route(self, item_id):
        return next(item for item in self.model_routes if item.id == item_id)


def test_prefer_reuses_existing_scheduler_preference_seam_without_binding():
    update = compile_runtime_preference_update(
        Config(),
        existing_preferences={"review": ["reviewer"]},
        role="implement",
        mode="prefer",
        runtime_id="oc",
    )
    assert update.agent_preferences == {
        "review": ["reviewer"],
        "implement": ["openclaw", "native"],
    }
    assert update.binding_roles == ()


def test_force_marks_selected_role_as_binding_and_automatic_clears_it():
    forced = compile_runtime_preference_update(
        Config(),
        existing_preferences={},
        role="implement",
        mode="force",
        runtime_id="oc",
        invocation_active=True,
    )
    assert forced.agent_preferences["implement"] == ["openclaw"]
    assert forced.binding_roles == ("implement",)
    assert forced.plan.hot_migration_supported is False
    assert forced.plan.requires_cancel_for_immediate is True

    automatic = compile_runtime_preference_update(
        Config(),
        existing_preferences=forced.agent_preferences,
        existing_binding_roles=forced.binding_roles,
        role="implement",
        mode="automatic",
    )
    assert "implement" not in automatic.agent_preferences
    assert automatic.binding_roles == ()
