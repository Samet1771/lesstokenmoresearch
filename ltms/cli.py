"""Command line entry point.

    ltms brief.md         run research from a brief you wrote
    ltms "topic"          quick one-off, queries expanded from the topic
    ltms template         print a brief skeleton to fill in
    ltms gui              the window: pick models, watch runs
    ltms init             interactive setup
    ltms watch [run]      attach the dashboard to a run
    ltms runs             list recent runs
    ltms stop             stop the managed SearXNG container
    ltms status           show configuration and what is reachable

The default command prints exactly one line to stdout. That is the point of the
project: a coding agent pays ~30 tokens to learn where a full report lives, and
decides for itself how much of it to read.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from rich.console import Console

from . import brief as brief_mod
from . import config as config_mod
from . import docker_mgr, ui, window
from .pipeline import EFFORTS, run_sync
from .runs import RunWriter, load_state, new_run_id, resolve_run

SUBCOMMANDS = {"init", "gui", "watch", "runs", "stop", "status", "template", "help"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ltms",
        description="Fewer tokens. More search. Local multi-agent web research.",
    )
    parser.add_argument("topic", nargs="*", help="a brief file (.md), or a topic to research")
    parser.add_argument("-e", "--effort", choices=list(EFFORTS), default="medium")
    parser.add_argument("--read", type=int, default=None, help="override how many pages to read")
    parser.add_argument("--no-window", action="store_true", help="do not open a dashboard window")
    parser.add_argument("--quiet", action="store_true", help="no dashboard even on a terminal")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def cmd_research(args: argparse.Namespace, console: Console) -> int:
    raw = " ".join(args.topic).strip()
    if not raw:
        console.print("[red]nothing to research[/red]")
        console.print("[dim]  ltms research.md            a brief you wrote[/dim]")
        console.print("[dim]  ltms \"topic in a few words\"  quick one-off[/dim]")
        console.print("[dim]  ltms template               print a brief to fill in[/dim]")
        return 2

    config = config_mod.load()
    if not config_mod.exists():
        console.print("[dim]no config yet — using defaults. run `ltms init` to set up.[/dim]", highlight=False)

    try:
        if brief_mod.looks_like_brief(raw):
            brief = brief_mod.load(Path(raw))
        elif brief_mod.missing_brief(raw):
            print(f"failed · no such brief: {raw}", file=sys.stderr)
            print("Pass an existing .md file, or a plain topic with no path in it.", file=sys.stderr)
            return 2
        else:
            brief = brief_mod.from_topic(raw, EFFORTS[args.effort].queries)
    except brief_mod.BriefError as error:
        print(f"failed · {error}", file=sys.stderr)
        return 2

    writer = RunWriter(config.runs_path, new_run_id(brief.topic))
    result: dict = {}
    failure: BaseException | None = None

    def work() -> None:
        nonlocal result, failure
        try:
            result = run_sync(brief, args.effort, config, writer, args.read)
        except BaseException as error:  # noqa: BLE001 - always close the event stream
            failure = error
            writer.note(str(error).splitlines()[0][:120], "error")
            writer.end("failed", summary=type(error).__name__)

    interactive = console.is_terminal and not args.quiet

    if interactive:
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        ui.watch(writer.dir, console=console, hold=False)
        worker.join(timeout=10)
    else:
        if config.ui.open_window and not args.no_window:
            window.open_dashboard(writer.dir)
        work()

    if failure is not None:
        message = str(failure).strip()
        if args.json:
            print(json.dumps({"status": "failed", "error": message, "run": writer.run_id}))
        else:
            print(f"failed · {message.splitlines()[0]}", file=sys.stderr)
            if len(message.splitlines()) > 1:
                print("\n".join(message.splitlines()[1:]), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result))
    else:
        print(
            f"done · {result.get('pages_read', 0)} sources read"
            f" · {result.get('facts', 0)} facts"
            f" · {result.get('report_tokens', 0)} tokens"
            f" · {result.get('report', '')}"
        )
    return 0


def cmd_gui(args: list[str], console: Console) -> int:
    from .gui import launch  # imported lazily: textual is slow to load

    config = config_mod.load()
    target = None
    if args:
        candidate = Path(args[0])
        target = candidate if candidate.exists() else resolve_run(config.runs_path, args[0])
    return launch(target)


def cmd_template(console: Console) -> int:
    # Printed raw so an agent can redirect it straight into a file.
    sys.stdout.write(brief_mod.TEMPLATE)
    return 0


def cmd_watch(args: list[str], console: Console) -> int:
    config = config_mod.load()
    target = args[0] if args else None
    if target and Path(target).exists():
        run_dir = Path(target)
    else:
        run_dir = resolve_run(config.runs_path, target)
    if run_dir is None:
        console.print("[yellow]no runs found[/yellow]")
        return 1
    ui.watch(run_dir, console=console, hold=console.is_terminal)
    return 0


def cmd_runs(console: Console) -> int:
    config = config_mod.load()
    root = config.runs_path
    if not root.exists():
        console.print("[dim]no runs yet[/dim]")
        return 0
    entries = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    if not entries:
        console.print("[dim]no runs yet[/dim]")
        return 0
    for entry in entries[:15]:
        state = load_state(entry / "progress.jsonl")
        status = state.status or ("running" if not state.finished else "?")
        tone = {"ok": "green", "failed": "red", "running": "cyan"}.get(status, "grey42")
        console.print(
            f"  [{tone}]{status:<8}[/{tone}] [bold]{entry.name}[/bold]  [dim]{state.summary or state.query}[/dim]"
        )
    return 0


def cmd_stop(console: Console) -> int:
    config = config_mod.load()
    runtime = docker_mgr.find_runtime(config.search.runtime)
    if runtime is None:
        console.print("[yellow]no container runtime found[/yellow]")
        return 1
    if docker_mgr.stop_container(runtime):
        console.print("[green]stopped SearXNG[/green]")
    else:
        console.print("[dim]nothing to stop[/dim]")
    return 0


def cmd_status(console: Console) -> int:
    config = config_mod.load()
    console.print()
    console.print(f"  config     [dim]{config_mod.config_path()}[/dim]"
                  + ("" if config_mod.exists() else "  [yellow](not created — run `ltms init`)[/yellow]"))
    console.print(f"  runs       [dim]{config.runs_path}[/dim]")
    console.print()
    console.print(f"  search     mode [bold]{config.search.mode}[/bold]"
                  + (f"  url {config.search.url}" if config.search.url else ""))

    runtime = docker_mgr.find_runtime(config.search.runtime)
    if runtime is None:
        console.print("             [yellow]no container engine found[/yellow]")
    else:
        ready, detail = docker_mgr.runtime_ready(runtime)
        mark = "[green]ready[/green]" if ready else f"[yellow]not running[/yellow] [dim]{detail[:60]}[/dim]"
        console.print(f"             {runtime.label} {mark}")
        if ready:
            state = docker_mgr.container_state(runtime)
            url = docker_mgr.container_url(runtime) if state == "running" else None
            console.print(f"             container [bold]{state}[/bold]" + (f"  {url}" if url else ""))
            console.print(f"             image {'[green]present[/green]' if docker_mgr.has_image(runtime) else '[yellow]not pulled[/yellow]'}")

    from .llm import detect_servers  # local import keeps `ltms --help` fast

    console.print()
    name = config.model.name or "[dim](whatever is loaded)[/dim]"
    console.print(f"  model      {name}  [dim]{config.model.base_url}[/dim]")

    servers = detect_servers()
    configured = next((s for s in servers if s.base_url.rstrip("/") == config.model.base_url.rstrip("/")), None)
    if configured is None:
        console.print("             [yellow]not reachable[/yellow]")
        if servers:
            others = ", ".join(f"{s.label} at {s.base_url}" for s in servers)
            console.print(f"             [dim]but found: {others} — run `ltms init`[/dim]")
        else:
            console.print("             [dim]start LM Studio's local server, or Ollama[/dim]")
    elif not configured.models:
        console.print(f"             [yellow]{configured.label} is up but has no model loaded[/yellow]")
    else:
        console.print(f"             [green]{configured.label} ready[/green]  [dim]{len(configured.models)} model(s)[/dim]")
    console.print()
    return 0


def _force_utf8() -> None:
    """Windows pipes default to cp1252, which mangles the dashboard glyphs."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _force_utf8()
    console = Console(stderr=False)

    if argv and argv[0] in SUBCOMMANDS:
        command, rest = argv[0], argv[1:]
        if command == "help":
            build_parser().print_help()
            return 0
        if command == "init":
            from .init_wizard import run_wizard

            return run_wizard(console)
        if command == "watch":
            return cmd_watch(rest, console)
        if command == "runs":
            return cmd_runs(console)
        if command == "stop":
            return cmd_stop(console)
        if command == "status":
            return cmd_status(console)
        if command == "template":
            return cmd_template(console)
        if command == "gui":
            return cmd_gui(rest, console)

    args = build_parser().parse_args(argv)
    return cmd_research(args, console)


if __name__ == "__main__":
    raise SystemExit(main())
