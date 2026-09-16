# OpenClaw workflow-skill bridge

Execraft remains authoritative for workflow-skill selection. Managed OpenClaw turns
may receive selected skills through an Execraft-owned runtime projection so large
skill bodies do not need to be repeated in every prompt.

## Ownership

Skills originate from the project/workspace skill catalog. Projection copies only
the selected, validated content into Execraft runtime state. It does not modify the
product repository or grant OpenClaw authority to choose additional skills.

## Managed versus external Gateway

Managed Gateways can load projected skill directories owned by Execraft. External
Gateways do not receive an inferred filesystem path; when Execraft cannot prove the
projection is available, selected skill content stays inline for correctness.

## Continuation safety

Skill version/content hashes participate in the continuation context. A changed
skill invalidates an otherwise reusable session so stale instructions are not
silently continued.

## Security

Projected skills remain data/instructions. They do not expand tool permissions,
repository write scope, or orchestration authority.

See [`openclaw-session-continuation.md`](openclaw-session-continuation.md) and
[`openclaw-security.md`](openclaw-security.md).
