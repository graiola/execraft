# Live agent workbench

Use **Open console** from an agent detail surface—or **Open supervisor console**
from the active Supervisor recovery details in the Action Center—to inspect an
execution while it is running and, where the
transport permits it, provide operator guidance. The workbench is designed as a
rich coding-agent client, not as a browser imitation of an ANSI terminal.

Codex and Claude Code use provider-native streaming protocols by default:

- Codex runs through `codex app-server`, the bidirectional JSON-RPC/JSONL
  interface used by rich Codex clients. Plans, messages, command execution,
  file changes, diffs, usage and turn state are translated into stable Execraft
  interaction events. Operator messages use `turn/steer`; **Interrupt** uses
  `turn/interrupt`.
- Claude Code runs as a persistent `stream-json` input/output session with
  partial messages and subagent text forwarding enabled when supported by the
  installed CLI. Operator messages are queued on the same live process and
  retain the current session context. The CLI transport does not claim a hard
  in-flight interrupt; the workbench labels this mode **queued guidance**.

OpenCode's unattended `run --format json` transport is observed as a semantic
JSONL stream, so assistant text, status, reasoning summaries explicitly emitted
by OpenCode, and tool events appear in Conversation. It is intentionally
**observation-only**: the one-shot transport does not advertise provider-native
steering and never receives a controlling PTY. This keeps the structured worker
headless and prevents TTY-sensitive startup from blocking before it contacts an
Ollama endpoint.

Standalone provider CLIs retain the Terminal/Activity interface. Structured
one-shot compatibility runs remain headless and expose Activity only. A failed
provider-native handshake falls back to that legacy adapter only before model
work begins, and the workbench shows a degraded-mode event instead of silently
pretending steering is active.

## Views

The workbench separates the normal semantic workflow from transport diagnostics.
**Conversation** and **Changes** are the only primary views; transport/session
surfaces live under **Advanced**:

- **Conversation** begins with a progress panel showing the current semantic
  activity, target, useful counters and conservative repeated-action warnings.
  It then streams assistant text, readable reasoning summaries, operator
  guidance, structured tool cards, approvals and status transitions.
- **Changes** shows the latest shared plan and working diff without scraping
  terminal text.
- **Advanced → Terminal** renders a persistent VT surface for standalone and
  compatibility sessions.
- **Advanced → Raw activity** contains bounded stdout/stderr and system diagnostics.
- **Advanced → Result** previews the durable structured result artifact produced by
  the orchestrator.

Completed provider items are authoritative snapshots. The client replaces their
accumulated deltas rather than rendering the same assistant message or command
output twice.

Tool cards are normalized before persistence. Instead of a generic `Read`,
`Write` or JSON blob, the workbench shows descriptions such as:

```text
Inspect file      src/execraft/orchestrate/orchestrator.py
Search code       ownership quarantine in src/
Run tests         python -m pytest tests/test_agent_console.py -q
Edit file         src/execraft/assets/gui/agent-console.js
Delegate work     reviewer
```

Commands, paths, search queries and subagent parent relationships remain
visible. Large command output is collapsed under **Tool output** so the live
timeline remains scannable.

## Progress and loop detection

The progress panel and human-readable heartbeat use observable provider events,
not hidden chain-of-thought. They track:

- current operation and target;
- distinct files/targets and commands;
- completed and failed tools;
- plan, diff and file-change updates;
- repeated equivalent operations.

After the same meaningful tool action recurs at least four times without a new
target, command, completed tool, plan or diff, the session is marked
**Possibly stalled**. This is deliberately advisory: Execraft does not terminate
or redirect the provider automatically because repeated reads or tests can be
legitimate. The operator can inspect the timeline and then steer, queue a
reassessment, or terminate through the appropriate transport.

## Steering a running agent

When a Codex or Claude semantic session exposes controls, the composer is
enabled immediately. Enter sends a bounded, audit-logged message; Shift+Enter
adds a line. The status badges and composer labels describe the real control
semantics:

- **Codex — live steering**: messages target the active turn through
  `turn/steer`; **Interrupt** uses `turn/interrupt`.
- **Claude — queued guidance**: messages enter the persistent stream-json input
  queue. **Queue reassess** adds an explicit stop-and-reassess request at the
  next provider input boundary; it is not presented as an immediate interrupt.
- **OpenCode — observation stream**: no composer or provider-native interrupt is
  exposed for unattended `run --format json` workers.

Operator messages are visible immediately as queued conversation entries and
are later acknowledged by provider events where the transport exposes them.
Steering can change files and invalidate assumptions in the original handoff;
the orchestrator still owns verification, review, state transitions and commit.

