"""Configuration: where it lives, how it is read and written.

Config is a small TOML file. Reading uses stdlib tomllib; writing is done by hand
so the package needs no TOML writer dependency.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

APP_NAME = "ltms"

# LM Studio is the default because it is the easiest local server to get
# running: install, load a model, flip the server on. Ollama and anything else
# speaking either protocol work just as well.
DEFAULT_MODEL_URL = "http://127.0.0.1:1234/v1"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
SEARXNG_IMAGE = "searxng/searxng:latest"
CONTAINER_NAME = "ltms-searxng"

# Probed in order by `ltms init` and `ltms status`.
KNOWN_SERVERS: list[tuple[str, str, str]] = [
    ("LM Studio", "openai-compatible", DEFAULT_MODEL_URL),
    ("Ollama", "ollama", DEFAULT_OLLAMA_URL),
    ("llama.cpp", "openai-compatible", "http://127.0.0.1:8081/v1"),
    ("vLLM", "openai-compatible", "http://127.0.0.1:8000/v1"),
]


def config_dir() -> Path:
    """Per-user config directory, following platform convention."""
    override = os.environ.get("LTMS_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.toml"


def documents_dir() -> Path:
    """The user's Documents folder, as the system actually defines it.

    On Windows this is a shell known folder, not necessarily ~/Documents --
    OneDrive backup moves it, and guessing lands reports somewhere the person
    will never look.
    """
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes as wintypes

            class _Guid(ctypes.Structure):
                _fields_ = [
                    ("d1", wintypes.DWORD),
                    ("d2", wintypes.WORD),
                    ("d3", wintypes.WORD),
                    ("d4", ctypes.c_byte * 8),
                ]

            # FOLDERID_Documents
            guid = _Guid(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_byte * 8)(*b"\xad\xb4\x6c\x85\x48\x03\x69\xc7"))
            buffer = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(guid), 0, None, ctypes.byref(buffer)
            ) == 0 and buffer.value:
                return Path(buffer.value)
        except Exception:  # noqa: BLE001 - any failure just means "guess"
            pass
    else:
        # XDG records a translated or relocated Documents folder here.
        user_dirs = Path.home() / ".config" / "user-dirs.dirs"
        if user_dirs.exists():
            try:
                for line in user_dirs.read_text(encoding="utf-8").splitlines():
                    if line.startswith("XDG_DOCUMENTS_DIR="):
                        value = line.split("=", 1)[1].strip().strip('"')
                        return Path(value.replace("$HOME", str(Path.home())))
            except OSError:
                pass
    return Path.home() / "Documents"


@dataclass
class SearchConfig:
    # ephemeral: start SearXNG per run, stop it afterwards
    # warm:      start it once, leave it running between runs
    # external:  do not manage anything, use `url` as-is
    mode: str = "ephemeral"
    url: str = ""
    port: int = 0  # 0 = pick a free port at start time
    runtime: str = ""  # "docker" | "wsl-docker"; empty = autodetect
    # SearXNG groups its engines by category. `general` is the web engines that
    # rate-limit hardest; `it` is stackoverflow, github, mdn and friends, which
    # are both better sources for technical research and do not throttle a
    # single machine. Measured on one query: general alone returned 20 results
    # with two engines blocked, general+it returned 81 with none blocked.
    categories: str = "general,it"


@dataclass
class ModelConfig:
    provider: str = "openai-compatible"  # "openai-compatible" | "ollama"
    base_url: str = DEFAULT_MODEL_URL
    # Empty means "whatever the server currently has loaded" -- which is the
    # normal way to use LM Studio, where you pick the model in the app.
    name: str = ""
    # Reading 40 pages and writing one report are different jobs. The extraction
    # pass is bulk work where speed decides whether a run takes 3 minutes or an
    # hour; planning and the final report are a handful of calls where quality
    # shows. Leave this empty to use `name` for everything.
    fast_name: str = ""
    # The reading model may live on a different server -- a small model in
    # Ollama while the big one stays loaded in LM Studio, say. Empty means "the
    # same server as the report model".
    fast_provider: str = ""
    fast_base_url: str = ""
    # How many extractor agents run at once. Should not exceed the server's
    # own parallelism (Ollama: OLLAMA_NUM_PARALLEL) or requests just queue.
    parallel: int = 4
    context_tokens: int = 32768

    def for_role(self, role: str) -> "ModelConfig":
        """role: 'fast' for bulk extraction, anything else for reasoning work."""
        if role != "fast" or not self.fast_name:
            return self
        return replace(
            self,
            name=self.fast_name,
            provider=self.fast_provider or self.provider,
            base_url=self.fast_base_url or self.base_url,
        )


@dataclass
class UiConfig:
    # Open a separate terminal window with the live dashboard when the tool is
    # invoked without a TTY (i.e. by a coding agent).
    open_window: bool = True
    theme: str = "cute"


@dataclass
class Config:
    search: SearchConfig = field(default_factory=SearchConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    runs_dir: str = ""
    # Where reports land when a person asked for one. An agent names its own
    # destination; a person should not have to go digging in an app folder.
    reports_dir: str = ""

    @property
    def runs_path(self) -> Path:
        return Path(self.runs_dir).expanduser() if self.runs_dir else config_dir() / "runs"

    @property
    def reports_path(self) -> Path:
        return Path(self.reports_dir).expanduser() if self.reports_dir else documents_dir() / "ltms"


def _coerce(section_cls: Any, raw: dict[str, Any]) -> Any:
    known = {f.name: f.type for f in fields(section_cls)}
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key in known:
            kwargs[key] = value
    return section_cls(**kwargs)


def load() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    return Config(
        search=_coerce(SearchConfig, raw.get("search", {})),
        model=_coerce(ModelConfig, raw.get("model", {})),
        ui=_coerce(UiConfig, raw.get("ui", {})),
        runs_dir=raw.get("runs_dir", ""),
        reports_dir=raw.get("reports_dir", ""),
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save(config: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# lesstokenmoresearch configuration",
        "# Regenerate interactively with:  ltms init",
        "",
    ]
    for key in ("runs_dir", "reports_dir"):
        value = getattr(config, key)
        if value:
            lines.append(f"{key} = {_toml_value(value)}")
    if config.runs_dir or config.reports_dir:
        lines.append("")
    for name in ("search", "model", "ui"):
        lines.append(f"[{name}]")
        for key, value in asdict(getattr(config, name)).items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def exists() -> bool:
    return config_path().exists()
