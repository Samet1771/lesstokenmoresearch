"""The window.

Two jobs in one place:

  models  pick the two models -- one that reads pages, one that writes the
          report -- from whatever LM Studio, Ollama or anything else on this
          machine currently has loaded.
  runs    watch a run happen, or look at an earlier one.

The same window is what opens by itself when a coding agent starts a run, so
there is one thing to learn rather than two.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from . import config as config_mod
from .config import Config
from .llm import DetectedServer, detect_servers, is_embedding_model
from .runs import PROGRESS_FILE, load_state
from .ui import render


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


class ModelsTab(Vertical):
    """Pick the two models and the SearXNG policy, then write config.toml."""

    def __init__(self, config: Config, servers: list[DetectedServer]) -> None:
        super().__init__()
        self.config = config
        self.servers = servers
        self.last_status = ""

    def compose(self) -> ComposeResult:
        yield Static(id="servers", classes="panel")

        yield Label("report model", classes="field-label")
        yield Static("plans nothing, but writes the findings — quality shows here", classes="hint")
        yield Select([], id="report-model", prompt="use whatever is loaded")

        yield Label("reading model", classes="field-label")
        yield Static(
            "reads every fetched page — throughput decides minutes or an hour; "
            "a small non-reasoning model belongs here",
            classes="hint",
        )
        yield Select([], id="fast-model", prompt="same as the report model")

        with Horizontal(id="row"):
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

        with Horizontal(id="buttons"):
            yield Button("save", id="save", variant="primary")
            yield Button("refresh servers", id="refresh")
        yield Static("", id="save-status", classes="hint")

    def on_mount(self) -> None:
        self.refresh_servers(rescan=False)

    # ---------------------------------------------------------------- data --

    def refresh_servers(self, rescan: bool = True) -> None:
        if rescan:
            self.servers = detect_servers()

        panel = self.query_one("#servers", Static)
        if not self.servers:
            panel.update(
                "[yellow]no local model server answered[/yellow]\n"
                "[dim]LM Studio: load a model, then Developer -> start the server\n"
                "Ollama: ollama serve[/dim]"
            )
        else:
            lines = []
            for server in self.servers:
                chat = [m for m in server.models if not is_embedding_model(m)]
                lines.append(
                    f"[green]●[/green] [bold]{server.label}[/bold]  [dim]{server.base_url}[/dim]  "
                    f"{len(chat)} model(s)"
                )
            panel.update("\n".join(lines))

        options = _model_options(self.servers)
        report = self.query_one("#report-model", Select)
        fast = self.query_one("#fast-model", Select)
        report.set_options(options)
        fast.set_options(options)

        chosen = _match(self.servers, self.config.model.base_url, self.config.model.name)
        if chosen:
            report.value = chosen
        fast_chosen = _match(
            self.servers,
            self.config.model.fast_base_url or self.config.model.base_url,
            self.config.model.fast_name,
        )
        if fast_chosen:
            fast.value = fast_chosen

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            self.refresh_servers(rescan=True)
            self.query_one("#save-status", Static).update("[dim]rescanned[/dim]")
        elif event.button.id == "save":
            self.save()

    def save(self) -> str:
        """Write config.toml. Returns the status line (also shown in the tab)."""
        model = self.config.model

        chosen = _decode(self.query_one("#report-model", Select).value)
        if chosen and chosen[0] < len(self.servers):
            index, name = chosen
            server = self.servers[index]
            model.name = name
            model.provider = server.provider
            model.base_url = server.base_url
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

        path = config_mod.save(self.config)
        reading = model.fast_name or "same as report"
        self.last_status = (
            f"saved {path}\nreport: {model.name or 'auto'} · reading: {reading} · {model.parallel} parallel"
        )
        self.query_one("#save-status", Static).update(
            f"[green]saved[/green] [dim]{path}[/dim]\n"
            f"report: {model.name or 'auto'} · reading: {reading}"
        )
        return self.last_status


class RunsTab(Horizontal):
    """A list of runs on the left, the live dashboard on the right."""

    tick_count = reactive(0)

    def __init__(self, runs_root: Path, initial: Path | None = None) -> None:
        super().__init__()
        self.runs_root = runs_root
        self.selected: Path | None = initial
        self._order: list[Path] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="run-list"):
            yield Label("runs", classes="field-label")
            yield ListView(id="runs")
        with VerticalScroll(id="run-detail"):
            yield Static(id="dashboard")

    def on_mount(self) -> None:
        self.reload_runs()
        self.set_interval(0.4, self.refresh_dashboard)
        self.set_interval(4.0, self.reload_runs)

    def reload_runs(self) -> None:
        if not self.runs_root.exists():
            return
        entries = sorted(
            (p for p in self.runs_root.iterdir() if p.is_dir() and (p / PROGRESS_FILE).exists()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:40]
        if entries == self._order:
            return
        self._order = entries

        listing = self.query_one("#runs", ListView)
        listing.clear()
        for entry in entries:
            state = load_state(entry / PROGRESS_FILE)
            mark = {"ok": "[green]●[/green]", "failed": "[red]●[/red]"}.get(
                state.status, "[cyan]●[/cyan]" if not state.finished else "[grey42]●[/grey42]"
            )
            listing.append(ListItem(Static(f"{mark} {state.query or entry.name}")))

        if self.selected is None and entries:
            self.selected = entries[0]

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._order):
            self.selected = self._order[index]
            self.refresh_dashboard()

    def refresh_dashboard(self) -> None:
        board = self.query_one("#dashboard", Static)
        if self.selected is None:
            board.update("[dim]no runs yet — start one with  ltms brief.md[/dim]")
            return
        self.tick_count += 1
        state = load_state(self.selected / PROGRESS_FILE)
        board.update(render(state, self.tick_count))


class LtmsApp(App):
    TITLE = "LessTokenMoreSearch"
    SUB_TITLE = "fewer tokens, more search"

    CSS = """
    Screen { background: $surface; }
    .panel { padding: 1 2; border: round $primary 30%; margin: 1 0; }
    .field-label { color: $accent; text-style: bold; margin: 1 0 0 0; }
    .hint { color: $text-muted; margin: 0 0 1 0; }
    #row { height: auto; }
    .half { width: 1fr; padding: 0 1 0 0; }
    #buttons { height: auto; margin: 1 0; }
    #buttons Button { margin: 0 2 0 0; }
    #run-list { width: 38; border-right: solid $primary 20%; padding: 0 1; }
    #run-detail { padding: 0 1; }
    ModelsTab { padding: 0 2; }
    """

    BINDINGS = [
        ("q", "quit", "quit"),
        ("r", "refresh", "refresh"),
    ]

    def __init__(self, config: Config, servers: list[DetectedServer], run: Path | None = None) -> None:
        super().__init__()
        self.config = config
        self.servers = servers
        self.initial_run = run

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="tab-runs" if self.initial_run else "tab-models"):
            with TabPane("models", id="tab-models"):
                yield ModelsTab(self.config, self.servers)
            with TabPane("runs", id="tab-runs"):
                yield RunsTab(self.config.runs_path, self.initial_run)
        yield Footer()

    def action_refresh(self) -> None:
        try:
            self.query_one(ModelsTab).refresh_servers(rescan=True)
        except Exception:  # noqa: BLE001 - the tab may not be mounted
            pass


def launch(run: Path | None = None) -> int:
    # Detection is blocking, so do it before the app paints rather than
    # freezing an already-visible window.
    config = config_mod.load()
    servers = detect_servers()
    LtmsApp(config, servers, run).run()
    return 0
