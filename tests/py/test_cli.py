"""The CLI: the launch banner, loopback refusal, port hunting, and `doctor` (with a fake `gh`)."""

from __future__ import annotations

import json
import shutil
import socket
import sys
from pathlib import Path

import pytest

from inbox_sift import __version__
from inbox_sift.cli import (
    display_dir,
    doctor,
    find_free_port,
    format_banner,
    is_loopback,
    main,
    server_url,
)
from inbox_sift.config import default_paths
from inbox_sift.github.gh_auth import Runner

BANNER_HOSTS = [("github.com", True, None), ("ghe.corp", False, "sign-in expired: gh auth login -h ghe.corp")]


def gh_runner(ok: tuple[str, ...] = ("github.com",), broken: tuple[str, ...] = ()) -> Runner:
    """A fake `gh`: (returncode, stdout, stderr) for `auth status` and `auth token`."""

    def run(argv: list[str]) -> tuple[int, str, str]:
        if argv[:3] == ["gh", "auth", "status"]:
            payload = {
                "hosts": {
                    **{host: [{"login": "dbirks", "active": True, "state": "success"}] for host in ok},
                    **{host: [{"login": "dbirks", "active": True, "state": "failure", "error": "bad"}]
                       for host in broken},
                }
            }
            return (1 if broken else 0), json.dumps(payload), ""
        if argv[:3] == ["gh", "auth", "token"]:
            return 0, "ghs_secret", ""
        return 1, "", f"unexpected argv: {argv}"

    return run


#: kept for readability in the doctor tests below
def noop_runner(argv: list[str]) -> tuple[int, str, str]:  # pragma: no cover - must never be called
    raise AssertionError("doctor must not run gh here")


# -- banner ------------------------------------------------------------------------------------------------


def test_format_banner_is_the_design_handoff_card() -> None:
    banner = format_banner("0.1.0", BANNER_HOSTS, 1240, "/nowhere/inbox-sift", "http://127.0.0.1:8765", demo=True)
    expected = (
        "inbox sift 0.1.0 · for GitHub · demo\n"
        "  hosts   github.com ✓  ghe.corp ×  (sign-in expired: gh auth login -h ghe.corp)\n"
        "  cache   1,240 threads · /nowhere/inbox-sift\n"
        "  open    http://127.0.0.1:8765"
    )
    assert banner == expected


def test_format_banner_real_mode_healthy_hosts() -> None:
    banner = format_banner(__version__, [("github.com", True, None)], 7, "~/.local/share/inbox-sift",
                           "http://127.0.0.1:8765", demo=False)
    assert banner.splitlines()[0] == f"inbox sift {__version__} · for GitHub"
    assert banner.splitlines()[1] == "  hosts   github.com ✓"
    assert banner.splitlines()[2] == "  cache   7 threads · ~/.local/share/inbox-sift"


def test_format_banner_without_hosts() -> None:
    assert format_banner("0.1.0", [], 0, "/tmp", "http://127.0.0.1:8765", False).splitlines()[1] == "  hosts   (none)"


def test_display_dir_uses_home() -> None:
    assert display_dir(Path.home() / ".local/share/inbox-sift") == "~/.local/share/inbox-sift"
    assert display_dir("/nowhere/inbox-sift") == "/nowhere/inbox-sift"


# -- loopback and ports -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True), ("127.1.2.3", True),
     ("0.0.0.0", False), ("192.168.1.5", False), ("example.com", False)],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected


def test_server_url_brackets_ipv6() -> None:
    assert server_url("127.0.0.1", 8765) == "http://127.0.0.1:8765"
    assert server_url("::1", 8765) == "http://[::1]:8765"


def test_non_loopback_host_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--host", "0.0.0.0", "--data-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 1
    assert "refusing to bind 0.0.0.0" in err
    assert "--allow-remote" in err
    assert "no authentication" in err


def test_find_free_port_skips_a_busy_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert find_free_port("127.0.0.1", port) == port + 1
        assert find_free_port("127.0.0.1", port + 1) == port + 1
        assert find_free_port("127.0.0.1", port, attempts=0) is None  # no candidates left


def test_main_reports_when_no_port_is_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("inbox_sift.cli.find_free_port", lambda *args, **kwargs: None)
    assert main(["--demo", "--data-dir", str(tmp_path)]) == 1
    assert "no free port" in capsys.readouterr().err


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == f"inbox-sift {__version__}\n"


# -- doctor -------------------------------------------------------------------------------------------------


def run_doctor(tmp_path: Path, **kwargs: object) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = doctor(default_paths(tmp_path), out=lines.append, **kwargs)  # type: ignore[arg-type]
    return code, lines


def test_doctor_all_green(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
    code, lines = run_doctor(tmp_path, run=gh_runner(("github.com",)))
    assert code == 0
    assert [line.split()[0] for line in lines] == ["python", "gh", "hosts", "data", "db"]
    assert lines[0] == f"python    {'.'.join(str(p) for p in sys.version_info[:3])} (ok)"
    assert "gh        /usr/bin/gh" in lines[1]
    assert "hosts     github.com ✓" in lines[2]
    assert "(writable)" in lines[3] and "(ok)" in lines[4]
    assert "ghs_secret" not in "\n".join(lines)  # never print tokens


def test_doctor_flags_an_expired_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
    code, lines = run_doctor(tmp_path, run=gh_runner(("github.com",), ("ghe.corp",)))
    assert code == 1
    assert "ghe.corp ×" in lines[2] and "(gh auth login -h ghe.corp)" in lines[2]


def test_doctor_without_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    code, lines = run_doctor(tmp_path, run=noop_runner)
    assert code == 1
    assert lines[1].startswith("gh        not found")
    assert "https://cli.github.com" in lines[1]
    assert [line.split()[0] for line in lines] == ["python", "gh", "data", "db"]


def test_doctor_skips_gh_in_demo_mode(tmp_path: Path) -> None:
    code, lines = run_doctor(tmp_path, demo=True, run=noop_runner)
    assert code == 0
    assert "gh        skipped (demo mode uses fixture data)" in lines
    assert "github.com ✓" in lines[2] and "ghe.corp ×  (demo: sign-in expired)" in lines[2]  # matches the demo UI
    _code, healthy = run_doctor(tmp_path, demo=True, demo_host_problem="none", run=noop_runner)
    assert "ghe.corp ✓" in healthy[2]


def test_doctor_reports_an_unwritable_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)  # r-x: mkdir succeeds, writing fails
    try:
        code, lines = run_doctor(blocked / "base", run=gh_runner())
    finally:
        blocked.chmod(0o700)
    assert code == 1
    assert any("not writable" in line for line in lines)
