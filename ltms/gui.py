"""The console.

A full-screen terminal app: type what you want researched, watch it happen,
keep the history. Slash commands for everything else.

  topic or brief.md    start a run
  /models              pick the reading model and the report model
  /runs                past runs
  /status              what ltms can see on this machine
  /stop                stop the managed SearXNG container
  /clear /help /quit

The transcript is the point. A run emits events the whole time, and they land
here as they happen rather than arriving all at once at the end.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Select, Static

from . import brief as brief_mod
from . import config as config_mod
from . import docker_mgr
from .config import Config
from .llm import DetectedServer, detect_servers, is_embedding_model
from .pipeline import EFFORTS, run_sync
from .runs import PROGRESS_FILE, RunState, RunWriter, load_state, new_run_id, resolve_run
from .ui import AGENT_STATE, SPINNER, _bar, _fmt_count, _fmt_elapsed, _short_url

BANNER = r""" _   _____ __  __ ___
| | |_   _|  \/  / __|   [b]LessTokenMoreSearch[/b]
| |__ | | | |\/| \__ \   [dim]fewer tokens, more search[/dim]
|____||_| |_|  |_|___/"""

HELP = """[b]research[/b]
  [cyan]<topic>[/cyan]                 research a topic straight away
  [cyan]<brief.md>[/cyan]              research from a brief you wrote
  [cyan]--low --medium --high[/cyan]   add to either, to set effort

[b]commands[/b]
  [cyan]/models[/cyan]     choose the reading and report models
  [cyan]/runs[/cyan]       past runs
  [cyan]/watch[/cyan]      replay the last run, or /watch <id>
  [cyan]/status[/cyan]     what ltms can see here
  [cyan]/template[/cyan]   print a brief skeleton
  [cyan]/stop[/cyan]       stop the SearXNG container
  [cyan]/clear[/cyan]  [cyan]/help[/cyan]  [cyan]/quit[/cyan]
