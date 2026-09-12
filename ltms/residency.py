"""Getting a model out of VRAM when we are done with it.

The pipeline uses two models in sequence: a small one reads every page, then a
large one writes the report. They never run at the same time, so only one needs
to be resident -- but neither LM Studio nor Ollama evicts the first one on its
own.

Measured on a 16 GB card: leaving a 2.68 GB reader loaded next to a 14.33 GB
writer puts the pair at 17 GB, the writer spills into system RAM, and
generation crawls. Unloading first is the difference between a report in two
minutes and a report in twenty.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

import httpx

from .config import ModelConfig


def _lms() -> str | None:
    return shutil.which("lms")


@dataclass
class Resident:
    name: str
    context: int = 0
    parallel: int = 1

    @property
    def context_per_slot(self) -> int:
        """What one concurrent request actually gets.

        Servers divide a model's context between parallel slots, so a model
        loaded with 8192 context and 4 slots refuses any prompt over ~2048 --
        with an error that says nothing about slots.
        """
        return self.context // max(1, self.parallel) if self.context else 0


def _parse_lms_ps(output: str) -> list[Resident]:
    rows: list[Resident] = []
    for line in output.splitlines():
        columns = [cell.strip() for cell in re.split(r"\s{2,}", line.strip()) if cell.strip()]
        if len(columns) < 2 or columns[0].upper() == "IDENTIFIER":
            continue
        numbers = [int(cell) for cell in columns if cell.isdigit()]
        rows.append(
            Resident(
                name=columns[0],
                context=numbers[0] if numbers else 0,
                parallel=numbers[1] if len(numbers) > 1 else 1,
            )
        )
    return rows


def residents(config: ModelConfig, timeout: float = 6.0) -> list[Resident]:
    """What is in memory right now, and how much room each request really has."""
    if config.provider == "ollama":
        base = config.base_url.rstrip("/")
        try:
            response = httpx.get(f"{base}/api/ps", timeout=timeout)
            response.raise_for_status()
            entries = response.json().get("models", [])
        except (httpx.HTTPError, ValueError, KeyError):
            return []
        return [Resident(name=entry["name"]) for entry in entries if entry.get("name")]

    binary = _lms()
    if not binary:
        return []
    try:
        result = subprocess.run(
            [binary, "ps"], capture_output=True, text=True, timeout=timeout, check=False,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    return _parse_lms_ps(result.stdout)


def loaded(config: ModelConfig, timeout: float = 6.0) -> list[str]:
    """Which models are currently in memory on this server."""
    return [resident.name for resident in residents(config, timeout)]


def context_warning(config: ModelConfig, needed_tokens: int) -> str:
    """Tell the user before a run fails, not after.

    Returns "" when there is nothing to say -- including when we cannot see the
    server's settings at all, which is the normal case for most backends.
    """
    for resident in residents(config):
        if config.name and resident.name != config.name:
            continue
        room = resident.context_per_slot
        if room and room < needed_tokens:
            return (
                f"{resident.name} is loaded with {resident.context} context across "
                f"{resident.parallel} parallel slots, so each page gets about {room} tokens "
                f"and needs {needed_tokens}. Reload it with fewer slots or more context, "
                f"or lower model.parallel."
            )
    return ""


def unload(name: str, config: ModelConfig, timeout: float = 30.0) -> bool:
    """Evict one model. Returns whether the server acknowledged it."""
    if not name:
        return False

    if config.provider == "ollama":
        # Ollama has no unload call; a request with keep_alive 0 releases it.
        base = config.base_url.rstrip("/")
        try:
            response = httpx.post(
                f"{base}/api/generate",
                json={"model": name, "keep_alive": 0, "prompt": ""},
                timeout=timeout,
            )
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    binary = _lms()
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "unload", name], capture_output=True, text=True, timeout=timeout, check=False,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def release(previous: ModelConfig, upcoming: ModelConfig) -> str:
    """Free the model we have finished with before the next one loads.

    Returns a short line for the run log, or "" when there was nothing to do --
    the same model, or a server that does not let us evict anything.
    """
    if not previous.name:
        return ""

    same_server = previous.base_url.rstrip("/") == upcoming.base_url.rstrip("/")
    if same_server and previous.name == upcoming.name:
        return ""

    resident = loaded(previous)
    if resident and previous.name not in resident:
        return ""

    return f"unloaded {previous.name}" if unload(previous.name, previous) else ""
