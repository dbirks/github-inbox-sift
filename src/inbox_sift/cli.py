"""`inbox-sift` entry point: launch banner, `doctor`, and the loopback server.

The CLI is deliberately boring: parse flags, build an :class:`~inbox_sift.context.AppContext`, print the
design-handoff §0 (14c) banner, then serve uvicorn on a loopback port and open the browser. It refuses to bind a
non-loopback address unless ``--allow-remote`` says so explicitly, because the server has no authentication.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import shutil
import socket
import sys
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path

import uvicorn

from . import __version__
from .api.app import create_app
from .config import Paths, default_paths
from .context import AppContext, build_demo_context, build_real_context
from .demo.fake_client import DemoScenario
from .github.errors import GitHubError
from .github.gh_auth import GhNotFound, Runner, default_runner, discover_hosts
from .storage.db import Database

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_ATTEMPTS = 20
LOOPBACK_NAMES = ("localhost", "ip6-loopback")


def _echo(line: str) -> None:
    """`inbox-sift | tee` should still show the banner, so never leave it sitting in the stdout buffer."""
    print(line, flush=True)


# -- banner -----------------------------------------------------------------------------------------------


def display_dir(path: Path | str) -> str:
    """Home-relative display, so the banner reads `~/.local/share/inbox-sift` rather than an absolute wall."""
    resolved = Path(path).expanduser()
    home = Path.home()
    try:
        return "~/" + str(resolved.relative_to(home))
    except ValueError:
        return str(resolved)


def format_banner(
    version: str,
    hosts: Sequence[tuple[str, bool, str | None]],
    threads: int,
    data_dir: Path | str,
    url: str,
    demo: bool,
) -> str:
    """The launch banner (design-handoff §0, card 14c). `hosts` is (host, signed-in, one-line fix)."""
    title = f"inbox sift {version} · for GitHub" + (" · demo" if demo else "")
    parts: list[str] = []
    fixes: list[str] = []
    for host, ok, fix in hosts:
        parts.append(f"{host} {'✓' if ok else '×'}")
        if not ok and fix:
            fixes.append(f"({fix})")
    hosts_line = "  hosts   " + ("  ".join(parts + fixes) if parts else "(none)")
    lines = [
        title,
        hosts_line,
        f"  cache   {threads:,} threads · {display_dir(data_dir)}",
        f"  open    {url}",
    ]
    return "\n".join(lines)


def _demo_rows(scenario: DemoScenario) -> list[tuple[str, bool, str | None]]:
    """Banner rows for demo mode: the GHES host's problem decides the second chip."""
    rows: list[tuple[str, bool, str | None]] = [("github.com", True, None)]
    if scenario.host_problem == "none":
        rows.append(("ghe.corp", True, None))
    elif scenario.host_problem == "offline":
        rows.append(("ghe.corp", False, "ghe.corp did not respond: check your network/VPN"))
    else:
        rows.append(("ghe.corp", False, "sign-in expired: gh auth login -h ghe.corp"))
    return rows


def _real_rows(ctx: AppContext) -> list[tuple[str, bool, str | None]]:
    rows: list[tuple[str, bool, str | None]] = []
    for entry in ctx.host_logins:
        fix = entry.fix_command or f"gh auth login -h {entry.host}"
        rows.append((entry.host, entry.ok, None if entry.ok else f"sign-in expired: {fix}"))
    return rows


