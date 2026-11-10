"""Paths and TOML settings, including the defensive validation the UI depends on."""

from __future__ import annotations

from pathlib import Path

import pytest

from inbox_sift import config
from inbox_sift.models import Settings


@pytest.fixture()
def base(tmp_path: Path) -> Path:
    base = tmp_path / "app"
    base.mkdir()
    return base


# -- paths ---------------------------------------------------------------


def test_paths_under_a_base_directory(base: Path) -> None:
    p = config.default_paths(base)
    assert p.data_dir == base / "data"
    assert p.config_dir == base / "config"
    assert p.cache_dir == base / "cache"
    assert p.db_path == base / "data" / "inbox-sift.db"
    assert p.settings_path == base / "config" / "settings.toml"
    assert p.log_path == base / "cache" / "inbox-sift.log"


def test_ensure_creates_the_directories(base: Path) -> None:
    p = config.default_paths(base)
    assert not p.data_dir.exists()
    p.ensure()
    assert p.data_dir.is_dir() and p.config_dir.is_dir() and p.cache_dir.is_dir()


def test_platform_paths_use_the_app_name() -> None:
    p = config.default_paths()
    for d in (p.data_dir, p.config_dir, p.cache_dir):
        assert str(config.APP_NAME) in str(d)


# -- load / save ---------------------------------------------------------


def test_missing_file_gives_defaults(base: Path) -> None:
    s = config.load_settings(config.default_paths(base).settings_path)
    assert s == Settings()
    assert (s.theme, s.density, s.read_on_open, s.poll) == ("system", "compact", False, "adaptive")
    assert s.layout == {"navW": 212, "drawerW": 360, "navCollapsed": False, "repoW": 170, "reasonW": 104, "oldest": False}
    assert s.hidden_hosts == []


def test_round_trip(base: Path) -> None:
    path = config.default_paths(base).settings_path
    original = Settings(theme="dark", density="balanced", read_on_open=True, poll="5m",
                        layout={"navW": 250, "drawerW": 420, "navCollapsed": True, "repoW": 170, "reasonW": 104, "oldest": False},
                        hidden_hosts=["ghe.corp"])
    config.save_settings(path, original)
    assert path.is_file()
    assert config.load_settings(path) == original


def test_save_creates_parents_and_leaves_no_temp_file(base: Path) -> None:
    path = base / "deep" / "nested" / "settings.toml"
    config.save_settings(path, Settings(theme="light"))
    assert config.load_settings(path).theme == "light"
    assert [p.name for p in path.parent.iterdir()] == ["settings.toml"]


def test_unknown_keys_are_ignored(base: Path) -> None:
    path = base / "settings.toml"
    path.write_text('theme = "dark"\nquarantine = true\n[extra]\nanswer = 42\n', encoding="utf-8")
    s = config.load_settings(path)
    assert s.theme == "dark"
    assert s.density == "compact"


DEFAULT_LAYOUT = {"navW": 212, "drawerW": 360, "navCollapsed": False, "repoW": 170, "reasonW": 104, "oldest": False}


@pytest.mark.parametrize(
    "text,field,expected",
    [
        ('theme = "neon"', "theme", "system"),
        ('density = "roomy"', "density", "compact"),
        ('poll = "hourly"', "poll", "adaptive"),
        ("theme = 7", "theme", "system"),
        ('read_on_open = "sure"', "read_on_open", False),
        ('hidden_hosts = "ghe.corp"', "hidden_hosts", []),
        ('hidden_hosts = ["ghe.corp", 7]', "hidden_hosts", ["ghe.corp", "7"]),
        ('layout = "wide"', "layout", DEFAULT_LAYOUT),
        ("[layout]\nnavW = 'lots'", "layout", DEFAULT_LAYOUT),
        ("[layout]\nnavCollapsed = 0", "layout", {**DEFAULT_LAYOUT, "navCollapsed": False}),
        ("[layout]\nnavW = 300", "layout", {**DEFAULT_LAYOUT, "navW": 300}),
    ],
)
def test_invalid_values_fall_back_to_defaults(base: Path, text: str, field: str, expected: object) -> None:
    path = base / "settings.toml"
    path.write_text(text, encoding="utf-8")
    s = config.load_settings(path)
    assert getattr(s, field) == expected


@pytest.mark.parametrize(
    "nav_w,drawer_w,expected",
    [
        (100, 100, (160, 280)),
        (999, 9999, (340, 720)),
        (212, 360, (212, 360)),
    ],
)
def test_layout_is_clamped(base: Path, nav_w: int, drawer_w: int, expected: tuple[int, int]) -> None:
    path = base / "settings.toml"
    path.write_text(f"[layout]\nnavW = {nav_w}\ndrawerW = {drawer_w}\nnavCollapsed = true\n", encoding="utf-8")
    s = config.load_settings(path)
    assert (s.layout["navW"], s.layout["drawerW"]) == expected
    assert s.layout["navCollapsed"] is True
    assert set(s.layout) == {"navW", "drawerW", "navCollapsed", "repoW", "reasonW", "oldest"}


# -- merge ---------------------------------------------------------------


def test_merge_is_shallow_but_deep_for_layout() -> None:
    s = Settings(theme="light", layout={"navW": 212, "drawerW": 360, "navCollapsed": False, "repoW": 170, "reasonW": 104, "oldest": False})
    out = config.merge_settings(s, {"theme": "dark", "layout": {"drawerW": 500}})
    assert out.theme == "dark"
    assert out.layout == {"navW": 212, "drawerW": 500, "navCollapsed": False, "repoW": 170, "reasonW": 104, "oldest": False}
    # the original object is untouched
    assert s.layout["drawerW"] == 360


def test_merge_validates_and_clamps() -> None:
    s = Settings()
    out = config.merge_settings(s, {"theme": "neon", "poll": "5m", "layout": {"navW": 4000, "navCollapsed": 1}})
    assert out.theme == "system"
    assert out.poll == "5m"
    assert out.layout == {"navW": 340, "drawerW": 360, "navCollapsed": True, "repoW": 170, "reasonW": 104, "oldest": False}


def test_merge_ignores_unknown_keys() -> None:
    s = Settings(density="balanced")
    assert config.merge_settings(s, {"nope": 1}).density == "balanced"


def test_merge_empty_patch_is_a_copy() -> None:
    s = Settings(theme="dark", hidden_hosts=["ghe.corp"])
    assert config.merge_settings(s, {}) == s


def test_layout_column_widths_and_sort_are_clamped_and_round_trip(tmp_path) -> None:
    from inbox_sift.config import load_settings, merge_settings, save_settings
    from inbox_sift.models import Settings

    s = merge_settings(Settings(), {"layout": {"repoW": 9999, "reasonW": 10, "oldest": True}})
    assert (s.layout["repoW"], s.layout["reasonW"], s.layout["oldest"]) == (360, 84, True)
    assert s.layout["navW"] == 212  # untouched keys keep their defaults
    path = tmp_path / "settings.toml"
    save_settings(path, s)
    assert load_settings(path).layout == s.layout
    assert Settings().layout["repoW"] == 170 and Settings().layout["reasonW"] == 104
