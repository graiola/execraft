"""Dashboard integration for task-scoped temporary provider promotions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from execraft.gui.errors import GuiError
from execraft.orchestrate.identity import OrchestrationStorageIdentity
from execraft.orchestrate.journal import EventJournal
from execraft.orchestrate.provider_promotion import ProviderPromotionStore


class ProviderPromotionDashboardMixin:
    """Expose safe hot promotion operations without bloating the dashboard server.

    A promotion deliberately differs from provider-health mutation: it is a
    task-local scheduling policy update and remains safe while the orchestrator
    owns the run. The runtime watches the same atomic state file and picks up the
    change on its next eligibility check.
    """

    state_dir: Path
    task_id: str
    project_id: str
    storage_identity: OrchestrationStorageIdentity
    promotion_store: ProviderPromotionStore
    _agent_configs: Callable[..., list[Any]]

    def _init_provider_promotions(self) -> None:
        self.promotion_store = ProviderPromotionStore(
            self.state_dir / "provider-promotions.json"
        )

    def set_agent_promotion(
        self,
        agent_id: str,
        *,
        capabilities: list[str],
        promoted_max_complexity: int = 100,
        duration_seconds: int = 4 * 60 * 60,
        fallback_only: bool = True,
        allow_final_review: bool = False,
        package_id: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """Create task-scoped promotions without stopping an active driver."""

        agent_id = str(agent_id).strip()
        configs = {item.provider_id: item for item in self._agent_configs()}
        config = configs.get(agent_id)
        if config is None:
            raise GuiError(f"unknown configured provider: {agent_id}")
        if not config.enabled:
            raise GuiError(f"configured provider is disabled: {agent_id}")

        supported = {item.value: item for item in config.capabilities}
        requested = _unique_strings(capabilities)
        if not requested:
            raise GuiError("select at least one provider capability to promote")
        unknown = [item for item in requested if item not in supported]
        if unknown:
            raise GuiError(
                f"provider {agent_id} does not support: {', '.join(sorted(unknown))}"
            )
        ceiling = _validate_ceiling(promoted_max_complexity)
        duration = _validate_duration(duration_seconds)

        created = []
        for capability_name in requested:
            capability = supported[capability_name]
            base = config.max_complexity_for(capability)
            if ceiling <= base:
                continue
            promotion = self.promotion_store.promote(
                agent_id,
                capability_name,
                base_max_complexity=base,
                promoted_max_complexity=ceiling,
                duration_seconds=duration,
                fallback_only=fallback_only,
                allow_final_review=allow_final_review,
                package_id=str(package_id).strip(),
                reason=reason,
                source="gui",
            )
            created.append(promotion.as_mapping())
        if not created:
            raise GuiError(
                "the requested ceiling does not exceed this provider's configured limits"
            )
        journal = EventJournal(self.storage_identity.journal_path)
        for promotion in created:
            journal.append(
                "provider_promotion_created",
                {**promotion, "task_id": self.task_id, "project_id": self.project_id},
            )
        return {"agent_id": agent_id, "promotions": created, "hot_reload": True}

    def revoke_agent_promotion(
        self,
        agent_id: str,
        *,
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        """Revoke active promotions for one provider in the current task."""

        agent_id = str(agent_id).strip()
        configured = {item.provider_id for item in self._agent_configs()}
        if agent_id not in configured:
            raise GuiError(f"unknown configured provider: {agent_id}")
        removed = self.promotion_store.revoke(
            agent_id, capabilities=capabilities or None
        )
        journal = EventJournal(self.storage_identity.journal_path)
        for promotion in removed:
            journal.append(
                "provider_promotion_revoked",
                {
                    **promotion.as_mapping(),
                    "task_id": self.task_id,
                    "project_id": self.project_id,
                    "source": "gui",
                },
            )
        return {
            "agent_id": agent_id,
            "revoked": [item.as_mapping() for item in removed],
        }

    def _provider_promotion_fields(self, config: Any) -> dict[str, Any]:
        """Return provider-card fields while keeping static policy explicit."""

        promotions = self.promotion_store.list(provider_id=config.provider_id)
        task_wide = {item.capability: item for item in promotions if not item.package_id}
        capabilities = sorted(config.capabilities, key=lambda value: value.value)
        return {
            "effective_max_complexity": {
                item.value: max(
                    config.max_complexity_for(item),
                    task_wide[item.value].promoted_max_complexity
                    if item.value in task_wide
                    else config.max_complexity_for(item),
                )
                for item in capabilities
            },
            "promotions": [item.as_mapping() for item in promotions],
        }


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _validate_ceiling(value: Any) -> int:
    try:
        ceiling = int(value)
    except (TypeError, ValueError) as exc:
        raise GuiError("temporary complexity ceiling must be an integer") from exc
    if not 0 <= ceiling <= 100:
        raise GuiError("temporary complexity ceiling must be between 0 and 100")
    return ceiling


def _validate_duration(value: Any) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError) as exc:
        raise GuiError("promotion duration must be an integer number of seconds") from exc
    if duration < 60 or duration > 7 * 24 * 60 * 60:
        raise GuiError("promotion duration must be between 60 seconds and 7 days")
    return duration
