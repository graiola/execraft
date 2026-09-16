# Compatibility ledger

This document lists compatibility behavior intentionally retained by the current
release. New code should use the canonical APIs even when older forms remain
readable.

## Configuration schemas

- Schema v4 is canonical for runtime/profile/model-route/target configuration.
- Schema v1-v3 remain readable and migratable during the compatibility window.
- Legacy provider-only configuration is normalized into the canonical execution
  model rather than becoming a second source of truth.
- The legacy `opencode/providers.yaml` endpoint format remains readable.

## Agent/profile terminology

- Scheduler identity is an **agent profile/candidate**.
- Historical `provider_id`, `--provider`, and provider-named health/state fields
  remain readable where documented.
- Model-provider names such as OpenAI, Anthropic, and Ollama remain correctly
  described as providers.
- `AgentAdapterCapabilities` remains a compatibility alias for the canonical
  `RuntimeCapabilities` type.

## Invocation and runtime records

Older invocation/runtime-session records are migrated or read conservatively.
Missing newer dimensions do not authorize stronger execution. Opaque runtime
session references remain disposable optimization state.

## Native runtime

Native adapter command behavior and live-session compatibility remain stable
behind the normalized runtime facade. OS process/PTY supervision is owned by
`execraft.process` even when callers use higher-level orchestration APIs.

## OpenClaw Gateway

- The validated OpenClaw release is **2026.7.1-2** with Gateway protocol **4**.
- Managed and external Gateway modes remain distinct ownership models.
- Explicit external configuration is never overwritten by Execraft.
- Credential references remain references; secret values are not projected into
  browser/read-model payloads.
- Public Gateway APIs are the compatibility boundary; private databases and
  transcripts are not authoritative inputs.

## Model projection and targets

- Canonical model routes and execution targets own provider/model/endpoint and
  placement data.
- Legacy provider aliases may still resolve through the compatibility registry.
- Supported target kinds are `local` and `inference_endpoint`.
- `remote_runtime` configuration remains readable but is experimental-disabled in
  normal execution.

## OpenClaw continuation and optimization

- Cold reconstruction remains the fallback when exact session compatibility
  cannot be proven.
- Session bindings include the context needed to invalidate stale reuse.
- Managed workflow-skill projection is optional; external Gateways fall back to
  inline skill content when projection availability cannot be proven.
- Context compaction/reuse is optimization only and must pass authoritative guards.

## OpenClaw security

- Managed children use isolated Execraft-owned runtime state and a restricted
  environment.
- Hard sandbox/tool claims fail closed when enforcement cannot be provided.
- Configuration-level security diagnostics do not fabricate live enforcement
  evidence.

## Experimental-disabled definitions

OpenClaw specialist/sub-agent delegation and full remote-runtime placement remain
readable for compatibility but are not supported normal execution paths. Release
code must fail closed rather than activating them implicitly.

## Runtime extensions

Third-party runtime kinds remain supported through the runtime registry seam. A
runtime extension must use canonical execution requests/results and cannot create
parallel scheduler/configuration authority.
