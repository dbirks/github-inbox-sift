"""Avatar images, fetched once and cached on disk (design-handoff §2 Avatar).

The frontend must not talk to GitHub directly — the CSP allows ``img-src 'self'`` and a browser request would
leak which avatars the user is looking at to a third-party host. So ``/api/avatars/{host}/{login}`` is the only
image origin, and this module is the one thing that fetches from GitHub for it:

* dotcom avatars come from ``avatars.githubusercontent.com`` **without** any token; GHES avatars come from
  ``https://{host}/{login}.png`` and do carry that host's token (GHES will not serve them anonymously). The
  token is therefore only ever attached to a URL whose host is exactly the token's host.
* Results — hits and 404s alike — land in ``{cache_dir}/avatars/{host}/{login}`` so a page of 30 rows costs one
  request per person ever, not per render. A cached miss is an empty marker file, which is what keeps repeated
  scrolling from hammering GitHub for people who have no avatar.
* Everything is bounded and conservative: image content types only, under 512 KB, at most 3 https redirects,
  and the ``Authorization`` header is dropped on the first redirect so a hostile ``Location`` cannot take the
  token anywhere.

Non-image, oversize and server-error responses return ``None`` and are *not* cached, because those are the
kinds of failure that can fix themselves.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Final
from urllib.parse import urljoin

import httpx

from .client import api_base  # reused: the host rule that keeps a token on one origin must not be duplicated

# GitHub logins: alphanumerics, internal hyphens, at most 39 chars, no leading/trailing hyphen.
_LOGIN_RE: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
# GitHub App bots log in as "something[bot]"; the bracket is already outside the login grammar above, and the
# explicit check keeps the intent readable if the grammar is ever loosened.
_BOT_SUFFIX: Final = "[bot]"

_IMAGE_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_MAX_BYTES: Final = 512 * 1024  # "under 512 KB" — an exactly-512 KiB body is rejected like a larger one
_MAX_REDIRECTS: Final = 3
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
# Permanent "this person has no avatar"; anything else that fails may succeed later, so we do not cache it.
_NEGATIVE_STATUSES: Final = frozenset({404, 410})

DOTCOM_AVATAR_HOST = "avatars.githubusercontent.com"


def _check_host(host: str) -> str:
    """Validate a host with ``api_base``'s rule, then reject path traversal (dots are legal in hostnames)."""
    api_base(host)  # raises ValueError for anything that is not a bare `hostname[:port]`
    if ".." in host:
        raise ValueError(f"invalid GitHub host: {host!r}")
    return host


def _is_login(login: object) -> bool:
    """fullmatch, not match: ``$`` also matches before a trailing newline, which would reach the URL and path."""
    if not isinstance(login, str) or login.endswith(_BOT_SUFFIX):
        return False
    return _LOGIN_RE.fullmatch(login) is not None


def _media_type(raw: str | None) -> str | None:
    """The bare type from a ``Content-Type`` header, or None when it is not an image we will serve."""
    if not raw:
        return None
    base = raw.split(";", 1)[0].strip().lower()
    return base if base in _IMAGE_TYPES else None


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


class _Negative:
    """Sentinel: GitHub answered "no such avatar", which we cache and answer with nothing."""


_NEGATIVE = _Negative()


