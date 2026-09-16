"""Logical agent-profile configuration separated from runtime/model placement."""

from __future__ import annotations

from dataclasses import dataclass

from execraft.orchestrate.scheduler import AgentCapability


@dataclass(frozen=True)
class AgentExecutionPolicy:
    """Per-profile execution/security hints retained across runtime migration."""

    timeout_seconds: int = 1800
    inactivity_timeout_seconds: int = 900
    output_silence_timeout_seconds: int = 0
    first_output_timeout_seconds: int = 0
    max_output_bytes: int = 64 * 1024 * 1024
    max_internal_retry_delay_seconds: int = 120
    live_sessions: bool = True
    auto_approve: bool = False
    sandbox: str = "workspace-write"
    permission_mode: str = "acceptEdits"
    agent_by_capability: tuple[tuple[AgentCapability, str], ...] = ()
    format_repair_agent: str = ""
    effort: str = ""
    effort_by_capability: tuple[tuple[AgentCapability, str], ...] = ()
    dangerously_skip_permissions: bool = False
    sandbox_enabled: bool = False
    policy_paths: tuple[str, ...] = ()

    def agent_for_capability(self, capability: AgentCapability) -> str:
        return dict(self.agent_by_capability).get(capability, "")

    def effort_for_capability(self, capability: AgentCapability) -> str:
        return dict(self.effort_by_capability).get(capability, self.effort)

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "timeout_seconds": self.timeout_seconds,
            "inactivity_timeout_seconds": self.inactivity_timeout_seconds,
            "output_silence_timeout_seconds": self.output_silence_timeout_seconds,
            "first_output_timeout_seconds": self.first_output_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_internal_retry_delay_seconds": self.max_internal_retry_delay_seconds,
            "live_sessions": self.live_sessions,
            "auto_approve": self.auto_approve,
            "sandbox": self.sandbox,
            "permission_mode": self.permission_mode,
            "dangerously_skip_permissions": self.dangerously_skip_permissions,
            "sandbox_enabled": self.sandbox_enabled,
        }
        if self.agent_by_capability:
            result["agent_by_capability"] = {
                capability.value: agent
                for capability, agent in self.agent_by_capability
            }
        if self.format_repair_agent:
            result["format_repair_agent"] = self.format_repair_agent
        if self.effort:
            result["effort"] = self.effort
        if self.effort_by_capability:
            result["effort_by_capability"] = {
                capability.value: effort
                for capability, effort in self.effort_by_capability
            }
        if self.policy_paths:
            result["policy_paths"] = list(self.policy_paths)
        return result


@dataclass(frozen=True)
class AgentProfileConfig:
    """Stable scheduler candidate referencing independent execution dimensions."""

    id: str
    name: str
    enabled: bool
    capabilities: frozenset[AgentCapability]
    runtime_id: str
    model_route_id: str = ""
    target_id: str = ""
    aliases: tuple[str, ...] = ()
    priority: int = 0
    capability_weight: int = 50
    capability_weights: tuple[tuple[AgentCapability, int], ...] = ()
    max_complexity: int = 100
    max_complexity_by_capability: tuple[tuple[AgentCapability, int], ...] = ()
    concurrency_group: str = ""
    skill_set: tuple[str, ...] = ()
    policy: AgentExecutionPolicy = AgentExecutionPolicy()

    @property
    def candidate_id(self) -> str:
        """Runtime-neutral scheduler identity introduced without a mass rename."""

        return self.id

    @property
    def provider_id(self) -> str:
        """Compatibility alias for schema-v3/provider-centric callers."""

        return self.id

    def weight_for_capability(self, capability: AgentCapability) -> int:
        return dict(self.capability_weights).get(capability, self.capability_weight)

    def max_complexity_for(self, capability: AgentCapability) -> int:
        return dict(self.max_complexity_by_capability).get(
            capability, self.max_complexity
        )

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "runtime": self.runtime_id,
            "enabled": self.enabled,
            "capabilities": sorted(item.value for item in self.capabilities),
            "priority": self.priority,
            "capability_weight": self.capability_weight,
            "max_complexity": self.max_complexity,
            "concurrency_group": self.concurrency_group,
            "policy": self.policy.as_mapping(),
        }
        if self.name != self.id:
            result["name"] = self.name
        if self.model_route_id:
            result["model_route"] = self.model_route_id
        if self.target_id:
            result["target"] = self.target_id
        if self.aliases:
            result["aliases"] = list(self.aliases)
        if self.capability_weights:
            result["capability_weights"] = {
                capability.value: weight
                for capability, weight in self.capability_weights
            }
        if self.max_complexity_by_capability:
            result["max_complexity_by_capability"] = {
                capability.value: maximum
                for capability, maximum in self.max_complexity_by_capability
            }
        if self.skill_set:
            result["skills"] = list(self.skill_set)
        return result
