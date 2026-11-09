"""Discover GitHub hosts/accounts and tokens via the `gh` CLI.

The runner is injectable (``Runner = argv -> (returncode, stdout, stderr)``) so tests
never touch the real binary. Tokens are returned to the caller and never logged;
error messages mention the host only.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from .errors import GitHubError


class GhNotFound(Exception):
    """Raised when the `gh` binary is not installed / not on PATH."""


Runner = Callable[[list[str]], tuple[int, str, str]]


def default_runner(argv: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise GhNotFound(f"{argv[0]} not found on PATH") from None
    return proc.returncode, proc.stdout, proc.stderr


@dataclass
class HostLogin:
    host: str
    login: str | None
    ok: bool
    error: str | None
    fix_command: str | None


def _pick(entries: list[dict]) -> dict | None:
    """One entry per host: prefer the marked-active one, else the first."""
    for entry in entries:
        if entry.get("active"):
            return entry
    return entries[0] if entries else None


def discover_hosts(run: Runner = default_runner) -> list[HostLogin]:
    # `gh auth status` exits 1 when any host fails, but still prints the JSON we want.
    _code, out, err = run(["gh", "auth", "status", "--json", "hosts"])
    try:
        hosts = json.loads(out)["hosts"]
        if not isinstance(hosts, dict):
            raise TypeError("hosts is not an object")
    except (ValueError, TypeError, KeyError) as exc:
        detail = err.strip() if err.strip() else str(exc)
        raise GitHubError("auth", f"could not read gh auth status: {detail}") from None

    result: list[HostLogin] = []
    for host, entries in hosts.items():
        if not isinstance(entries, list):
            continue
        entry = _pick([e for e in entries if isinstance(e, dict)])
        if entry is None:
            continue
        ok = entry.get("state") == "success"
        result.append(
            HostLogin(
                host=host,
                login=entry.get("login"),
                ok=ok,
                error=entry.get("error"),
                fix_command=None if ok else f"gh auth login -h {host}",
            )
        )
    result.sort(key=lambda h: (h.host != "github.com", h.host))
    return result


def get_token(host: str, run: Runner = default_runner) -> str:
    code, out, _err = run(["gh", "auth", "token", "--hostname", host])
    token = out.strip()
    if code != 0 or not token:
        raise GitHubError("auth", f"No gh token for {host}")
    return token
