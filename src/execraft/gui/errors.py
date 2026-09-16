"""GUI-specific domain and transport errors."""


class GuiError(RuntimeError):
    """Raised when a dashboard request is invalid or unsafe."""