## Supervisor visibility

Supervisor attempts use the same adapter path as implementers, reviewers and
fixers. When recovery is active, **Recovery details → Open supervisor console**
from the Action Center therefore exposes live diagnosis,
reasoning summaries, delegated/tool activity, plan updates, diffs and the final
structured recovery result. Steering the Supervisor changes the current
incident response without bypassing its incident journal or the orchestrator's
safety checks.

## Configuration

Provider-native live sessions are enabled by default for Codex and Claude Code.
They can be disabled per provider for compatibility testing:

```yaml
providers:
  codex:
    adapter: codex
    live_sessions: true
  claude:
    adapter: claude-code
    live_sessions: true
```

Interactive operator controls remain fail-closed at project level:

```yaml
scheduling:
  interactive_console:
    enabled: true
    max_input_event_bytes: 8192
    max_pending_input_bytes: 65536
    default_rows: 40
    default_columns: 120
```

`live_sessions` controls the provider transport. `interactive_console.enabled`
allows browser controls only when the selected adapter declares a compatible
control capability. It does **not** assign a PTY globally:

- Codex consumes bounded in-turn steering and interrupt records;
- Claude consumes bounded queued-guidance records;
- OpenCode remains headless and observation-only;
- explicit standalone terminals may consume terminal input, resize, EOF, and
  signals through a PTY.

Invalid values fail configuration validation.

### Transport capabilities

Adapters declare independent transport capabilities instead of one overloaded
"interactive" flag:

| Capability | Meaning | Codex | Claude | OpenCode worker | Standalone CLI |
| --- | --- | --- | --- | --- | --- |
| Raw output stream | Bounded stdout/stderr diagnostics | yes | yes | yes | yes |
| Semantic stream | Provider events for Conversation/Changes | yes | yes | yes | no |
| In-turn steering | Change the active provider turn | yes | no | no | no |
| Queued guidance | Continue the same session with new operator input | yes | yes | no | no |
| Hard turn interrupt | Stop the current provider turn through its protocol | yes | no | no | no |
| Interactive PTY | Controlling terminal and terminal keystrokes | no | no | no | yes |
| Session resume | Continue one provider session | yes | yes | no | provider-specific |

The orchestrator configures only the callbacks supported by the adapter. A
semantic stream therefore never implies `stdin.isatty()`, and enabling the GUI
console cannot change the execution semantics of an OpenCode/Ollama worker.

## Standalone terminal workflow

Standalone sessions remain useful for exploratory work while orchestration is
idle:

1. Open an agent console and expand **Advanced**.
2. Select **New terminal** and confirm workspace access.
3. Open **Terminal** and complete any provider setup wizard.
4. Expand **Terminal controls** when direct keys are required, then focus the terminal.
5. Use the composer, arrows, Tab, Enter, Ctrl-C or Ctrl-D as needed.
6. Close and reopen the workbench without terminating the process.
7. Use **Kill terminal** under Advanced when finished.

Standalone sessions do not own a work package and never commit automatically.
Their file changes remain visible in **Changes**. The workspace action lock
prevents a standalone session and the orchestrator from mutating the task
workspace concurrently.

## Streaming architecture

```text
Codex app-server JSON-RPC       Claude stream-json       OpenCode JSONL
            |                         |                         |
            +---- provider controller-+                         |
                         |                                      |
                         +---------- event bridge ---------------+
                                        |
                                        v
                            provider-neutral events
       message / reasoning / tool / plan / diff
                         |
                         v
          semantic normalization + progress tracker
       operation / target / command / parent / warning
                         |
                         v
               interactions.jsonl
              + in-memory live state
                         |
            long-poll incremental API
                         |
                         v
       Conversation / Changes workbench views
                         ^
                         |
             bounded control.jsonl queue
                         ^
                         |
          live steering or queued guidance
```

The provider controller owns protocol IDs, turn/session state, approvals and
message translation. The generic live-session transport owns process groups,
non-blocking JSONL I/O, watchdogs, bounded captures and operator-input polling.
The orchestrator sees the existing `AgentAdapter.execute()` result contract.
This separation keeps provider protocol changes out of scheduling and state
management.

## Responsiveness

The workbench avoids terminal-style polling and full repainting for semantic
sessions:

- active conversations use bounded long polling and wake only when a session
  version changes;
- streaming token/tool deltas are coalesced in short batches before disk and DOM
  updates;
- provider JSONL stdout is parsed once and is not duplicated into Activity;
- raw stderr remains available for diagnostics and failure classification;
- Conversation and Changes are fetched incrementally with independent byte
  offsets;
