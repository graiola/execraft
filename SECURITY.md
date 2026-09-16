# Security policy

## Supported versions

Security fixes are applied to the latest published Execraft release and the current
`main` branch. Older versions may require upgrading before a fix can be applied.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for a suspected vulnerability.

Send a private report to:

**Gennaro Raiola — [gennaro.raiola@gmail.com](mailto:gennaro.raiola@gmail.com)**

Include, when possible:

- affected version or commit;
- operating system and Python version;
- a concise description of the impact;
- minimal reproduction steps or a proof of concept;
- whether credentials, filesystem boundaries, Git state, or remote execution are involved;
- any suggested mitigation.

Do not include unrelated private source code, credentials, tokens, or personal data.

You should receive an acknowledgement as soon as practical. Please allow time to confirm
the issue and prepare a coordinated fix before public disclosure.

## Security boundaries

Execraft intentionally treats execution agents and model output as untrusted inputs to a
deterministic control plane. Important boundaries are documented in:

- [`docs/architectural-invariants.md`](docs/architectural-invariants.md)
- [`docs/scope-checks.md`](docs/scope-checks.md)
- [`docs/workspace-lifecycle-safety.md`](docs/workspace-lifecycle-safety.md)
- [`docs/openclaw-security.md`](docs/openclaw-security.md)
