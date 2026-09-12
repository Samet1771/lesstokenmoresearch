"""Run directories and the progress event stream.

A run owns one directory. Progress is an append-only JSONL file: the worker
appends, any number of watchers tail it. That is the whole coordination
protocol -- no sockets, no job server. A watcher that attaches late replays the
file from the start and ends up in the same state.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

PROGRESS_FILE = "progress.jsonl"
REPORT_FILE = "report.md"


def _slug(text: str, limit: int = 32) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:limit].rstrip("-")) or "run"


def new_run_id(query: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(query)}"


class RunWriter:
    """Owns a run directory and appends progress events to it."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.run_id = run_id
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "extracts").mkdir(exist_ok=True)
        self._progress = self.dir / PROGRESS_FILE
        self._started = time.time()

    @property
    def progress_path(self) -> Path:
        return self._progress

    @property
    def report_path(self) -> Path:
        return self.dir / REPORT_FILE

    def emit(self, event_type: str, **payload: Any) -> None:
        record = {"t": round(time.time() - self._started, 2), "type": event_type, **payload}
        line = json.dumps(record, ensure_ascii=False)
        with self._progress.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # Convenience wrappers -- these names are the protocol the dashboard reads.

    def meta(self, query: str, effort: str, **extra: Any) -> None:
        self.emit("meta", run_id=self.run_id, query=query, effort=effort, **extra)

    def stage(self, name: str, status: str, detail: str = "") -> None:
        """status: pending | run | ok | warn | fail | skip"""
        self.emit("stage", name=name, status=status, detail=detail)

    def progress(self, name: str, done: int, total: int) -> None:
        self.emit("progress", name=name, done=done, total=total)

    def agent(self, agent_id: str, state: str, detail: str = "", score: float | None = None) -> None:
        """state: idle | fetch | read | think | write | done | fail"""
        self.emit("agent", id=agent_id, state=state, detail=detail, score=score)

    def note(self, text: str, level: str = "info") -> None:
        self.emit("note", text=text, level=level)

    def metric(self, **values: Any) -> None:
        self.emit("metric", **values)

    def end(self, status: str, report: str = "", summary: str = "") -> None:
        self.emit("end", status=status, report=report, summary=summary)

    def write_json(self, name: str, data: Any) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


@dataclass
class AgentView:
    id: str
    state: str = "idle"
    detail: str = ""
    score: float | None = None


@dataclass
class StageView:
    name: str
    status: str = "pending"
    detail: str = ""
    done: int = 0
    total: int = 0


@dataclass
class RunState:
    """Everything a dashboard needs, rebuilt by replaying the event stream."""

    run_id: str = ""
    query: str = ""
    effort: str = ""
    elapsed: float = 0.0
    stages: dict[str, StageView] = field(default_factory=dict)
    stage_order: list[str] = field(default_factory=list)
    agents: dict[str, AgentView] = field(default_factory=dict)
    notes: list[tuple[str, str]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    finished: bool = False
    status: str = ""
    report: str = ""
    summary: str = ""

    def apply(self, event: dict[str, Any]) -> None:
        self.elapsed = max(self.elapsed, float(event.get("t", 0.0)))
        kind = event.get("type")

        if kind == "meta":
            self.run_id = event.get("run_id", self.run_id)
            self.query = event.get("query", "")
            self.effort = event.get("effort", "")

        elif kind == "stage":
            name = event["name"]
            stage = self._stage(name)
            stage.status = event.get("status", stage.status)
            if event.get("detail"):
                stage.detail = event["detail"]

        elif kind == "progress":
            stage = self._stage(event["name"])
            stage.done = event.get("done", 0)
            stage.total = event.get("total", 0)
            if stage.status == "pending":
                stage.status = "run"

        elif kind == "agent":
            agent = self.agents.setdefault(event["id"], AgentView(id=event["id"]))
            agent.state = event.get("state", agent.state)
            agent.detail = event.get("detail", "")
            agent.score = event.get("score")

        elif kind == "note":
            self.notes.append((event.get("level", "info"), event.get("text", "")))
            del self.notes[:-6]

        elif kind == "metric":
            self.metrics.update({k: v for k, v in event.items() if k not in ("t", "type")})

        elif kind == "end":
            self.finished = True
            self.status = event.get("status", "")
            self.report = event.get("report", "")
            self.summary = event.get("summary", "")

    def _stage(self, name: str) -> StageView:
        if name not in self.stages:
            self.stages[name] = StageView(name=name)
            self.stage_order.append(name)
        return self.stages[name]


def read_events(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # a partially flushed final line; it will be read next poll


def load_state(path: Path) -> RunState:
    state = RunState()
    for event in read_events(path):
        state.apply(event)
    return state


def latest_run(root: Path) -> Path | None:
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir() and (p / PROGRESS_FILE).exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def resolve_run(root: Path, run_id: str | None) -> Path | None:
    if not run_id:
        return latest_run(root)
    direct = root / run_id
    if direct.exists():
        return direct
    matches = sorted(p for p in root.glob(f"*{run_id}*") if p.is_dir())
    return matches[-1] if matches else None
