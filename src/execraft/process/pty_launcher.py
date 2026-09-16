"""Small exec launcher that makes stdin the controlling PTY.

`subprocess.Popen(start_new_session=True)` creates the child session safely,
without `preexec_fn` in a threaded orchestrator.  This module then claims file
descriptor 0 as the controlling terminal and execs the provider CLI in-place.
Stdout/stderr remain supervised pipes so structured provider output keeps its
existing parsing contract.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--":
        arguments.pop(0)
    if not arguments:
        print("execraft PTY launcher requires a command", file=sys.stderr)
        return 2

    if os.name == "posix":
        try:
            import fcntl
            import termios

            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            try:
                os.tcsetpgrp(0, os.getpgrp())
            except OSError:
                pass
        except (ImportError, OSError):
            # stdin is still a PTY even if the platform does not permit claiming
            # it as controlling terminal. Input remains usable in line mode.
            pass

    os.execvp(arguments[0], arguments)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
