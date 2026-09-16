# ADR 0002 — Canonical Project and Task terminology

Status: Accepted

## Decision

Reserve **Phase**, **Gate**, and **Milestone** for the Project Execution domain.
The Task domain uses **Task**, **TaskExecution**, **WorkPackage**,
**WorkPackageStage (Stage)**, **Check**, **Hold**, **Verification**, **Attempt**,
and **AcceptanceCriterion**.

A ProjectGate decides whether a project boundary is satisfied. A
ProjectMilestone freezes evidence for an achieved capability. A ProjectPhase
organizes execution. None executes software. Only a Task executes software by
running Work Packages.

`ProjectState` is renamed `TaskExecutionState`. Work Package directive and UI
surfaces use Work Package rather than the historical “milestone” synonym.
Task-local review/scope/acceptance/commit boundaries are Checks or Holds, not
Project Gates.

Historical persisted records remain readable through bounded compatibility
readers. New writers emit only canonical vocabulary. Compatibility writers are
not retained.
