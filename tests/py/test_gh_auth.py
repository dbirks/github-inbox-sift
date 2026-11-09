"""gh_auth.py: host discovery + token via an injected fake runner. Never calls the real gh."""

from __future__ import annotations

import json
import subprocess

import pytest

from inbox_sift.github import gh_auth
from inbox_sift.github.errors import GitHubError
from inbox_sift.github.gh_auth import GhNotFound, discover_hosts, get_token

STATUS_JSON = json.dumps({
    "hosts": {
        "ghe.corp": [
            {"active": True, "hosts": {"ghe.corp": True}, "protocol": "https",
             "apiProtocol": "https", "state": "error", "error": "token expired",
             "name": "dbirks", "login": "dbirks"},
        ],
        "github.com": [
            {"active": True, "hosts": {"github.com": True}, "protocol": "https",
             "apiProtocol": "https", "state": "success",
             "name": "dbirks", "login": "dbirks"},
        ],
        "zeta.example": [
            {"active": True, "state": "success", "login": "other"},
        ],
    }
})


def runner_for(returncode: int, stdout: str = "", stderr: str = ""):
    def run(argv: list[str]) -> tuple[int, str, str]:
        return returncode, stdout, stderr

    return run


def test_discover_hosts_success_and_error() -> None:
    hosts = discover_hosts(runner_for(0, STATUS_JSON))
    assert [h.host for h in hosts] == ["github.com", "ghe.corp", "zeta.example"]  # github.com first, then alpha
    gh, ghe = hosts[0], hosts[1]
    assert gh.ok is True and gh.login == "dbirks" and gh.error is None and gh.fix_command is None
    assert ghe.ok is False and ghe.login == "dbirks" and ghe.error == "token expired"
    assert ghe.fix_command == "gh auth login -h ghe.corp"


def test_discover_hosts_nonzero_exit_with_json_still_parses() -> None:
    # gh exits 1 when any host fails, but the JSON is still on stdout.
    hosts = discover_hosts(runner_for(1, STATUS_JSON, "gh: authentication"))
    assert [h.host for h in hosts] == ["github.com", "ghe.corp", "zeta.example"]


def test_discover_hosts_prefers_active_entry() -> None:
    payload = json.dumps({"hosts": {"github.com": [
        {"state": "success", "active": False, "login": "old"},
        {"state": "error", "active": True, "login": "current", "error": "nope"},
    ]}})
    hosts = discover_hosts(runner_for(0, payload))
    assert hosts[0].login == "current"
    assert hosts[0].ok is False


def test_discover_hosts_falls_back_to_first_when_none_active() -> None:
    payload = json.dumps({"hosts": {"github.com": [{"state": "success", "login": "solo"}]}})
    hosts = discover_hosts(runner_for(0, payload))
    assert hosts[0].login == "solo" and hosts[0].ok is True


def test_discover_hosts_empty_and_garbage() -> None:
    assert discover_hosts(runner_for(0, json.dumps({"hosts": {}}))) == []
    assert discover_hosts(runner_for(0, json.dumps({"hosts": {"x": []}}))) == []
    for bad_out in ("", "not json", json.dumps({"nope": 1}), json.dumps({"hosts": "x"})):
        with pytest.raises(GitHubError) as ei:
            discover_hosts(runner_for(1, bad_out, "gh: not logged in"))
        assert ei.value.kind == "auth"


def test_discover_hosts_invokes_expected_argv() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str, str]:
        seen.append(argv)
        return 0, json.dumps({"hosts": {}}), ""

    discover_hosts(run)
    assert seen == [["gh", "auth", "status", "--json", "hosts"]]


def test_default_runner_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(gh_auth.subprocess, "run", boom)
    with pytest.raises(GhNotFound):
        discover_hosts()


def test_default_runner_invokes_subprocess_and_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list, dict]] = []

    class FakeProc:
        returncode = 0
        stdout = " gho_secret\n"
        stderr = ""

    def fake_run(argv: list[str], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return FakeProc()

    monkeypatch.setattr(gh_auth.subprocess, "run", fake_run)
    assert get_token("ghe.corp") == "gho_secret"
    argv, kwargs = calls[0]
    assert argv == ["gh", "auth", "token", "--hostname", "ghe.corp"]
    assert kwargs == {"check": False, "capture_output": True, "text": True, "timeout": 10}


def test_get_token_success_and_failure() -> None:
    assert get_token("github.com", runner_for(0, "gho_abc\n")) == "gho_abc"

    for code, out in [(1, ""), (1, "gho_abc"), (0, ""), (0, "  \n")]:
        with pytest.raises(GitHubError) as ei:
            get_token("ghe.corp", runner_for(code, out))
        assert ei.value.kind == "auth"
        assert str(ei.value) == "No gh token for ghe.corp"
        # failure messages mention the host only — never a token fragment
        assert "gho_abc" not in str(ei.value)


def test_get_token_argv() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str, str]:
        seen.append(argv)
        return 0, "t\n", ""

    get_token("ghe.corp", run)
    assert seen == [["gh", "auth", "token", "--hostname", "ghe.corp"]]