class AvatarCache:
    """Disk-backed avatar fetcher for the hosts in ``tokens`` (``{host: gh token}``).

    ``transport`` is injectable for tests (httpx.MockTransport); demo mode never builds one at all.
    """

    def __init__(
        self,
        cache_dir: Path | str,
        tokens: dict[str, str],
        transport: httpx.AsyncBaseTransport | None = None,
        ttl_s: float = 86400,
        *,
        timeout: float = 10.0,
    ) -> None:
        self.root = Path(cache_dir) / "avatars"
        self._tokens = dict(tokens)
        self._transport = transport
        self.ttl_s = float(ttl_s)
        self._timeout = timeout

    def __repr__(self) -> str:
        # Never repr the tokens: this object hangs off AppContext, whose default repr shows every field.
        return f"AvatarCache(root={str(self.root)!r}, hosts={sorted(self._tokens)!r})"

    async def get(self, host: str, login: str) -> tuple[bytes, str] | None:
        """``(content, content_type)`` for one login, or None for "no avatar to show"."""
        _check_host(host)
        if not _is_login(login):
            return None

        entry = self._load(host, login)
        if entry is _NEGATIVE:
            return None
        if entry is not None:
            return entry

        result = await self._fetch(host, login)
        if result is None:
            return None
        if result is _NEGATIVE:
            self._store(host, login, b"", "")
            return None
        self._store(host, login, result[0], result[1])
        return result

    # ------------------------------------------------------------------ endpoints

    def _endpoint(self, host: str, login: str) -> tuple[str, dict[str, str]]:
        """``(url, headers)`` — only the GHES URL is the token's own origin, so only it gets the header."""
        if host == "github.com":
            return f"https://{DOTCOM_AVATAR_HOST}/{login}?s=64", {}
        headers = {}
        token = self._tokens.get(host)
        if token:
            headers["Authorization"] = f"token {token}"
        return f"https://{host}/{login}.png?size=64", headers

    async def _fetch(self, host: str, login: str) -> tuple[bytes, str] | _Negative | None:
        url, headers = self._endpoint(host, login)
        # One client per cache miss: avatars are rare, already cached on disk, and the app has no shutdown hook
        # for a long-lived client (see run_shutdown, which only knows about the API clients).
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout, follow_redirects=False
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                try:
                    async with client.stream("GET", url, headers=headers) as resp:
                        if resp.status_code in _REDIRECT_STATUSES:
                            location = resp.headers.get("location")
                            target = urljoin(str(resp.url), location) if location else None
                            # https only, and the token never survives the redirect.
                            if target is None or not target.lower().startswith("https://"):
                                return None
                            url, headers = target, {}
                            continue
                        if resp.status_code in _NEGATIVE_STATUSES:
                            return _NEGATIVE
                        if resp.status_code != 200:
                            return None
                        media_type = _media_type(resp.headers.get("content-type"))
                        if media_type is None:
                            return None
                        declared = _declared_length(resp)
                        if declared is not None and declared >= _MAX_BYTES:
                            return None
                        body = bytearray()
                        async for chunk in resp.aiter_bytes():
                            body.extend(chunk)
                            if len(body) >= _MAX_BYTES:  # a lying Content-Length must not blow up memory
                                return None
                        return bytes(body), media_type
                except httpx.TransportError:
                    # Class name only — the exception text contains the URL, and we keep the cache silent.
                    return None
            return None  # redirect loop: give up rather than chase a cycle

    # ------------------------------------------------------------------ disk cache

    def _paths(self, host: str, login: str) -> tuple[Path, Path]:
        """(image file, content-type sidecar); both host and login are validated, so nothing escapes root."""
        blob = self.root / host / login
        return blob, blob.with_name(blob.name + ".type")

    def _load(self, host: str, login: str) -> tuple[bytes, str] | _Negative | None:
        blob, meta = self._paths(host, login)
        try:
            age = time.time() - blob.stat().st_mtime
            if age >= self.ttl_s:
                return None
            content = blob.read_bytes()
            media_type = meta.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not content:  # the empty marker written by _store for a cached 404
            return _NEGATIVE
        if media_type not in _IMAGE_TYPES or len(content) >= _MAX_BYTES:
            return None  # a corrupt entry is treated as no entry: refetch rather than serve garbage
        return content, media_type

    def _store(self, host: str, login: str, content: bytes, media_type: str) -> None:
        blob, meta = self._paths(host, login)
        try:
            blob.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(blob, content)
            _atomic_write(meta, media_type.encode("utf-8"))
        except OSError:
            return  # an unwritable cache costs us requests, not the page


def _declared_length(resp: httpx.Response) -> int | None:
    raw = resp.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
