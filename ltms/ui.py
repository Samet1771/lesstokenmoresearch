"""The live dashboard.

The worker never draws anything. It appends events to progress.jsonl and this
module -- running either in-place or in a separate terminal window -- replays
them. Keeping the two apart means the research never depends on a window being
open, and you can attach to a run at any time with `ltms watch`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .runs import PROGRESS_FILE, RunState

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

STAGE_LABELS = {
    "plan": "plan",
    "search": "search",
    "filter": "filter",
    "read": "read",
    "rank": "rank",
    "debate": "debate",
    "write": "write",
}

STAGE_GLYPH = {
    "pending": ("○", "grey42"),
    "run": ("", "cyan"),  # replaced by the spinner frame
    "ok": ("✓", "green"),
    "warn": ("!", "yellow"),
    "fail": ("✗", "red"),
    "skip": ("–", "grey42"),
}

AGENT_STATE = {
    "idle": ("·", "grey42", "idle"),
    "fetch": ("◌", "cyan", "fetching"),
    "read": ("◉", "green", "reading"),
    "think": ("◈", "magenta", "thinking"),
    "write": ("✎", "yellow", "writing"),
    "done": ("✓", "green", "done"),
    "fail": ("✗", "red", "failed"),
}

MASCOT = "(ᵔᴗᵔ)"


def _short_url(url: str, width: int = 44) -> str:
    text = url.replace("https://", "").replace("http://", "").rstrip("/")
    if len(text) <= width:
        return text
    head = text[: width - 12]
    return f"{head}…{text[-10:]}"


def _bar(done: int, total: int, width: int = 18) -> Text:
    if total <= 0:
        return Text("─" * width, style="grey35")
    filled = max(0, min(width, round(width * done / total)))
    bar = Text()
    bar.append("━" * filled, style="cyan")
    if filled < width:
        bar.append("╸", style="cyan")
        bar.append("─" * (width - filled - 1), style="grey35")
    return bar


def _fmt_count(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(int(value))


def _fmt_elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def _header(state: RunState, frame: str) -> Panel:
    title = Text()
    title.append("LessTokenMoreSearch ", style="bold cyan")
    title.append("· fewer tokens, more search", style="dim")

    body = Text()
    body.append(f"{MASCOT}  ", style="magenta" if not state.finished else "green")
    body.append(state.query or "…", style="bold white")
    if state.effort:
        body.append(f"\n{'':7}effort ", style="dim")
        body.append(state.effort, style="yellow")
        if state.run_id:
            body.append(f"  ·  run {state.run_id}", style="dim")
    return Panel(body, title=title, title_align="left", border_style="cyan", padding=(0, 1))


def _stages(state: RunState, frame: str) -> RenderableType:
    table = Table.grid(padding=(0, 1))
    table.add_column(width=2, justify="center")
    table.add_column(width=7)
    table.add_column(ratio=1)

    for name in state.stage_order:
        stage = state.stages[name]
        glyph, color = STAGE_GLYPH.get(stage.status, ("○", "grey42"))
        if stage.status == "run":
            glyph = frame
        label_style = "white" if stage.status in ("run", "ok") else "grey42"

        detail: RenderableType
        if stage.total:
            detail = Text.assemble(
                _bar(stage.done, stage.total),
                ("  ", ""),
                (f"{stage.done}/{stage.total}", "white"),
                ("  " + stage.detail if stage.detail else "", "dim"),
            )
        else:
            detail = Text(stage.detail, style="dim")

        table.add_row(
            Text(glyph, style=color),
            Text(STAGE_LABELS.get(name, name), style=label_style),
            detail,
        )
    return table


def _agents(state: RunState) -> RenderableType | None:
    if not state.agents:
        return None
    table = Table.grid(padding=(0, 1))
    table.add_column(width=3)
    table.add_column(width=9)
    table.add_column(width=2, justify="center")
    table.add_column(width=9)
    table.add_column(ratio=1)
    table.add_column(width=5, justify="right")

    for agent in state.agents.values():
        glyph, color, label = AGENT_STATE.get(agent.state, ("·", "grey42", agent.state))
        score = Text("")
        if agent.score is not None:
            tone = "green" if agent.score >= 0.6 else "yellow" if agent.score >= 0.3 else "grey42"
            score = Text(f"★{agent.score:.1f}", style=tone)
        detail = agent.detail
        if detail.startswith("http"):
            detail = _short_url(detail)
        table.add_row(
            Text(""),
            Text(agent.id, style="bold grey70"),
            Text(glyph, style=color),
            Text(label, style=color),
            Text(detail, style="dim"),
            score,
        )
    return table


def _notes(state: RunState) -> RenderableType | None:
    if not state.notes:
        return None
    table = Table.grid(padding=(0, 1))
    table.add_column(width=2, justify="center")
    table.add_column(ratio=1)
    tones = {"info": ("·", "grey42"), "warn": ("!", "yellow"), "error": ("✗", "red")}
    for level, text in state.notes[-4:]:
        glyph, color = tones.get(level, ("·", "grey42"))
        table.add_row(Text(glyph, style=color), Text(text, style=color if level != "info" else "dim"))
    return table


def _footer(state: RunState) -> RenderableType:
    line = Text()
    saved = state.metrics.get("tokens_saved")
    if saved:
        line.append("◇ saved ≈ ", style="dim")
        line.append(_fmt_count(saved), style="bold green")
        line.append(" tokens", style="dim")
        line.append("   ·   ", style="grey35")
    line.append("elapsed ", style="dim")
    line.append(_fmt_elapsed(state.elapsed), style="white")

    if state.finished:
        line.append("   ·   ", style="grey35")
        if state.status == "ok":
            line.append("done ", style="bold green")
            line.append(state.summary or "", style="dim")
        else:
            line.append(f"{state.status} ", style="bold red")
            line.append(state.summary or "", style="dim")
    return line


def render(state: RunState, tick: int) -> RenderableType:
    frame = SPINNER[tick % len(SPINNER)]
    blocks: list[RenderableType] = [_header(state, frame), Text("")]
    blocks.append(_stages(state, frame))

    agents = _agents(state)
    if agents is not None:
        blocks.extend([Text(""), agents])

    notes = _notes(state)
    if notes is not None:
        blocks.extend([Text(""), notes])

    blocks.extend([Text(""), _footer(state)])

    if state.finished and state.report:
        blocks.append(Text.assemble(("report ", "dim"), (state.report, "cyan underline")))

    return Panel(Group(*blocks), border_style="grey35", padding=(1, 2))


class _Tail:
    """Incremental JSONL reader; never consumes a half-written final line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0

    def poll(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
        if not chunk:
            return []
        cut = chunk.rfind(b"\n")
        if cut == -1:
            return []
        self.offset += cut + 1
        events = []
        for line in chunk[:cut].split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return events


def watch(run_dir: Path, console: Console | None = None, hold: bool = True) -> RunState:
    """Follow a run to completion, drawing the dashboard."""
    console = console or Console()
    tail = _Tail(run_dir / PROGRESS_FILE)
    state = RunState()
    tick = 0
    idle_since = time.time()

    with Live(render(state, tick), console=console, refresh_per_second=12, transient=False) as live:
        while True:
            events = tail.poll()
            if events:
                idle_since = time.time()
                for event in events:
                    state.apply(event)
            tick += 1
            live.update(render(state, tick))
            if state.finished:
                break
            # The worker process may have died without writing an `end` event.
            if time.time() - idle_since > 900:
                state.finished = True
                state.status = "stalled"
                state.summary = "no progress for 15 minutes"
                live.update(render(state, tick))
                break
            time.sleep(1 / 12)

    if hold and state.finished:
        console.print()
        console.print("[dim]this window stays open — press Ctrl+C or close it[/dim]")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return state