"""


def _encode(server_index: int, model: str) -> str:
    return f"{server_index}::{model}"


def _decode(value: object) -> tuple[int, str] | None:
    """Read back an encoded choice, or None for "nothing selected".

    Deliberately not compared against Select.BLANK: that sentinel has moved
    between Textual versions, while the shape of our own encoding has not.
    """
    text = str(value)
    index, separator, model = text.partition("::")
    if not separator or not index.isdigit() or not model:
        return None
    return int(index), model


def _model_options(servers: list[DetectedServer]) -> list[tuple[str, str]]:
    """Every chat model on every detected server, labelled by where it lives."""
    options: list[tuple[str, str]] = []
    for index, server in enumerate(servers):
        for model in server.models:
            if is_embedding_model(model):
                continue
            options.append((f"{model}   ({server.label})", _encode(index, model)))
    return options


def _match(servers: list[DetectedServer], base_url: str, name: str) -> str | None:
    if not name:
        return None
    wanted = (base_url or "").rstrip("/")
    for index, server in enumerate(servers):
        if wanted and server.base_url.rstrip("/") != wanted:
            continue
        if name in server.models:
            return _encode(index, name)
    return None


# ------------------------------------------------------------------ modals ---


class ModelsScreen(ModalScreen[str]):
    """Pick the two models. Dismisses with a line for the transcript."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, config: Config, servers: list[DetectedServer]) -> None:
        super().__init__()
        self.config = config
        self.servers = servers

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Label("models", classes="modal-title")
            yield Static(id="servers", classes="hint")

            yield Label("report model", classes="field-label")
            yield Static("writes the findings — quality shows here", classes="hint")
            yield Select([], id="report-model", prompt="use whatever is loaded")

            yield Label("reading model", classes="field-label")
            yield Static("reads every page — throughput decides the length of a run", classes="hint")
            yield Select([], id="fast-model", prompt="same as the report model")

            with Horizontal(classes="row"):
                with Vertical(classes="half"):
                    yield Label("parallel readers", classes="field-label")
                    yield Input(str(self.config.model.parallel), id="parallel", type="integer")
                with Vertical(classes="half"):
                    yield Label("searxng", classes="field-label")
                    yield Select(
                        [
                            ("start and stop per run", "ephemeral"),
                            ("keep it warm", "warm"),
                            ("I run my own", "external"),
                        ],
                        id="searxng-mode",
                        value=self.config.search.mode,
                        allow_blank=False,
                    )

            with Horizontal(classes="row"):
                yield Button("save", id="save", variant="primary")
                yield Button("rescan", id="rescan")
                yield Button("cancel", id="cancel")

    def on_mount(self) -> None:
        self.fill(rescan=False)

    def fill(self, rescan: bool = True) -> None:
        if rescan:
            self.servers = detect_servers()

        panel = self.query_one("#servers", Static)
        if not self.servers:
            panel.update(
                "[yellow]no local model server answered[/yellow]\n"
                "LM Studio: load a model, then Developer → start server\n"
                "Ollama: ollama serve"
            )
        else:
            panel.update(
                "\n".join(
                    f"[green]●[/green] {server.label}  [dim]{server.base_url}[/dim]  "
                    f"{len([m for m in server.models if not is_embedding_model(m)])} model(s)"
                    for server in self.servers
                )
            )

        options = _model_options(self.servers)
        for widget_id, name, base in (
            ("#report-model", self.config.model.name, self.config.model.base_url),
            (
                "#fast-model",
                self.config.model.fast_name,
                self.config.model.fast_base_url or self.config.model.base_url,
            ),
        ):
            select = self.query_one(widget_id, Select)
            select.set_options(options)
            chosen = _match(self.servers, base, name)
            if chosen:
                select.value = chosen

    def action_cancel(self) -> None:
        self.dismiss("")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rescan":
            self.fill(rescan=True)
        elif event.button.id == "cancel":
            self.dismiss("")
        elif event.button.id == "save":
            self.dismiss(self.save())

    def save(self) -> str:
        model = self.config.model

        chosen = _decode(self.query_one("#report-model", Select).value)
        if chosen and chosen[0] < len(self.servers):
            index, name = chosen
            server = self.servers[index]
            model.name, model.provider, model.base_url = name, server.provider, server.base_url
        else:
            model.name = ""

        chosen = _decode(self.query_one("#fast-model", Select).value)
        if chosen and chosen[0] < len(self.servers):
            index, name = chosen
            server = self.servers[index]
            model.fast_name = name
            # Only record an override when it actually differs, so the common
            # case keeps a config file with nothing surprising in it.
            same = server.base_url.rstrip("/") == model.base_url.rstrip("/")
            model.fast_provider = "" if same else server.provider
            model.fast_base_url = "" if same else server.base_url
        else:
            model.fast_name = model.fast_provider = model.fast_base_url = ""

        try:
            model.parallel = max(1, int(self.query_one("#parallel", Input).value or 1))
        except ValueError:
            model.parallel = 4

        mode = str(self.query_one("#searxng-mode", Select).value)
        if mode in ("ephemeral", "warm", "external"):
            self.config.search.mode = mode

        config_mod.save(self.config)
        return (
            f"saved · report [b]{model.name or 'auto'}[/b] · "
            f"reading [b]{model.fast_name or 'same'}[/b] · {model.parallel} parallel"
        )


