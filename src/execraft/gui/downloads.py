"""HTTP-neutral binary download contract for GUI route groups."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BinaryDownload:
    filename: str
    content_type: str
    content: bytes
