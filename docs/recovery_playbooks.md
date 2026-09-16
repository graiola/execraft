# Deterministic recovery playbooks

`Execraft` uses model-driven supervision only when an incident actually requires
open-ended diagnosis. Some terminal states already contain a complete repair
specification. Sending those states back through a broad Supervisor prompt wastes
provider quota, can fail the Supervisor's structured-output contract, and may
hide the original actionable findings behind cooldown and retry state.

Recovery playbooks are bounded orchestrator-owned state transformations for
those known cases. They do not edit source code, accept review findings, or
bypass any Check or Hold. They convert a precise terminal incident into the next precise
agent task, then return the package through verification and the configured review checks,
acceptance evidence, and commit.

## Exhausted review/fix cycles

When final review rejects a package after the ordinary review/fix budget has
been consumed, the persisted review findings are already the repair plan. With
the playbook enabled, `Execraft` therefore:

1. assigns stable IDs such as `RF-001` to the exact persisted findings;
2. closes any broad Supervisor incident created for the same escalation;
3. queues one bounded `fix_review` campaign;
4. prefers a fixer other than the configured Supervisor;
5. requires the fixer to acknowledge every finding ID and return a corrected
   implementation summary and criterion-specific evidence;
6. reruns regression verification and the final-review check;
7. lets the normal orchestrator-owned commit transaction finish the package.

A fixer cannot silently omit a finding. Missing or unknown IDs fail the strict
result contract. The playbook also has its own rescue-cycle budget, so a package
that remains rejected eventually returns to a real operator decision rather than
looping forever.

## Configuration

Projects opt in explicitly under `scheduling`:

```yaml
scheduling:
  recovery_playbooks:
    enabled: true
    review_exhausted:
      enabled: true
      max_rescue_cycles: 2
      max_findings: 32
      prefer_non_supervisor_fixer: true
      allow_supervisor_fallback: false
```

`prefer_non_supervisor_fixer` keeps the provider that failed the Supervisor
contract out of the direct repair campaign. With `allow_supervisor_fallback:
false`, `Execraft` waits for another eligible fixer instead of consuming more
Supervisor quota. The project scaffold enables this policy; library callers that
construct orchestration configuration directly remain fail-closed until they
select it.

## Operator visibility

The CLI exposes the playbook through the existing Supervisor status command:

```bash
execraft orchestrate supervisor --project <project> --task-id <task>
```

For an eligible incident it reports that deterministic recovery is available,
the finding count and rescue cycle, and that the configured Supervisor is
excluded. `execraft orchestrate run` then resumes without a manual transition or a
stale human answer.

The dashboard shows **Repair review findings directly** and explains that the
known findings will be sent to a bounded fixer. Progress logs include the
playbook cycle, finding count, excluded Supervisor, fixer completion, and return
to regression verification.

## Safety boundary

A playbook is not an alternate acceptance path. It cannot:

- mark a finding resolved without a successful fixer result;
- skip verification or the configured final-review check;
- approve its own changes;
- create commits, push, switch branches, or rewrite history;
- handle ambiguous product decisions;
- exceed configured finding or rescue-cycle limits.

Incidents outside a registered deterministic pattern continue to use the
project Supervisor. If neither mechanism can proceed safely, `Execraft` asks the
operator a concrete question.

## Package-scoped verification baselines

A failed test command is not itself a safe baseline. When an operator explicitly
accepts a demonstrated pre-existing failure set, `Execraft` now persists a
package-scoped authorization containing the repository, exact command, individual
test identifiers, and normalized failure signatures. The command's raw result
continues to be recorded as failed; only the package-level blocking decision can
become `passed_with_accepted_baseline`.

The authorization is deliberately narrow:

- a new failing test blocks;
- the same test with a changed normalized signature blocks;
- a changed command or repository blocks;
- a failed command whose individual test failures cannot be parsed blocks;
- the authorization is valid only for the package/profile that received the
  operator decision and expires as an authorization when the package completes.

Volatile diagnostics such as generated long numeric topic tokens and memory
addresses are normalized before hashing so the same defect can match across
runs without turning the whole command into a wildcard exemption.

