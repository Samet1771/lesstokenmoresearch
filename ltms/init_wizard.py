"""`ltms init` -- the setup conversation.

Runs once. Detects what is on the machine, asks the few things that cannot be
guessed, and writes config.toml. Everything it asks has a working default, so
pressing Enter through the whole thing produces a valid setup.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

from . import docker_mgr
from .config import Config, ModelConfig, SearchConfig, UiConfig, config_path, load, save
from .llm import detect_servers, is_embedding_model

BANNER = r"""
 _   _____ __  __ ___
| | |_   _|  \/  / __|   LessTokenMoreSearch
| |__ | | | |\/| \__ \   fewer tokens, more search
|____||_| |_|  |_|___/
"""


def _ok(console: Console, text: str) -> None:
    console.print(Text.assemble(("  ✓ ", "green"), (text, "white")))


def _warn(console: Console, text: str) -> None:
    console.print(Text.assemble(("  ! ", "yellow"), (text, "yellow")))


def _info(console: Console, text: str) -> None:
    console.print(Text.assemble(("    ", ""), (text, "dim")))


def run_wizard(console: Console | None = None) -> int:
    console = console or Console()

    if not console.is_terminal:
        console.print(
            "[red]ltms init needs an interactive terminal.[/red]\n"
            "Open a terminal and run:  ltms init"
        )
        return 2

    console.print(Text(BANNER, style="cyan"))
    existing = load()
    if config_path().exists():
        _info(console, f"updating existing config at {config_path()}")
        console.print()

    # ---------------------------------------------------------------- search
    console.rule("[bold]search[/bold]", style="grey35")
    console.print()
    console.print("  ltms searches through [bold]SearXNG[/bold], a local meta-search engine.")
    console.print("  It runs in a container and is never exposed outside your machine.")
    console.print()

    runtime = docker_mgr.find_runtime()
    runtime_usable = False
    if runtime is None:
        _warn(console, "no container engine found")
        _info(console, "Windows: install.ps1 sets up Docker inside WSL2, or by hand:")
        _info(console, "           wsl --install -d Ubuntu")
        _info(console, "           wsl -d Ubuntu -u root -- sh -c 'curl -fsSL https://get.docker.com | sh'")
        _info(console, "Linux:   https://docs.docker.com/engine/install/")
    else:
        ready, detail = docker_mgr.runtime_ready(runtime)
        if ready:
            _ok(console, f"{runtime.label} ready (server {detail})")
            runtime_usable = True
        else:
            _warn(console, f"{runtime.label} found but not responding — start it, then rerun `ltms init`")
            _info(console, detail)
    console.print()

    choices = {
        "1": ("ephemeral", "start SearXNG for each run, stop it afterwards"),
        "2": ("warm", "start it once and leave it running between runs"),
        "3": ("external", "I already run SearXNG somewhere"),
    }
    console.print("  [bold]How should ltms handle SearXNG?[/bold]")
    console.print("    [cyan]1[/cyan]  per run      — clean, nothing left behind [dim](+8-15s each run)[/dim]")
    console.print("    [cyan]2[/cyan]  keep warm    — starts once, later runs are instant [dim](~200 MB RAM idle)[/dim]")
    console.print("    [cyan]3[/cyan]  external     — point at a SearXNG you already have")
    console.print()

    default_key = {"ephemeral": "1", "warm": "2", "external": "3"}.get(existing.search.mode, "1")
    picked = Prompt.ask("  choice", choices=list(choices), default=default_key, console=console)
    mode = choices[picked][0]

    url = ""
    if mode == "external":
        url = Prompt.ask("  SearXNG url", default=existing.search.url or "http://127.0.0.1:8080", console=console)
        console.print()
        if docker_mgr.probe(url):
            _ok(console, "reachable, and JSON output is enabled")
        else:
            _warn(console, "could not reach it, or JSON output is disabled there")
            _info(console, "in that instance's settings.yml add:  search.formats: [html, json]")
    elif not runtime_usable:
        _warn(console, "saving this choice, but ltms cannot run until a container runtime is available")

    search = SearchConfig(mode=mode, url=url, port=0, runtime=runtime.name if runtime else "")
    console.print()

    # ----------------------------------------------------------------- model
    console.rule("[bold]model[/bold]", style="grey35")
    console.print()

    with console.status("[cyan]looking for a local model server…[/cyan]", spinner="dots"):
        servers = detect_servers()

    provider = existing.model.provider
    base_url = existing.model.base_url
    models: list[str] = []

    if servers:
        for index, server in enumerate(servers, start=1):
            loaded = f"{len(server.models)} model(s)" if server.models else "[yellow]no model loaded[/yellow]"
            _ok(console, f"[cyan]{index}[/cyan]  {server.label}  [dim]{server.base_url}[/dim]  {loaded}")
        console.print()
        if len(servers) == 1:
            chosen = servers[0]
        else:
            pick = Prompt.ask(
                "  which server", choices=[str(i) for i in range(1, len(servers) + 1)], default="1", console=console
            )
            chosen = servers[int(pick) - 1]
        provider, base_url, models = chosen.provider, chosen.base_url, chosen.models
    else:
        _warn(console, "no local model server found")
        _info(console, "LM Studio (easiest): https://lmstudio.ai — load a model,")
        _info(console, "then Developer tab → turn the local server on")
        _info(console, "Ollama: https://ollama.com/download")
        console.print()
        base_url = Prompt.ask("  model server url", default=base_url, console=console)
        provider = "ollama" if ":11434" in base_url else "openai-compatible"

    console.print()
    chat_models = [name for name in models if not is_embedding_model(name)]
    fast_name = existing.model.fast_name

    if chat_models:
        for name in chat_models[:10]:
            _info(console, name)
        console.print()
        console.print("  [dim]Leave blank to always use whatever the server has loaded.[/dim]")
        model_name = Prompt.ask(
            "  main model [dim](planning, debate, final report)[/dim]",
            default=existing.model.name,
            show_default=False,
            console=console,
        ).strip()

        if len(chat_models) > 1:
            console.print()
            console.print("  [dim]Reading 40 pages is bulk work — a small fast model does it in[/dim]")
            console.print("  [dim]minutes where a large one takes an hour. The two never run at[/dim]")
            console.print("  [dim]the same time, so both do not need to fit in VRAM together.[/dim]")
            fast_name = Prompt.ask(
                "  fast model for reading [dim](blank = use the main one)[/dim]",
                default=fast_name,
                show_default=False,
                console=console,
            ).strip()
    else:
        model_name = existing.model.name

    console.print()
    console.print("  [dim]Extractor agents run in parallel. Keep this at or below the[/dim]")
    console.print("  [dim]server's own limit, or the requests just queue up.[/dim]")
    if provider == "openai-compatible":
        console.print("  [dim]LM Studio: Developer → Settings → 'Serve on local network' page,[/dim]")
        console.print("  [dim]raise max parallel requests to match.[/dim]")
    else:
        console.print("  [dim]Ollama: set OLLAMA_NUM_PARALLEL.[/dim]")
    parallel = int(Prompt.ask("  parallel agents", default=str(existing.model.parallel), console=console))

    model = ModelConfig(
        provider=provider,
        base_url=base_url,
        name=model_name,
        fast_name=fast_name,
        parallel=max(1, parallel),
        context_tokens=existing.model.context_tokens,
    )
    console.print()

    # -------------------------------------------------------------------- ui
    console.rule("[bold]display[/bold]", style="grey35")
    console.print()
    console.print("  [dim]When a coding agent runs ltms there is no visible terminal.[/dim]")
    console.print("  [dim]ltms can open a small window showing the agents working.[/dim]")
    open_window = Confirm.ask("  open a dashboard window", default=existing.ui.open_window, console=console)
    ui = UiConfig(open_window=open_window, theme=existing.ui.theme)

    config = Config(search=search, model=model, ui=ui, runs_dir=existing.runs_dir)
    path = save(config)

    console.print()
    console.rule(style="grey35")
    console.print()
    _ok(console, f"saved {path}")

    # Pre-pulling turns a 4-minute surprise on the first real run into a
    # deliberate wait here.
    if runtime_usable and mode != "external" and not docker_mgr.has_image(runtime):
        console.print()
        if Confirm.ask("  download the SearXNG image now (~250 MB)", default=True, console=console):
            console.print()
            with console.status("[cyan]pulling searxng…[/cyan]", spinner="dots"):
                try:
                    docker_mgr.pull_image(runtime)
                except docker_mgr.SearxngError as error:
                    _warn(console, str(error))
                else:
                    _ok(console, "image ready")

    console.print()
    console.print(
        Panel(
            Text.assemble(
                ("try it\n\n", "dim"),
                ('  ltms "how does WAL work in postgres" --effort low\n\n', "bold cyan"),
                ("point your coding agent at it\n\n", "dim"),
                ("  add to CLAUDE.md:  for web research run  ltms \"<topic>\"\n", "white"),
            ),
            border_style="grey35",
            padding=(1, 2),
        )
    )
    return 0