class RunsScreen(ModalScreen[str]):
    """Past runs, newest first. Dismisses with the chosen run directory name."""

    BINDINGS = [Binding("escape", "cancel", "close")]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.entries: list[Path] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Label("runs", classes="modal-title")
            with VerticalScroll(id="run-list"):
                yield Static(id="run-table")
            yield Static("[dim]press 1-9 to replay one, esc to close[/dim]", classes="hint")
            yield Button("close", id="close")

    def on_mount(self) -> None:
        self.entries = self.find_runs()
        if not self.entries:
            self.query_one("#run-table", Static).update("[dim]no runs yet[/dim]")
            return

        table = Table.grid(padding=(0, 2))
        table.add_column(width=3, justify="right")
        table.add_column(width=8)
        table.add_column(ratio=1)
        for number, path in enumerate(self.entries, start=1):
            state = load_state(path / PROGRESS_FILE)
            tone = {"ok": "green", "failed": "red"}.get(state.status, "cyan")
            table.add_row(
                Text(str(number), style="cyan"),
                Text(state.status or "running", style=tone),
                Text((state.query or path.name)[:46] + (f"  · {state.summary}" if state.summary else "")),
            )
        self.query_one("#run-table", Static).update(table)

    def find_runs(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            (p for p in self.root.iterdir() if p.is_dir() and (p / PROGRESS_FILE).exists()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:9]

    def on_key(self, event) -> None:
        if event.key.isdigit() and event.key != "0":
            index = int(event.key) - 1
            if index < len(self.entries):
                self.dismiss(self.entries[index].name)

    def action_cancel(self) -> None:
        self.dismiss("")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("")


# ----------------------------------------------------------------- console ---


class Console(App):
    TITLE = "LessTokenMoreSearch"

    CSS = """
    Screen { background: $surface; }
    #banner { height: auto; padding: 1 2 0 2; color: $accent; }
    #transcript { padding: 0 2; background: $surface; border: none; }
    #live { height: auto; padding: 0 2; display: none; }
    #live.running { display: block; }
    #prompt-row { height: 3; padding: 0 1; }
    #command { border: tall $primary 40%; }
    #command:focus { border: tall $accent; }
    #statusbar { height: 1; padding: 0 2; background: $panel; }

    #modal { width: 80; height: auto; max-height: 90%; padding: 1 2;
             background: $panel; border: round $accent; }
    .modal-title { color: $accent; text-style: bold; }
    .field-label { color: $accent; margin: 1 0 0 0; }
    .hint { color: $text-muted; }
    .row { height: auto; margin: 1 0 0 0; }
    .row Button { margin: 0 2 0 0; }
    .half { width: 1fr; padding: 0 1 0 0; }
    #run-list { height: auto; max-height: 18; }
    ModelsScreen, RunsScreen { align: center middle; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "quit", priority=True),
        Binding("ctrl+l", "clear", "clear"),
        Binding("f1", "help", "help"),
    ]

    def __init__(self, config: Config | None = None, run: Path | None = None) -> None:
        super().__init__()
        self.config = config or config_mod.load()
        self.servers: list[DetectedServer] = []
        self.watching: Path | None = run
        self.state = RunState()
        self.offset = 0
        self.tick = 0
        self.busy = False
        self.engine = "checking…"

    # ------------------------------------------------------------- layout --

    def compose(self) -> ComposeResult:
        yield Static(BANNER, id="banner", markup=True)
        yield RichLog(id="transcript", markup=True, wrap=True, auto_scroll=True)
        yield Static(id="live")
        with Horizontal(id="prompt-row"):
            yield Input(placeholder="a topic, a brief.md, or /help", id="command")
        yield Static(id="statusbar", markup=True)

    def on_mount(self) -> None:
        self.say(HELP)
        self.query_one("#command", Input).focus()
        self.set_interval(1 / 8, self.pump)
        self.probe_environment()
        if self.watching:
            self.attach(self.watching)

    # ------------------------------------------------------------ plumbing --

    def say(self, markup) -> None:
        self.query_one("#transcript", RichLog).write(markup)

    def stamp(self, glyph: str, text: str, tone: str = "white") -> None:
        now = datetime.now().strftime("%H:%M:%S")
        self.say(f"[grey35]{now}[/grey35]  [{tone}]{glyph}[/{tone}]  {text}")

    @work(thread=True)
    def probe_environment(self) -> None:
        servers = detect_servers()
        runtime = docker_mgr.find_runtime(self.config.search.runtime)
        self.call_from_thread(
            self.environment_ready, servers, runtime.label if runtime else "no container engine"
        )

    def environment_ready(self, servers: list[DetectedServer], engine: str) -> None:
        self.servers = servers
        self.engine = engine
        if servers:
            names = ", ".join(f"{s.label} ({len(s.models)})" for s in servers)
            self.stamp("●", f"model servers: {names}", "green")
        else:
            self.stamp("!", "no local model server — start LM Studio or Ollama", "yellow")
        self.stamp("●", f"search engine: {engine}", "green" if "no " not in engine else "yellow")

    def status_line(self) -> str:
        model = self.config.model.name or "auto"
        reading = self.config.model.fast_name
        parts = [f"[cyan]{model}[/cyan]"]
        if reading and reading != model:
            parts.append(f"read [cyan]{reading}[/cyan]")
        parts.append(f"[dim]{self.engine}[/dim]")
        if self.busy:
            saved = self.state.metrics.get("tokens_saved")
            if saved:
                parts.append(f"saved [green]{_fmt_count(saved)}[/green]")
            parts.append(f"[white]{_fmt_elapsed(self.state.elapsed)}[/white]")
        parts.append("[dim]/help · ctrl+c quit[/dim]")
        return "   ".join(parts)

    # ------------------------------------------------------------ the loop --

    def pump(self) -> None:
        self.tick += 1
        if self.watching:
            self.drain()
        self.query_one("#statusbar", Static).update(self.status_line())
        if self.busy:
            self.query_one("#live", Static).update(self.live_panel())

    def drain(self) -> None:
        """Read new events and turn them into transcript lines."""
        assert self.watching is not None
        path = self.watching / PROGRESS_FILE
        if not path.exists():
            return
        with path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
        cut = chunk.rfind(b"\n")
        if cut == -1:
            return
        self.offset += cut + 1

        for line in chunk[:cut].split(b"\n"):
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            self.state.apply(event)
            self.narrate(event)

    def narrate(self, event: dict) -> None:
        """Only the lines worth keeping. Progress belongs in the live panel."""
        kind = event.get("type")
        if kind == "stage":
            status = event.get("status")
            detail = event.get("detail", "")
            if status == "ok":
                self.stamp("✓", f"[b]{event['name']}[/b]  {detail}", "green")
            elif status in ("fail", "warn"):
                tone = "red" if status == "fail" else "yellow"
                self.stamp("✗" if status == "fail" else "!", f"[b]{event['name']}[/b]  {detail}", tone)
        elif kind == "note":
            level = event.get("level", "info")
            tone = {"warn": "yellow", "error": "red"}.get(level, "grey50")
            glyph = {"warn": "!", "error": "✗"}.get(level, "·")
            self.stamp(glyph, f"[{tone}]{event.get('text','')}[/{tone}]", tone)
        elif kind == "end":
            self.busy = False
            self.query_one("#live").set_class(False, "running")
            if event.get("status") == "ok":
                self.stamp("◆", f"[b green]done[/b green]  {event.get('summary','')}", "green")
                if event.get("report"):
                    self.say(f"             [cyan]{event['report']}[/cyan]")
            else:
                self.stamp("✗", f"[b red]{event.get('status')}[/b red]  {event.get('summary','')}", "red")

    def live_panel(self) -> Table:
        frame = SPINNER[self.tick % len(SPINNER)]
        table = Table.grid(padding=(0, 1))
        table.add_column(width=2, justify="center")
        table.add_column(width=8)
        table.add_column(ratio=1)

        for name in self.state.stage_order:
            stage = self.state.stages[name]
            if stage.status != "run":
                continue
            detail = (
                Text.assemble(
                    _bar(stage.done, stage.total), ("  ", ""), (f"{stage.done}/{stage.total}", "white")
                )
                if stage.total
                else Text(stage.detail or "working", style="dim")
            )
            table.add_row(Text(frame, style="cyan"), Text(name, style="white"), detail)

        for agent in self.state.agents.values():
            if agent.state in ("done", "idle", "fail"):
                continue
            glyph, colour, label = AGENT_STATE.get(agent.state, ("·", "grey42", agent.state))
            detail = _short_url(agent.detail) if agent.detail.startswith("http") else agent.detail
            table.add_row(
                Text(""),
                Text(agent.id, style="grey70"),
                Text.assemble((f"{glyph} {label}  ", colour), (detail, "dim")),
            )
        return table

    # ----------------------------------------------------------- commands --

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        self.say(f"[dim]›[/dim] [b]{text}[/b]")
        if text.startswith("/"):
            self.command(text[1:].split())
        else:
            self.start_research(text)

    def command(self, parts: list[str]) -> None:
        name = parts[0].lower() if parts else ""
        rest = parts[1:]

        if name in ("help", "?"):
            self.say(HELP)
        elif name == "clear":
            self.action_clear()
        elif name in ("quit", "exit", "q"):
            self.exit()
        elif name == "models":
            self.push_screen(ModelsScreen(self.config, self.servers), self.models_done)
        elif name == "runs":
            self.push_screen(RunsScreen(self.config.runs_path), self.runs_done)
        elif name == "watch":
            self.replay(rest[0] if rest else None)
        elif name == "status":
            self.show_status()
        elif name == "template":
            self.say(f"[grey70]{brief_mod.TEMPLATE}[/grey70]")
        elif name == "stop":
            self.stop_searxng()
        else:
            self.stamp("!", f"unknown command: /{name} — try /help", "yellow")

    def models_done(self, result: str | None) -> None:
        if result:
            self.stamp("◆", result, "green")

    def runs_done(self, result: str | None) -> None:
        if result:
            self.replay(result)

    def replay(self, run_id: str | None) -> None:
        target = resolve_run(self.config.runs_path, run_id)
        if target is None:
            self.stamp("!", "no such run", "yellow")
            return
        self.attach(target)
        self.stamp("●", f"replaying [b]{target.name}[/b]", "cyan")

    @work(thread=True, exclusive=True)
    def stop_searxng(self) -> None:
        runtime = docker_mgr.find_runtime(self.config.search.runtime)
        stopped = bool(runtime) and docker_mgr.stop_container(runtime)
        self.call_from_thread(
            self.stamp,
            "◆" if stopped else "·",
            "stopped SearXNG" if stopped else "nothing to stop",
            "green" if stopped else "grey50",
        )

    @work(thread=True, exclusive=True)
    def show_status(self) -> None:
        lines = [
            f"config     [dim]{config_mod.config_path()}[/dim]",
            f"runs       [dim]{self.config.runs_path}[/dim]",
            f"search     {self.config.search.mode}",
        ]
        runtime = docker_mgr.find_runtime(self.config.search.runtime)
        if runtime is None:
            lines.append("engine     [yellow]none found[/yellow]")
        else:
            ready, detail = docker_mgr.runtime_ready(runtime)
            mark = "[green]ready[/green]" if ready else f"[yellow]{detail[:48]}[/yellow]"
            lines.append(f"engine     {runtime.label} {mark}")
            if ready:
                lines.append(f"container  {docker_mgr.container_state(runtime)}")
        for server in detect_servers():
            lines.append(f"models     {server.label} [dim]{server.base_url}[/dim] · {len(server.models)} loaded")
        self.call_from_thread(self.say, "\n".join(f"         {line}" for line in lines))

    # ----------------------------------------------------------- the run --

    def attach(self, run_dir: Path) -> None:
        self.watching = run_dir
        self.offset = 0
        self.state = RunState()

    def start_research(self, text: str) -> None:
        if self.busy:
            self.stamp("!", "a run is already going", "yellow")
            return

        effort = "medium"
        words = []
        for word in text.split():
            if word.lower().lstrip("-") in EFFORTS and word.startswith("--"):
                effort = word.lower().lstrip("-")
            else:
                words.append(word)
        argument = " ".join(words)

        try:
            if brief_mod.looks_like_brief(argument):
                brief = brief_mod.load(Path(argument))
                origin = f"brief [b]{Path(argument).name}[/b]"
            elif brief_mod.missing_brief(argument):
                self.stamp("✗", f"no such brief: {argument}", "red")
                return
            else:
                brief = brief_mod.from_topic(argument, EFFORTS[effort].queries)
                origin = "topic"
        except brief_mod.BriefError as error:
            self.stamp("✗", str(error).splitlines()[0], "red")
            return

        writer = RunWriter(self.config.runs_path, new_run_id(brief.topic))
        self.busy = True
        self.query_one("#live").set_class(True, "running")
        self.attach(writer.dir)
        self.stamp("●", f"{origin} · {len(brief.queries)} queries · effort [b]{effort}[/b]", "cyan")

        def body() -> None:
            try:
                run_sync(brief, effort, self.config, writer)
            except BaseException as error:  # noqa: BLE001 - always close the stream
                writer.note(str(error).splitlines()[0][:160], "error")
                writer.end("failed", summary=type(error).__name__)

        threading.Thread(target=body, daemon=True).start()

    # ------------------------------------------------------------ actions --

    def action_clear(self) -> None:
        self.query_one("#transcript", RichLog).clear()

    def action_help(self) -> None:
        self.say(HELP)


def launch(run: Path | None = None) -> int:
    Console(config_mod.load(), run).run()
    return 0
