"""Filesystem locations and TOML settings.

Two jobs: pick per-platform directories (platformdirs, or a `base` directory for tests and portable installs),
and load/save `models.Settings` defensively — the settings file is user-editable, so unknown keys are ignored and
invalid values fall back to the defaults rather than crashing the app on boot.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import platformdirs
import tomli_w

from inbox_sift.models import Settings

APP_NAME = "inbox-sift"

_THEMES = ("light", "dark", "system")
_DENSITIES = ("compact", "balanced")
_POLLS = ("adaptive", "5m", "manual")
# key -> (min, max); anything outside is clamped, anything non-integer falls back to the default.
_LAYOUT_LIMITS = {"navW": (160, 340), "drawerW": (280, 720), "repoW": (80, 360), "reasonW": (84, 220)}
_LAYOUT_DEFAULTS = {"navW": 212, "drawerW": 360, "navCollapsed": False, "repoW": 170, "reasonW": 104, "oldest": False}


@dataclass
class Paths:
    data_dir: Path
    config_dir: Path
    cache_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "inbox-sift.db"

    @property
    def settings_path(self) -> Path:
        return self.config_dir / "settings.toml"

    @property
    def log_path(self) -> Path:
        return self.cache_dir / "inbox-sift.log"

    def ensure(self) -> None:
        for d in (self.data_dir, self.config_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)


def default_paths(base: Path | None = None) -> Paths:
    """`base` gives a self-contained layout (base/data, base/config, base/cache); otherwise the OS dirs."""
    if base is not None:
        base = Path(base)
        return Paths(data_dir=base / "data", config_dir=base / "config", cache_dir=base / "cache")
    return Paths(
        data_dir=Path(platformdirs.user_data_dir(APP_NAME)),
        config_dir=Path(platformdirs.user_config_dir(APP_NAME)),
        cache_dir=Path(platformdirs.user_cache_dir(APP_NAME)),
    )


def _clean_enum(value: object, allowed: tuple[str, ...], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _clean_bool(value: object, default: bool) -> bool:
    return bool(value) if isinstance(value, (bool, int)) else default


def _clean_layout(value: object) -> dict:
    """Start from the defaults, then overlay valid, clamped keys from `value`."""
    layout: dict = {**_LAYOUT_DEFAULTS}
    if not isinstance(value, dict):
        return layout
    for key, limits in _LAYOUT_LIMITS.items():
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int):
            continue
        low, high = limits
        layout[key] = max(low, min(high, raw))
    for flag in ("navCollapsed", "oldest"):
        if flag in value:
            layout[flag] = _clean_bool(value[flag], layout[flag])
    return layout


def _clean_hidden_hosts(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _validate(raw: dict, defaults: Settings) -> Settings:
    """Shallow-merge `raw` over `defaults` with per-field validation (unknown keys ignored)."""
    return Settings(
        theme=_clean_enum(raw.get("theme", defaults.theme), _THEMES, defaults.theme),  # type: ignore[arg-type]
        density=_clean_enum(raw.get("density", defaults.density), _DENSITIES, defaults.density),  # type: ignore[arg-type]
        read_on_open=_clean_bool(raw.get("read_on_open", defaults.read_on_open), defaults.read_on_open),
        poll=_clean_enum(raw.get("poll", defaults.poll), _POLLS, defaults.poll),  # type: ignore[arg-type]
        layout=_clean_layout(raw.get("layout", defaults.layout)),
        hidden_hosts=_clean_hidden_hosts(raw.get("hidden_hosts", defaults.hidden_hosts)),
    )


def load_settings(path: Path | str) -> Settings:
    """Read settings.toml; a missing or unreadable-as-TOML-file setting falls back field by field."""
    p = Path(path)
    if not p.is_file():
        return Settings()
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    return _validate(raw, Settings())


def save_settings(path: Path | str, s: Settings) -> None:
    """Write atomically (temp file + os.replace) so a crash never leaves a half-written settings.toml."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(s.to_dict(), fh)
    os.replace(tmp, p)


def merge_settings(s: Settings, patch: dict) -> Settings:
    """Apply a partial update (PUT /api/settings): shallow, except `layout`, which merges deeply."""
    merged = s.to_dict()
    for key, value in patch.items():
        if key == "layout" and isinstance(value, dict):
            merged["layout"] = _clean_layout({**_clean_layout(s.layout), **value})
        else:
            merged[key] = value
    return _validate(merged, s)
