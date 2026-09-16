"""Configuration errors shared by legacy and normalized agent schemas."""


class AgentConfigError(ValueError):
    """Raised when ``agents.yaml`` is ambiguous or unsafe."""
