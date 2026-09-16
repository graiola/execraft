---
name: ai-fix-review
description: Fix approved review findings and prove each resolution.
argument-hint: "[task-id]"
roles:
  - fix_review
---

Fix the unresolved findings selected in `REVIEW.md`.

1. Validate the workspace with `execraft workspace status <task-id>`.
2. Address only active findings and their necessary regression surface.
3. Add tests that reproduce each defect before or alongside the fix.
4. Run the focused and regression verification profiles with `execraft workspace verify`.
5. Update each finding with resolution evidence and append durable engineering evidence to `HANDOFF.md`; do not use it as the live orchestration status.
6. Leave the task ready for an independent final review.

Do not suppress tests, weaken acceptance criteria, or commit unless explicitly requested.
