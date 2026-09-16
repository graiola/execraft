"""Turn-scoped lazy-skill policy for managed OpenClaw execution.

This module keeps OpenClaw skill projection/rendering/telemetry out of the
Gateway executor. Execraft's canonical skill catalog and StructuredHandoff remain
the authority; this is only a runtime delivery strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from execraft.orchestrate.context_budget import estimate_tokens
from execraft.orchestrate.scheduler import StructuredHandoff, build_agent_prompt
from execraft.runtime_config import OpenClawMode, OpenClawRuntimeOptions

from .openclaw_skills import (
    OpenClawSkillProjection,
    load_openclaw_skill_projection,
    materialize_openclaw_skills,
)


@dataclass(frozen=True)
class OpenClawSkillTurn:
    """Prepared selected-skill state for one OpenClaw turn."""

    projection: OpenClawSkillProjection | None
    delivery_mode: str
    selected_skill_ids: tuple[str, ...]
    lazy: bool

    def render_prompt(self, handoff: StructuredHandoff) -> tuple[str, str]:
        """Return actual runtime prompt and the equivalent inline baseline."""

        prompt = build_agent_prompt(
            handoff,
            embed_workflow_skill_instructions=not self.lazy,
        )
        inline = build_agent_prompt(handoff) if self.lazy else prompt
        return prompt, inline

    def telemetry(
        self,
        *,
        prompt: str,
        inline_prompt: str,
        events: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return token/projection/read telemetry without affecting correctness."""

        prompt_savings_bytes = max(
            0,
            len(inline_prompt.encode("utf-8")) - len(prompt.encode("utf-8")),
        )
        prompt_savings_tokens = max(
            0,
            estimate_tokens(inline_prompt) - estimate_tokens(prompt),
        )
        skill_reads = count_openclaw_skill_reads(events, self.projection)
        metadata: dict[str, Any] = {
            "skill_delivery_mode": self.delivery_mode,
            "skill_ids": list(self.selected_skill_ids),
            "skill_read_count": sum(skill_reads.values()),
            "skill_reads": skill_reads,
            "skill_prompt_savings_bytes": prompt_savings_bytes,
            "skill_prompt_savings_estimated_tokens": prompt_savings_tokens,
        }
        if self.projection is not None:
            metadata.update(
                {
                    "skill_manifest_sha256": self.projection.manifest_sha256,
                    "skill_projection_changed": self.projection.changed,
                    "skill_instruction_bytes": self.projection.instruction_bytes,
                    "skill_instruction_estimated_tokens": (
                        self.projection.instruction_estimated_tokens
                    ),
                }
            )
        return metadata


def prepare_openclaw_skill_turn(
    options: OpenClawRuntimeOptions,
    *,
    workspace: Path | None,
    handoff: StructuredHandoff,
    has_session_ref: bool,
) -> OpenClawSkillTurn:
    """Prepare exact selected skills and choose managed-lazy vs inline delivery."""

    selected = tuple(handoff.workflow_skills)
    skill_ids = tuple(
        str(item.get("id", ""))
        for item in selected
        if isinstance(item, Mapping) and item.get("id")
    )
    if options.mode != OpenClawMode.MANAGED:
        return OpenClawSkillTurn(
            projection=None,
            delivery_mode="inline_external_fallback" if selected else "none",
            selected_skill_ids=skill_ids,
            lazy=False,
        )

    projection: OpenClawSkillProjection | None = None
    if workspace is None:
        if selected:
            raise ValueError(
                "managed OpenClaw lazy skills require an Execraft-owned skill workspace"
            )
    else:
        format_repair = bool(handoff.execution_context.get("format_repair"))
        if format_repair and has_session_ref and not selected:
            # Format repair intentionally strips the original skill bodies. Keep
            # only a verified snapshot from the compatible session workspace. A
            # missing/corrupt snapshot is cleared so the session cannot read
            # tampered generated instructions during the format-only turn.
            projection = load_openclaw_skill_projection(workspace)
            if projection is None:
                projection = materialize_openclaw_skills(workspace, ())
        else:
            projection = materialize_openclaw_skills(workspace, selected)
    return OpenClawSkillTurn(
        projection=projection,
        delivery_mode="lazy" if selected else "none",
        selected_skill_ids=skill_ids,
        lazy=bool(selected),
    )


def count_openclaw_skill_reads(
    events: Sequence[Mapping[str, Any]],
    projection: OpenClawSkillProjection | None,
) -> dict[str, int]:
    """Count best-effort tool/read events referencing generated SKILL.md files."""

    if projection is None or not projection.skills:
        return {}
    counts = {item.id: 0 for item in projection.skills}
    needles = {
        item.id: (
            str(item.path).replace("\\", "/"),
            f"skills/{item.id}/SKILL.md",
        )
        for item in projection.skills
    }
    for payload in events:
        stream = str(payload.get("stream", "")).lower().replace("-", "_")
        if "tool" not in stream and "read" not in stream:
            continue
        flattened = tuple(_event_strings(payload))
        for skill_id, candidates in needles.items():
            if any(
                candidate in value.replace("\\", "/")
                for candidate in candidates
                for value in flattened
            ):
                counts[skill_id] += 1
    return {key: value for key, value in counts.items() if value}


def _event_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _event_strings(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _event_strings(item)
