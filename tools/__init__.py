"""Repository maintenance and architecture-check tooling.

The package marker makes imports such as ``tools.check_architecture`` resolve to
this checkout deterministically during tests, even when the host environment
has an unrelated top-level ``tools`` namespace installed.
"""
