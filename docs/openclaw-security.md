# OpenClaw runtime security

The OpenClaw boundary is enforced by configuration, process isolation, tool policy,
and fail-closed validation rather than prompt conventions.

## Authority

OpenClaw never owns Execraft orchestration state. Runtime output is evidence and can
propose work, but Execraft retains verification, review, scope, commit, retry, and
lifecycle authority.

## Managed process boundary

Managed Gateway processes run with:

- an Execraft-owned runtime HOME/state area;
- a restricted inherited environment;
- explicit authentication configuration;
- isolated execution roots;
- runtime/tool policy projected from the selected agent profile.

Sensitive or overly broad execution roots are rejected. Product repositories are
not used as OpenClaw bootstrap/config homes.

## Tool and sandbox policy

Profiles may declare sandbox/tool policy. A runtime that cannot enforce a claimed
hard restriction must fail closed rather than silently weakening it. Wildcard or
privileged access is not inferred from normal workspace-write policy.

## Credentials

Credential references remain configuration metadata. Resolved secret values are
not exposed through browser DTOs, model inventory, logs intended for operators, or
projected files unless a protected runtime API requires them.

## Public API only

Security-sensitive runtime state is resolved through documented public Gateway
methods. Execraft does not depend on private OpenClaw databases or transcript files.

## Diagnostics

Configuration-level security diagnostics distinguish projected policy from live
runtime enforcement. A configured-secure status is not treated as proof that an
external environment enforced every control.

See [`supported-runtime-architecture.md`](supported-runtime-architecture.md) and
[`openclaw-gateway.md`](openclaw-gateway.md).
