"""HTTP transport helpers shared by the local dashboard server."""

from __future__ import annotations

import errno


_CLIENT_DISCONNECT_ERRNOS = {errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED}


def is_client_disconnect(exc: BaseException) -> bool:
    """Return whether an HTTP write failed because the browser closed the socket.

    Dashboard polling requests are intentionally replaceable: a browser refresh,
    tab close, or newer fetch may abort an older response while the server is still
    serializing a large snapshot. Those disconnects are normal transport events,
    not application failures, and must never trigger a second response attempt.
    """

    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return isinstance(exc, OSError) and exc.errno in _CLIENT_DISCONNECT_ERRNOS
