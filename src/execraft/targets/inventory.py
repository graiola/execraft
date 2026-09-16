"""Runtime-neutral physical execution/inference target inventory."""

from __future__ import annotations

from dataclasses import dataclass

from execraft.model_registry import ModelRouteRegistry
from execraft.targets.config import ExecutionTargetConfig, ExecutionTargetKind


@dataclass(frozen=True)
class ExecutionTargetInventory:
    """Validated target lookup independent from model-provider identity."""

    targets: tuple[ExecutionTargetConfig, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for target in self.targets:
            if target.id in seen:
                raise ValueError(f"duplicate execution target: {target.id}")
            seen.add(target.id)

    @classmethod
    def from_model_registry(cls, registry: ModelRouteRegistry) -> "ExecutionTargetInventory":
        return cls(registry.targets)

    @property
    def by_id(self) -> dict[str, ExecutionTargetConfig]:
        return {target.id: target for target in self.targets}

    @property
    def local(self) -> tuple[ExecutionTargetConfig, ...]:
        return tuple(target for target in self.targets if target.kind == ExecutionTargetKind.LOCAL)

    @property
    def inference_endpoints(self) -> tuple[ExecutionTargetConfig, ...]:
        return tuple(
            target
            for target in self.targets
            if target.kind == ExecutionTargetKind.INFERENCE_ENDPOINT
        )

    @property
    def remote_runtimes(self) -> tuple[ExecutionTargetConfig, ...]:
        return tuple(
            target
            for target in self.targets
            if target.kind == ExecutionTargetKind.REMOTE_RUNTIME
        )
