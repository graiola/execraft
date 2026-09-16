"""Ubuntu-focused installer for supported AI coding-agent CLIs.

Codex, Claude Code and OpenCode are installed from their npm packages into a
user-owned prefix. Antigravity uses Google's official non-root installer and is
kept separate from the npm toolchain. The installer never invokes ``sudo`` and
the Execraft-owned PATH marker is only written when ``--update-path`` is requested
explicitly. Antigravity's upstream installer may independently maintain the
user's shell PATH while installing ``agy`` into ``~/.local/bin``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

InstallRunner = Callable[..., "subprocess.CompletedProcess[str]"]

_ANTIGRAVITY_INSTALL_URL = "https://antigravity.google/cli/install.sh"


@dataclass(frozen=True)
class AgentInstallSpec:
    """One supported CLI and its deterministic installation contract."""

    id: str
    display_name: str
    package: str
    binary: str
    minimum_node_major: int = 0
    auth_hint: str = ""
    install_kind: str = "npm"


AGENT_INSTALL_SPECS: tuple[AgentInstallSpec, ...] = (
    AgentInstallSpec(
        id="codex",
        display_name="OpenAI Codex CLI",
        package="@openai/codex@latest",
        binary="codex",
        minimum_node_major=20,
        auth_hint="Run 'codex' and sign in with ChatGPT or configure OPENAI_API_KEY.",
    ),
    AgentInstallSpec(
        id="claude",
        display_name="Anthropic Claude Code",
        package="@anthropic-ai/claude-code@latest",
        binary="claude",
        minimum_node_major=20,
        auth_hint="Run 'claude' and complete the browser login flow.",
    ),
    AgentInstallSpec(
        id="opencode",
        display_name="OpenCode",
        package="opencode-ai@latest",
        binary="opencode",
        minimum_node_major=20,
        auth_hint="Run 'opencode', then use /connect or 'opencode auth login'.",
    ),
    AgentInstallSpec(
        id="antigravity",
        display_name="Google Antigravity CLI",
        package=_ANTIGRAVITY_INSTALL_URL,
        binary="agy",
        install_kind="official_script",
        auth_hint="Run 'agy' and complete the individual Google-account sign-in flow.",
    ),
)

_SPEC_BY_ID = {item.id: item for item in AGENT_INSTALL_SPECS}
_NODE_VERSION = re.compile(r"^v?(?P<major>\d+)(?:\.|$)")


def _default_runner(
    args: Sequence[str], *, env: dict[str, str] | None = None
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(args),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _ubuntu_like(os_release_path: Path = Path("/etc/os-release")) -> bool:
    """Return whether the host advertises Ubuntu compatibility."""

    if not os_release_path.is_file():
        return False
    values: dict[str, str] = {}
    for raw_line in os_release_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = raw_line.partition("=")
        if not separator:
            continue
        values[key.strip()] = value.strip().strip('"')
    identities = {
        values.get("ID", "").lower(),
        *values.get("ID_LIKE", "").lower().split(),
    }
    return "ubuntu" in identities or "debian" in identities


def _node_major(version_text: str) -> int | None:
    match = _NODE_VERSION.match(version_text.strip())
    return int(match.group("major")) if match else None


def _run_version(
    binary: str,
    *,
    runner: InstallRunner = _default_runner,
    env: dict[str, str] | None = None,
) -> str:
    if shutil.which(binary, path=(env or os.environ).get("PATH")) is None:
        return ""
    try:
        completed = runner([binary, "--version"], env=env)
    except OSError:
        return ""
    text = (completed.stdout or completed.stderr or "").strip().splitlines()
    return text[0] if completed.returncode == 0 and text else ""


def select_specs(agent_ids: Iterable[str]) -> list[AgentInstallSpec]:
    """Resolve IDs, preserving declaration order and rejecting ambiguity."""

    requested = {item.strip().lower() for item in agent_ids if item.strip()}
    if "all" in requested:
        requested = set(_SPEC_BY_ID)
    unknown = sorted(requested - set(_SPEC_BY_ID))
    if unknown:
        raise ValueError(f"unknown agents: {', '.join(unknown)}")
    return [item for item in AGENT_INSTALL_SPECS if item.id in requested]


def _ensure_profile_path(profile: Path, bin_dir: Path) -> bool:
    """Idempotently add the user CLI directory to one shell profile."""

    marker = "# Added by Execraft agent installer"
    export_line = f'export PATH="{bin_dir}:$PATH"'
    existing = profile.read_text(encoding="utf-8") if profile.is_file() else ""
    if str(bin_dir) in existing:
        return False
    profile.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not existing or existing.endswith("\n") else "\n"
    profile.write_text(
        existing + separator + marker + "\n" + export_line + "\n",
        encoding="utf-8",
    )
    return True


@dataclass
class InstallResult:
    agent_id: str
    package: str
    binary: str
    installed: bool
    version: str = ""
    detail: str = ""

    def as_mapping(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "package": self.package,
            "binary": self.binary,
            "installed": self.installed,
            "version": self.version,
            "detail": self.detail,
        }


class AgentInstaller:
    """Install selected agents with non-root, provider-supported mechanisms."""

    def __init__(
        self,
        *,
        prefix: Path | None = None,
        runner: InstallRunner = _default_runner,
        env: dict[str, str] | None = None,
    ) -> None:
        self.prefix = (prefix or Path.home() / ".local").expanduser().resolve()
        self.bin_dir = self.prefix / "bin"
        self.runner = runner
        self.env = dict(env or os.environ)
        path_parts = [str(self.bin_dir)]
        if self.env.get("PATH"):
            path_parts.append(self.env["PATH"])
        self.env["PATH"] = os.pathsep.join(path_parts)

    def validate_host(self, specs: Sequence[AgentInstallSpec]) -> None:
        if platform.system().lower() != "linux" or not _ubuntu_like():
            raise RuntimeError("this installer currently supports Ubuntu/Debian Linux")

        npm_specs = [item for item in specs if item.install_kind == "npm"]
        if npm_specs:
            if shutil.which("npm", path=self.env.get("PATH")) is None:
                raise RuntimeError(
                    "npm is required for Codex, Claude Code and OpenCode. "
                    "Install Node.js 20 or newer, then rerun this script."
                )
            try:
                completed = self.runner(["node", "--version"], env=self.env)
            except OSError as exc:
                raise RuntimeError(
                    "Node.js 20 or newer is required but node could not be executed"
                ) from exc
            major = _node_major(completed.stdout or completed.stderr or "")
            minimum = max(item.minimum_node_major for item in npm_specs)
            if completed.returncode != 0 or major is None or major < minimum:
                detected = (completed.stdout or completed.stderr or "not installed").strip()
                raise RuntimeError(
                    f"Node.js {minimum}+ is required; detected {detected or 'unknown'}. "
                    "Upgrade Node.js before installing the selected agents."
                )

        if any(item.install_kind == "official_script" for item in specs):
            if self.prefix != (Path.home() / ".local").resolve():
                raise RuntimeError(
                    "Antigravity's official installer targets ~/.local; use the default "
                    "--prefix when selecting antigravity."
                )
            for binary in ("bash", "curl"):
                if shutil.which(binary, path=self.env.get("PATH")) is None:
                    raise RuntimeError(f"{binary} is required to install Antigravity")

    def status(self, specs: Sequence[AgentInstallSpec]) -> list[InstallResult]:
        results: list[InstallResult] = []
        for spec in specs:
            version = _run_version(spec.binary, runner=self.runner, env=self.env)
            results.append(
                InstallResult(
                    agent_id=spec.id,
                    package=spec.package,
                    binary=spec.binary,
                    installed=bool(version),
                    version=version,
                )
            )
        return results

    def _install_command(self, spec: AgentInstallSpec) -> list[str]:
        if spec.install_kind == "npm":
            return [
                "npm",
                "install",
                "--global",
                "--prefix",
                str(self.prefix),
                spec.package,
            ]
        if spec.install_kind == "official_script":
            # Use the upstream fast-path contract without optional arguments.
            # Some installer revisions/CDN variants reject --skip-path even
            # though it has appeared in the public documentation. The default
            # installer remains non-root and installs agy into ~/.local/bin.
            # Keeping the invocation argument-free is therefore the most
            # portable contract across Antigravity installer revisions.
            script = f"curl -fsSL {shlex.quote(spec.package)} | bash"
            return ["bash", "-lc", script]
        raise RuntimeError(f"unsupported install kind: {spec.install_kind}")

    def install(
        self,
        specs: Sequence[AgentInstallSpec],
        *,
        dry_run: bool = False,
    ) -> list[InstallResult]:
        self.validate_host(specs)
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        results: list[InstallResult] = []
        for spec in specs:
            command = self._install_command(spec)
            if dry_run:
                results.append(
                    InstallResult(
                        agent_id=spec.id,
                        package=spec.package,
                        binary=spec.binary,
                        installed=False,
                        detail=shlex.join(command),
                    )
                )
                continue
            try:
                completed = self.runner(command, env=self.env)
            except OSError as exc:
                results.append(
                    InstallResult(
                        agent_id=spec.id,
                        package=spec.package,
                        binary=spec.binary,
                        installed=False,
                        detail=str(exc),
                    )
                )
                continue
            if completed.returncode != 0:
                detail = (
                    completed.stderr
                    or completed.stdout
                    or f"{spec.display_name} installation failed"
                ).strip()
                results.append(
                    InstallResult(
                        agent_id=spec.id,
                        package=spec.package,
                        binary=spec.binary,
                        installed=False,
                        detail=detail,
                    )
                )
                continue
            version = _run_version(spec.binary, runner=self.runner, env=self.env)
            results.append(
                InstallResult(
                    agent_id=spec.id,
                    package=spec.package,
                    binary=spec.binary,
                    installed=bool(version),
                    version=version,
                    detail=(
                        "installed"
                        if version
                        else "installer completed but binary verification failed"
                    ),
                )
            )
        return results


def _interactive_selection() -> list[str]:
    print("Select AI agents to install:")
    for index, spec in enumerate(AGENT_INSTALL_SPECS, start=1):
        installed = _run_version(spec.binary)
        state = f"installed: {installed}" if installed else "not installed"
        print(f"  {index}. {spec.display_name} [{state}]")
    print("  a. All agents")
    answer = input("Selection (comma-separated): ").strip().lower()
    if answer in {"a", "all"}:
        return ["all"]
    selected: list[str] = []
    for token in answer.split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit() and 1 <= int(token) <= len(AGENT_INSTALL_SPECS):
            selected.append(AGENT_INSTALL_SPECS[int(token) - 1].id)
        else:
            selected.append(token)
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select and install supported AI coding-agent CLIs on Ubuntu"
    )
    parser.add_argument(
        "--agents",
        help="Comma-separated IDs: codex,claude,opencode,antigravity,all",
    )
    parser.add_argument("--all", action="store_true", help="Install every supported agent")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print installation commands only"
    )
    parser.add_argument(
        "--prefix",
        type=Path,
        default=Path.home() / ".local",
        help="User-owned npm prefix (default: ~/.local)",
    )
    parser.add_argument(
        "--update-path",
        action="store_true",
        help="Add PREFIX/bin to ~/.profile when missing",
    )
    parser.add_argument("--list", action="store_true", help="Show supported agents and exit")
    parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        rows = [
            {
                "id": spec.id,
                "name": spec.display_name,
                "package": spec.package,
                "install_kind": spec.install_kind,
                "binary": spec.binary,
                "installed_version": _run_version(spec.binary),
            }
            for spec in AGENT_INSTALL_SPECS
        ]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                state = row["installed_version"] or "not installed"
                print(f"{row['id']}: {row['name']} ({state})")
        return 0

    requested = ["all"] if args.all else (
        [item.strip() for item in args.agents.split(",")]
        if args.agents
        else _interactive_selection()
    )
    try:
        specs = select_specs(requested)
    except ValueError as exc:
        parser.error(str(exc))
    if not specs:
        parser.error("select at least one agent")

    if not args.yes and not args.dry_run:
        names = ", ".join(item.display_name for item in specs)
        if input(f"Install {names} into {args.prefix}? [y/N] ").strip().lower() not in {
            "y",
            "yes",
        }:
            print("Cancelled")
            return 1

    installer = AgentInstaller(prefix=args.prefix)
    try:
        results = installer.install(specs, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.update_path:
        changed = _ensure_profile_path(Path.home() / ".profile", installer.bin_dir)
        if changed and not args.json:
            print(f'Updated ~/.profile; run: export PATH="{installer.bin_dir}:$PATH"')

    if args.json:
        print(json.dumps([item.as_mapping() for item in results], indent=2))
    else:
        for result in results:
            if args.dry_run:
                print(f"{result.agent_id}: {result.detail}")
                continue
            status = result.version if result.installed else f"FAILED: {result.detail}"
            print(f"{result.agent_id}: {status}")
            if result.installed:
                print(f"  {_SPEC_BY_ID[result.agent_id].auth_hint}")
    return 0 if all(item.installed or args.dry_run for item in results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
