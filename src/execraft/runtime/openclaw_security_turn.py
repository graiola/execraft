"""Turn-scoped OpenClaw security/workspace binding.

The OpenClaw Gateway's public ``agent`` RPC does not grant third-party operator
clients an arbitrary ``cwd`` override. Execraft therefore binds managed agents to
the exact task workspace through ``agents.update`` immediately before each run.
External Gateways are never mutated: their configured workspace must already
match the Execraft-authorized root or execution fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from execraft.agents.profile import AgentProfileConfig
from execraft.orchestrate.scheduler import AgentExecutionError
from execraft.runtime_config import OpenClawMode, RuntimeConfig

from .contracts import RuntimeExecutionRequest
from .security_policy import RuntimeSecurityPolicy, openclaw_security_turn


@dataclass(frozen=True)
class OpenClawSecurityTurn:
    """Effective agent/workspace/security projection for one runtime turn."""

    request: RuntimeExecutionRequest
    agent_id: str
    policy: RuntimeSecurityPolicy
    workspace: Path
    enforcement: str

    def telemetry(self) -> dict[str, object]:
        return {
            "security_enforcement": self.enforcement,
            "workspace_bound": True,
            "workspace_root": str(self.workspace),
            "security_policy": self.policy.as_mapping(),
        }


def resolve_openclaw_security_turn(
    runtime: RuntimeConfig,
    profile: AgentProfileConfig,
    request: RuntimeExecutionRequest,
) -> OpenClawSecurityTurn:
    """Resolve the exact workspace and provider policy without mutating state."""

    options = runtime.openclaw
    if options is None:
        raise _configuration_error("OpenClaw security turn requires runtime options")
    read_only = bool(getattr(request.handoff, "read_only", False))
    policy_agent_id, policy = openclaw_security_turn(profile, read_only=read_only)
    workspace = _execution_workspace(request)
    projected_handoff = replace(
        request.handoff,
        working_directory=str(workspace),
        additional_writable_roots=[],
    )
    projected_request = replace(request, handoff=projected_handoff)

    if options.mode == OpenClawMode.MANAGED:
        agent_id = policy_agent_id
        enforcement = "managed_hard"
    else:
        agent_id = profile.id
        enforcement = "external_provider_policy"
        required = str(getattr(request.handoff, "required_isolation", "")).strip()
        if read_only and required == "hard":
            raise _configuration_error(
                "external OpenClaw cannot prove Execraft hard read-only isolation; "
                "use managed OpenClaw or a Native candidate"
            )

    return OpenClawSecurityTurn(
        request=projected_request,
        agent_id=agent_id,
        policy=policy,
        workspace=workspace,
        enforcement=enforcement,
    )


def bind_openclaw_security_turn(
    client: Any,
    runtime: RuntimeConfig,
    turn: OpenClawSecurityTurn,
) -> None:
    """Bind or verify the public OpenClaw agent workspace for this turn."""

    options = runtime.openclaw
    if options is None:
        raise _configuration_error("OpenClaw security turn requires runtime options")
    if options.mode == OpenClawMode.MANAGED:
        client.request(
            "agents.update",
            {"agentId": turn.agent_id, "workspace": str(turn.workspace)},
        )
        return

    payload = client.request("agents.list", {})
    configured = _agent_workspace(payload, turn.agent_id)
    if configured is None:
        raise _configuration_error(
            f"external OpenClaw agent {turn.agent_id!r} is not discoverable"
        )
    if configured != turn.workspace:
        raise _configuration_error(
            "external OpenClaw agent workspace does not match the Execraft task "
            f"workspace: expected {turn.workspace}, got {configured}"
        )


def _execution_workspace(request: RuntimeExecutionRequest) -> Path:
    working = _safe_workspace_path(request.handoff.working_directory, label="working directory")
    additional = tuple(
        _safe_workspace_path(value, label="additional writable root")
        for value in request.handoff.additional_writable_roots
        if str(value).strip()
    )
    external = tuple(root for root in additional if not _is_within(root, working))
    if not external:
        return working
    if len(external) == 1:
        # Execraft commonly uses a control/task worktree plus one product-repository
        # worktree. OpenClaw gets only the explicitly writable product root; the
        # durable StructuredHandoff carries the control-plane context it needs.
        return external[0]
    raise _configuration_error(
        "managed OpenClaw supports one isolated execution root per turn; "
        "multiple external writable roots require a Native candidate or a later "
        "explicit multi-root transport policy"
    )


def _safe_workspace_path(value: object, *, label: str) -> Path:
    text = str(value).strip()
    if not text:
        raise _configuration_error(f"OpenClaw {label} is empty")
    path = Path(text).expanduser().resolve()
    if not path.is_dir():
        raise _configuration_error(f"OpenClaw {label} is not a directory: {path}")
    if _sensitive_root(path):
        raise _configuration_error(f"OpenClaw refuses sensitive {label}: {path}")
    return path


def _sensitive_root(path: Path) -> bool:
    home = Path.home().expanduser().resolve()
    forbidden = {
        Path("/").resolve(),
        home,
        Path("/boot").resolve(),
        Path("/dev").resolve(),
        Path("/etc").resolve(),
        Path("/proc").resolve(),
        Path("/root").resolve(),
        Path("/run").resolve(),
        Path("/sys").resolve(),
        Path("/var/run").resolve(),
    }
    if path in forbidden:
        return True
    secret_roots = tuple(
        home / name
        for name in (
            ".aws",
            ".cargo",
            ".config",
            ".docker",
            ".gnupg",
            ".npm",
            ".ssh",
        )
    )
    if any(_is_within(path, root.resolve()) for root in secret_roots if root.exists()):
        return True
    netrc = (home / ".netrc").resolve()
    return path == netrc


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _agent_workspace(payload: object, agent_id: str) -> Path | None:
    entries: object = payload
    if isinstance(payload, Mapping):
        entries = payload.get("agents", payload.get("entries", ()))
    if isinstance(entries, Mapping):
        entries = [dict(value, id=key) if isinstance(value, Mapping) else value for key, value in entries.items()]
    if not isinstance(entries, (list, tuple)):
        return None
    for raw in entries:
        if not isinstance(raw, Mapping):
            continue
        candidate = str(raw.get("id", raw.get("agentId", ""))).strip()
        if candidate != agent_id:
            continue
        workspace = str(raw.get("workspace", "")).strip()
        if not workspace:
            return None
        return Path(workspace).expanduser().resolve()
    return None


def _configuration_error(message: str) -> AgentExecutionError:
    return AgentExecutionError(
        message,
        classification="configuration_error",
        persistent=True,
    )