- Activity is loaded only when its tab is visible;
- the session list refreshes independently;
- completed authoritative items replace accumulated deltas;
- streamed tool-input JSON is associated with its provider item ID and
  incrementally converted into command/path/query summaries;
- subagent activities retain their parent tool ID and are indented in the live
  timeline;
- hidden tabs and idle sessions use slower polling;
- in-flight browser requests are aborted when the workbench closes or switches
  session.

The terminal fallback keeps the previous optimizations: tokenized screen
snapshots, in-memory reads, throttled persistence, incremental Activity DOM,
batched keyboard input and debounced resize.

## Durable state

Session state is stored under:

```text
~/.local/state/execraft/projects/<task-id>/agent-console/<session-id>/
  metadata.json
  interactions.jsonl  # semantic provider/operator stream
  events.jsonl        # raw transport diagnostics
  terminal-screen.json
  control.jsonl       # only when interactive controls are enabled
```

`metadata.json` records the interaction mode, transport, control mode, streaming
capability and latest progress snapshot. Progress metadata is a convenience for
the live UI and heartbeat; `interactions.jsonl` remains the auditable source of
provider events.
`interactions.jsonl` is append-only and provider-neutral. `terminal-screen.json`
is an atomically replaced VT snapshot, so redraw-heavy TUIs do not expand the
semantic conversation or raw transcript.

Structured results remain under:

```text
~/.local/state/execraft/projects/<task-id>/agent-artifacts/
```

## Authorization and audit

Every control request requires the dashboard session token, explicit browser
acknowledgement, a running session and the current task's console root. Inputs
are bounded both per event and across the pending queue.

The endpoint cannot select an arbitrary PID, start a shell, switch branches,
commit, push or address another project. Provider controls are accepted only
when the adapter advertised them for that exact session, and the stored
`control_mode` prevents the UI from presenting queued guidance as a hard
interrupt.

Operator actions are recorded in:

```text
~/.local/state/execraft/projects/<task-id>/agent-console/operator-input-audit.jsonl
```

The audit stores session identity, action, byte count and SHA-256, not clear-text
message content. The visible operator message remains in the session's bounded
`interactions.jsonl` so the conversation can be understood and reviewed.

Do not paste credentials into an agent workbench. A provider may include them in
its own output, tool logs or result artifacts.

## Watchdog semantics

Provider execution uses independent deadlines so local client hangs are
classified without conflating them with useful model activity:

- `first_output_timeout_seconds` limits startup until the first captured
  stdout/stderr or valid provider protocol event;
- `output_silence_timeout_seconds` limits the gap between provider outputs;
- `inactivity_timeout_seconds` considers process compute/I/O activity as a
  secondary liveness signal;
- `timeout_seconds` remains the absolute safety ceiling.

Operator input and PTY echo never reset provider-output watchdogs. For live
Codex/Claude sessions, valid protocol progress renews the rolling session
timeout after the first event. For OpenCode, a satellite profile can use a
180-second first-output deadline so a client blocked before contacting Ollama
fails over promptly instead of occupying a slot for 20 minutes.

## Troubleshooting

- **Conversation says compatibility mode**: update the provider CLI or set
  `live_sessions: false` intentionally. Execraft falls back only before agent work
  starts.
- **Supervisor console is empty**: select the current `supervise` session and
  verify that Codex/Claude is configured with `live_sessions: true`; Activity
  should contain any handshake error.
- **OpenCode shows observation stream**: this is expected. The structured
  OpenCode worker is visible in Conversation but does not accept live steering
  or terminal keystrokes. Start an explicit standalone terminal only for manual
  CLI interaction.
- **Composer is disabled**: the session is completed, the adapter is
  observation-only, or interactive console policy is disabled.
- **Claude says queued guidance**: this is expected for the CLI stream-json
  transport. The request stays in the same provider session but may not affect
  the command currently running.
- **Possibly stalled appears**: inspect the repeated tool cards. The warning is
  heuristic and never stops the agent automatically.
- **Claude setup wizard is visible**: it is a standalone Terminal session, not a
  stream-json orchestrated attempt. Click the terminal and use arrows/Enter.
- **No plan or diff**: the provider did not emit that event type. Conversation
  and tool activity can still be live.
- **Old sessions have only Terminal/Activity**: semantic event capture is not
  retroactive.

Default retention is 16 MiB per event stream and 50 completed sessions per
agent. Reaching a capture limit does not terminate the provider; the durable
result remains authoritative.
