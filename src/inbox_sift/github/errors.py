"""The single error type the GitHub adapter (real or fake) raises.

`kind` is what callers branch on; HTTP status is kept for messages and tests only.
"""

from __future__ import annotations

from typing import Literal

ErrorKind = Literal["auth", "forbidden", "offline", "rate_limited", "not_found", "server", "malformed"]


class GitHubError(Exception):
    def __init__(self, kind: ErrorKind, message: str, status: int | None = None,
                 retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after_s = retry_after_s

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"GitHubError(kind={self.kind!r}, status={self.status!r}, message={self.message!r})"