`passed` and `passed_with_accepted_baseline` are both successful *effective*
verification outcomes. Raw command statuses remain untouched for audit and are
never re-labelled as passing. Every downstream Work Package check (repository-sync
commit eligibility, acceptance-evidence reconstruction, finalization, and
restart recovery) must use the effective outcome contract rather than comparing
a status string to literal `passed`. Acceptance evidence records when a baseline
was used and retains the failed raw command entries.

For restart compatibility, Execraft recognizes the exact historical
repository-sync escalation produced by older literal-`passed` commit readiness checks. If
the package is still `ready_to_commit`, effective verification is accepted,
final review is approved, and the sync transaction is already verified, the
stale escalation and any Supervisor incident opened from it are retired
deterministically before agent scheduling. Other commit failures remain
blocking and are never auto-reconciled.

Operator acceptance is terminal for that decision: the orchestrator records the
fingerprints immediately and retires the active Supervisor incident instead of
asking a model to reinterpret the same choice. On restart, older journals that
contain an explicit baseline choice can be migrated automatically only when the
failure fingerprint immediately preceding that choice still exactly matches the
current failed verification. This keeps existing tasks recoverable without
manual `state.json` edits while failing closed on drift.

## External acceptance boundaries

A blocking final-review finding is not always source-repairable. Real simulator
or hardware acceptance, host networking/container access denied to the agent
sandbox, credentials, and external services may require an operator-owned run.
Reviewers mark a purely external blocker with `[EXTERNAL_ACTION_REQUIRED]`.
Older review artifacts are recognized only through conservative environment
phrases; a finding that also contains an explicit source/test repair remains in
the normal fix cycle until that repair is complete.

When every remaining blocking finding is external, `Execraft` does not consume
another review/fix or deterministic-rescue cycle. It keeps the package at the
current review stage, records the exact finding as durable evidence, disables
model-driven Supervisor recovery for that incident, and enters `human_required`.
After the required host/hardware/simulator action has produced durable evidence,
the operator resumes normally and independent review evaluates the evidence.
The Check is never converted into approval merely because the agent sandbox lacks
the required capability.

GUI-owned `orchestrate run` processes may exit non-zero when they reach this or
another durable operator Hold. The dashboard treats that as expected control
flow and renders **Action required** with the recorded action instead of
mislabeling it as a driver crash. Genuine process failures continue to use the
non-zero driver-error presentation.

## Explicit operator acceptance of a late human-decision hold

When an operator deliberately chooses to move on from a late review or
acceptance blocker, `Execraft` provides a package-scoped risk disposition rather
than weakening strict checks globally. The action is available only while the
project is `human_required` and the persisted package is at `final_review`,
`full_verify`, or `ready_to_commit`. Repository-scope, provider, invalid-output,
repository-sync, and commit-transaction failures are not eligible.

The dashboard exposes this as **Accept & continue** beside the normal repair or
Supervisor action. The confirmation dialog shows the exact blocking requirement,
reviewer evidence, and acceptance criteria that are still incomplete. It requires
both an explicit acknowledgement and an operator rationale. The preview carries
the journal sequence of the active `human_intervention_required` event; if that
event changes before confirmation, the mutation is rejected and the operator
must refresh the decision.

Acceptance is auditable and does not rewrite history. The package stores an
`operator_risk_acceptance` record with a unique decision ID, operator rationale,
source stage, escalation sequence, review findings, artifact reference, and the
criterion IDs covered by the decision. Covered acceptance criteria remain
`verified: false`; when they have no evidence text, Execraft records that the
criterion was operator-deferred and remains unverified. The journal records
`operator_risk_accepted` and any stale Supervisor incident for the same package
is retired.

After acceptance the package advances to `ready_to_commit`, the project returns
to `running`, and the GUI starts the normal orchestrator driver automatically.
The ready-to-commit evidence Check recognizes only the exact criterion IDs in the
package's active acceptance record, allowing finalization without pretending the
external or deferred check passed. The equivalent terminal command is:

```bash
execraft orchestrate accept-risk \
  --project sample \
  --task-id feature_auth \
  --package-id recovery \
  --expected-action-sequence 123 \
  --accept-reason "Real simulator validation is deferred to the host campaign." \
  --acknowledge-unverified
```
