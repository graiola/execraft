"""Deterministic prompt-composition metrics used by runtime benchmarks.

The measurements are intentionally tokenizer-agnostic and reuse Execraft's
existing conservative token estimator. Provider-reported usage remains the
source of truth whenever available; these fields explain where prompt size came
from and make architectural token regressions visible before OpenClaw exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .context_budget import estimate_tokens


_DYNAMIC_PROMPT_MARKER = "Handoff ID:"


@dataclass(frozen=True)
class PromptCompositionMetrics:
    prompt_bytes: int
    prompt_estimated_tokens: int
    stable_prefix_bytes: int
    stable_prefix_estimated_tokens: int
    skill_instruction_bytes: int
    skill_instruction_estimated_tokens: int

    def as_mapping(self) -> dict[str, int]:
        return {
            "prompt_bytes": self.prompt_bytes,
            "estimated_input_tokens": self.prompt_estimated_tokens,
            "stable_prefix_bytes": self.stable_prefix_bytes,
            "stable_prefix_estimated_tokens": self.stable_prefix_estimated_tokens,
            "skill_instruction_bytes": self.skill_instruction_bytes,
            "skill_instruction_estimated_tokens": self.skill_instruction_estimated_tokens,
        }


def _embedded_skill_instructions(
    prompt: str, skills: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    """Return selected skill bodies that are actually present in the prompt.

    This distinction matters once OpenClaw lazy skills stop embedding complete
    instructions: selection metadata may remain attached to the handoff while
    the invocation prompt contains only compact skill references.
    """

    embedded: list[str] = []
    for item in skills:
        text = str(item.get("instructions", "")).strip()
        if text and text in prompt:
            embedded.append(text)
    return tuple(embedded)


def measure_prompt_composition(
    prompt: str,
    *,
    workflow_skills: Sequence[Mapping[str, Any]] = (),
) -> PromptCompositionMetrics:
    """Measure stable-prefix and embedded-skill contribution to one prompt."""

    stable_prefix = prompt.split(_DYNAMIC_PROMPT_MARKER, 1)[0]
    skill_instructions = _embedded_skill_instructions(prompt, workflow_skills)
    skill_bytes = sum(len(item.encode("utf-8")) for item in skill_instructions)
    skill_tokens = sum(estimate_tokens(item) for item in skill_instructions)
    return PromptCompositionMetrics(
        prompt_bytes=len(prompt.encode("utf-8")),
        prompt_estimated_tokens=estimate_tokens(prompt),
        stable_prefix_bytes=len(stable_prefix.encode("utf-8")),
        stable_prefix_estimated_tokens=estimate_tokens(stable_prefix),
        skill_instruction_bytes=skill_bytes,
        skill_instruction_estimated_tokens=skill_tokens,
    )
