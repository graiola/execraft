# Project Milestones

A `ProjectMilestone` records that a significant capability existed at an exact
software/evidence baseline. It records achievement; it does not authorize a
later Task. Use a ProjectGate for downstream control.

Requirements may reference Tasks, ProjectGates, and earlier ProjectMilestones.
The first successful evaluation invokes `ProjectBaselineBuilder`. Required Task
and PLAN digests, terminal outcomes, verification summaries, repository
revisions, Gate evaluation revisions/fingerprints, artifact identifiers, the
Project Execution definition revision, and achievement time are captured.

If required reproducibility data is missing, achievement returns structured
issues rather than persisting a partial baseline. Once written, an achievement
is immutable: subsequent development or Project Execution edits do not silently
rewrite it.

Lifecycle is `PENDING`, `ACHIEVED`, or `CANCELLED`; schedule health is separate.
Schema v1 delivery policy is `none` or `candidate`. `candidate` freezes a
potentially deliverable baseline but performs no deployment. The downstream
provider-neutral Delivery layer may derive an immutable `DeliveryCandidate`
from that achieved baseline; provider/target configuration never becomes part
of `ProjectMilestone`. See [`project-delivery.md`](project-delivery.md).