# -- arguments --------------------------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--demo", action="store_true", help="run against seeded fixture data (no gh, no network)")
    parser.add_argument(
        "--demo-host-problem",
        choices=("none", "expired", "offline"),
        default="expired",
        help="what is wrong with the demo GHES host (default: expired)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default: 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT}; tries the next {PORT_ATTEMPTS})"
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument("--data-dir", metavar="PATH", help="base directory for data/config/cache (portable install)")
    parser.add_argument(
        "--allow-remote", action="store_true", help="allow a non-loopback --host (the server has no auth)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inbox-sift", description="Local, keyboard-first GitHub inbox triage.")
    _add_common(parser)
    parser.add_argument("--version", action="version", version=f"inbox-sift {__version__}")
    sub = parser.add_subparsers(dest="command")
    doctor = sub.add_parser("doctor", help="check python, gh, hosts, and the data directory")
    _add_common(doctor)
    return parser


def paths_for(args: argparse.Namespace) -> Paths:
    base = Path(args.data_dir).expanduser() if getattr(args, "data_dir", None) else None
    paths = default_paths(base)
    paths.ensure()
    return paths


def server_url(host: str, port: int) -> str:
    """Bracket a bare IPv6 literal so the URL is clickable."""
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def is_loopback(host: str) -> bool:
    name = host.strip().lower().strip("[]")
    if name in LOOPBACK_NAMES or name.startswith("127."):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def find_free_port(host: str, port: int, attempts: int = PORT_ATTEMPTS) -> int | None:
    """Bind-test candidates: the first free port at `port` or above (inclusive of `port + attempts`)."""
    name = host.strip().lower().strip("[]")
    family = socket.AF_INET6 if ":" in name else socket.AF_INET
    for candidate in range(port, port + attempts + 1):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((name, candidate))
            except OSError:
                continue
            return candidate
    return None


# -- doctor -----------------------------------------------------------------------------------------------


def doctor(
    paths: Paths,
    *,
    demo: bool = False,
    demo_host_problem: str = "expired",
    run: Runner = default_runner,
    out: Callable[[str], None] = _echo,
) -> int:
    """One line per check, exit 1 if anything failed. Never prints tokens."""
    failures = 0

    version = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info >= (3, 12)
    failures += 0 if ok else 1
    out(f"python    {version} {'(ok)' if ok else '(need 3.12+)'}")

    if demo:
        out("gh        skipped (demo mode uses fixture data)")
        problem = {"none": "✓", "expired": "×  (demo: sign-in expired)", "offline": "×  (demo: unreachable)"}
        out(f"hosts     github.com ✓  ghe.corp {problem.get(demo_host_problem, '✓')}")
    else:
        gh_path = shutil.which("gh")
        if gh_path:
            out(f"gh        {gh_path}")
        else:
            out("gh        not found — install the GitHub CLI (https://cli.github.com)")
            failures += 1
        if gh_path:
            try:
                logins = discover_hosts(run)
            except GhNotFound:
                out("hosts     gh is not runnable")
                failures += 1
            except GitHubError as exc:
                out(f"hosts     {exc.message}")
                failures += 1
            else:
                if not logins:
                    out("hosts     none — run `gh auth login`")
                    failures += 1
                else:
                    parts = [f"{entry.host} {'✓' if entry.ok else '×'}" for entry in logins]
                    fixes = [
                        f"({entry.fix_command or 'gh auth login -h ' + entry.host})"
                        for entry in logins
                        if not entry.ok
                    ]
                    out("hosts     " + "  ".join(parts + fixes))
                    failures += sum(1 for entry in logins if not entry.ok)

    data_dir = paths.data_dir
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".inbox-sift-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        out(f"data      {display_dir(data_dir)} (writable)")
    except OSError as exc:
        out(f"data      {display_dir(data_dir)} not writable ({exc.strerror or exc})")
        failures += 1

    try:
        db = Database(paths.db_path)
        db.close()
        out(f"db        {display_dir(paths.db_path)} (ok)")
    except Exception as exc:  # noqa: BLE001 - any sqlite failure is a doctor failure
        out(f"db        could not open {display_dir(paths.db_path)} ({exc})")
        failures += 1

    return 1 if failures else 0


# -- server -----------------------------------------------------------------------------------------------


def serve(ctx: AppContext, host: str, port: int, url: str, *, open_browser: bool = True) -> int:
    """Run uvicorn in this process; open the browser once the socket is actually listening."""
    config = uvicorn.Config(create_app(ctx), host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    async def amain() -> None:
        task = asyncio.create_task(server.serve())
        while not server.started and not task.done():
            await asyncio.sleep(0.05)
        if open_browser and not task.done():
            webbrowser.open(url)
        await task

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 130
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = paths_for(args)

    if args.command == "doctor":
        return doctor(paths, demo=args.demo, demo_host_problem=args.demo_host_problem)

    if not is_loopback(args.host) and not args.allow_remote:
        print(
            f"inbox sift: refusing to bind {args.host} — the server has no authentication, so a reachable port means "
            "anyone on the network can read your inbox and press Done. Pass --allow-remote if you really mean it.",
            file=sys.stderr,
        )
        return 1

    try:
        ctx = (
            build_demo_context(paths, args.demo_host_problem)
            if args.demo
            else build_real_context(paths)
        )
    except GhNotFound:
        print("inbox sift: `gh` was not found on PATH. Install the GitHub CLI, or run `inbox-sift --demo`.",
              file=sys.stderr)
        return 1
    except GitHubError as exc:
        print(f"inbox sift: {exc.message}", file=sys.stderr)
        return 1

    port = find_free_port(args.host, args.port)
    if port is None:
        print(f"inbox sift: no free port between {args.port} and {args.port + PORT_ATTEMPTS}.", file=sys.stderr)
        return 1
    url = server_url(args.host, port)
    if port != args.port:
        print(f"inbox sift: port {args.port} is busy, using {port} instead.", file=sys.stderr)

    rows = _demo_rows(ctx.demo_scenario or DemoScenario()) if ctx.demo else _real_rows(ctx)
    threads = sum(ctx.db.count_by_host().values())
    _echo(format_banner(__version__, rows, threads, paths.data_dir, url, ctx.demo))
    return serve(ctx, args.host, port, url, open_browser=not args.no_browser)


def run() -> None:  # pragma: no cover - console-script shim
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
