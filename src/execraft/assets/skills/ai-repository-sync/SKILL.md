---
name: ai-repository-sync
description: Resolve repository synchronization conflicts without owning Git state.
argument-hint: "[task-id] [package-id]"
roles:
  - implement
  - fix_review
---

Resolve only the compatibility work created by an Execraft repository-synchronization transaction.

The control plane owns all Git state. **Do not run** `git merge`, `git merge --abort`, `git rebase`, `git reset`, `git checkout`, `git switch`, `git cherry-pick`, `git commit`, `git push`, or any command that changes refs, the index, branches, or merge/rebase state. Do not stage files; Execraft validates and stages the candidate after your edit.

1. Read the bounded repository-sync transaction excerpt. Treat every recorded source SHA as immutable and authoritative for this attempt.
2. Inspect only the declared task-owned repositories and conflict/verification surface.
3. Resolve textual conflicts and the semantic API/configuration incompatibilities necessary for the synchronized histories to coexist.
4. Preserve both the accepted task contract and intended upstream behavior. Do not discard one side merely to remove conflict markers.
5. Add or adjust focused regression tests when semantic behavior changed.
6. Never edit `TASK.yaml`, `BRIEF.md`, `PLAN.md`, `PLAN.graph.yaml`, Execraft transaction/journal/state files, or unrelated repositories.
7. Report concise durable acceptance evidence. Execraft will verify there are no unmerged index entries, run configured verification, obtain independent review, and create merge commits.

If the correct resolution requires a product decision that cannot be inferred safely, stop and report the ambiguity rather than inventing policy or manipulating Git state.
