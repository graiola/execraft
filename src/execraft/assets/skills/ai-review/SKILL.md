---
name: ai-review
description: Review a work package independently and return structured findings.
argument-hint: "[task-id]"
roles:
  - review
  - final_review
---

Review the selected task or work package without modifying product files.

1. Run `execraft task status <task-id>` and inspect the actual diff, requirement evidence, verification reports, and relevant architecture decisions.
2. Check correctness, regressions, security, failure handling, concurrency, cleanup, tests, documentation, and scope.
3. Return every blocking finding as one self-contained string with a stable ID,
   severity, repository, file/line, violated requirement, evidence, and required
   fix. Put non-blocking notes in `observations`, never in `findings`. If a
   blocking finding cannot be resolved by workspace/source changes because it
   requires host-only capabilities (for example real simulator/hardware runs,
   Docker/network access denied by the agent sandbox, credentials, or an
   external service), prefix that finding with `[EXTERNAL_ACTION_REQUIRED]`.
   Never use this marker merely because a fix is difficult; it is only for an
   independently evidenced environment boundary.
4. Return exactly one JSON object matching this canonical contract, without a
   Markdown fence or surrounding prose:

   ```json
   {
     "verdict": "approved",
     "findings": [],
     "observations": [],
     "summary": "Concise evidence-based review conclusion."
   }
   ```

   `verdict` must be exactly `approved` or `changes_required`. An approved review
   must have an empty `findings` array. A changes-required review must contain at
   least one non-empty blocking finding. Always return `observations` as an array
   of non-empty strings or an empty array. Do not emit a second model-level
   `ok`; execution success is carried by the provider transport.
5. Do not approve while critical/high findings, missing evidence, or required
   verification remain unresolved.

Remain read-only; do not edit `REVIEW.md`, commit, or fix findings during review.
