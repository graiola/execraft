"""Runtime-neutral model route configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelRouteConfig:
    """Describe a logical model independently from the agent runtime.

    Endpoint/credential fields are intentionally declarative here. They are
    parsed and validated now so schema v4 has the correct ownership boundary;
    actual endpoint discovery and OpenCode/OpenClaw projection is implemented
    in later routing phases.
    """

    id: str
    provider: str
    model: str
    provider_alias: str = ""
    endpoint: str = ""
    credential_ref: str = ""
    api_family: str = ""
    context_window: int | None = None
    capabilities: frozenset[str] = frozenset()
    default_target: str = ""

    def reference_for_native_adapter(self, adapter: str) -> str:
        """Project the route onto the current Native adapter model syntax."""

        if adapter == "opencode":
            provider = self.provider_alias or self.provider
            return f"{provider}/{self.model}"
        return self.model

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "provider": self.provider,
            "model": self.model,
        }
        if self.provider_alias:
            result["provider_alias"] = self.provider_alias
        if self.endpoint:
            result["endpoint"] = self.endpoint
        if self.credential_ref:
            result["credential_ref"] = self.credential_ref
        if self.api_family:
            result["api_family"] = self.api_family
        if self.context_window is not None:
            result["context_window"] = self.context_window
        if self.capabilities:
            result["capabilities"] = sorted(self.capabilities)
        if self.default_target:
            result["default_target"] = self.default_target
        return result
