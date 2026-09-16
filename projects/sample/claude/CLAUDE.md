@AGENTS.md

# Claude Code conventions

- Prefer the project `/ai-*` commands and subagents instead of rebuilding these workflows in chat. The commands delegate to the canonical procedures under `.agents/skills/`.
- Use `ai-architect` for exhaustive read-only exploration, `ai-reviewer` for independent review, and `ai-verifier` for executable verification.
- The main session owns updates to the task dossier after subagents return their findings.
- Do not use the Anthropic API or third-party OAuth bridges for this repository when the Claude Pro subscription and official Claude Code client are intended.

## Permission model

- `.claude/settings.json` is compiled from the selected `Execraft` policy: `plan` for read-only work and `acceptEdits` for workspace-write work.
- Destructive Git, recursive deletion, privilege escalation, and container-prune commands are denied rather than presented for approval. Run such operations manually only when genuinely required.
- Reviewer and verifier roles must remain read-only at the orchestration layer even when the main implementation session uses workspace-write permissions.
- Never bypass a denied operation through shell indirection, scripts, interpreters, aliases, or equivalent commands.
