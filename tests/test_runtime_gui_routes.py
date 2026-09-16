from execraft.gui.routes import runtime


class FakeService:
    def preview_runtime_selection(self, **payload):
        return {"kind": "preview", **payload}

    def apply_runtime_selection(self, **payload):
        return {"kind": "apply", **payload}


def test_runtime_selection_routes_keep_cancel_switch_explicit():
    service = FakeService()
    preview = runtime.dispatch_post(
        service,
        "/api/runtime/selection/preview",
        {"package_id": "WP1", "role": "implement", "mode": "force", "runtime_id": "oc"},
    )
    assert preview["kind"] == "preview"
    assert preview["runtime_id"] == "oc"

    applied = runtime.dispatch_post(
        service,
        "/api/runtime/selection/apply",
        {"package_id": "WP1", "role": "implement", "mode": "force", "runtime_id": "oc", "cancel_and_switch": True},
    )
    assert applied["kind"] == "apply"
    assert applied["cancel_and_switch"] is True
