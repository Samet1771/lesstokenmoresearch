"""SearXNG lifecycle.

The user should never have to think about this. We generate a settings file,
start a container bound to localhost on a free port, wait for it to answer, and
(in ephemeral mode) stop it when the run ends. If a SearXNG is already reachable
we leave it alone.

Three SearXNG settings matter and all three are wrong by default:
  * search.formats must include `json` -- otherwise every API call returns 403
  * server.limiter must be off -- otherwise our own requests get bot-blocked
  * server.secret_key must be set -- otherwise the container refuses to start

On Windows there is usually no Docker on PATH. Rather than require a desktop
application, we drive Docker Engine inside a WSL2 distribution as root. WSL2
forwards localhost, so a container published on 127.0.0.1 inside the distro is
reachable from Windows at the same address.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import httpx

from .config import CONTAINER_NAME, SEARXNG_IMAGE, Config, config_dir

SETTINGS_IN_CONTAINER = "/etc/searxng/settings.yml"
SETTINGS_STAGING = "/tmp/ltms-searxng-settings.yml"

WINDOWS_DOCKER_PATHS = [
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
]


class SearxngError(RuntimeError):
    pass


@dataclass
class Runtime:
    """How to reach a container engine.

    `prefix` is whatever must come before the engine binary -- empty for a
    native install, a `wsl.exe -d <distro> -u root --` incantation otherwise.
    """

    name: str  # docker | wsl-docker
    binary: str
    prefix: list[str] = field(default_factory=list)
    distro: str = ""

    @property
    def argv(self) -> list[str]:
        return [*self.prefix, self.binary]

    @property
    def in_wsl(self) -> bool:
        return bool(self.prefix)

    @property
    def label(self) -> str:
        return f"docker in WSL ({self.distro})" if self.in_wsl else self.name


def _env() -> dict[str, str]:
    # Without this wsl.exe emits UTF-16 and every parse downstream breaks.
    return {**os.environ, "WSL_UTF8": "1"}


def _run(runtime: Runtime, *args: str, timeout: int = 60, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*runtime.argv, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=_env(),
        input=stdin,
    )


def _shell(runtime: Runtime, script: str, timeout: int = 120, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a shell command inside the distro (WSL runtimes only)."""
    if not runtime.in_wsl:
        raise SearxngError("shell access is only available for WSL runtimes")
    return subprocess.run(
        [*runtime.prefix, "sh", "-lc", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=_env(),
        input=stdin,
    )


# --------------------------------------------------------------- discovery ---


def wsl_available() -> bool:
    if sys.platform != "win32" or not shutil.which("wsl"):
        return False
    try:
        result = subprocess.run(
            ["wsl", "--status"], capture_output=True, text=True, timeout=20, env=_env(), check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def wsl_distros() -> list[str]:
    try:
        result = subprocess.run(
            ["wsl", "--list", "--quiet"], capture_output=True, text=True, timeout=20, env=_env(), check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _wsl_runtime(distro: str) -> Runtime:
    return Runtime(
        name="wsl-docker",
        binary="docker",
        prefix=["wsl", "-d", distro, "-u", "root", "--"],
        distro=distro,
    )


def find_runtime(preferred: str = "") -> Runtime | None:
    """Native engine first, then Docker inside a WSL distribution."""
    if preferred != "wsl-docker":
        found = shutil.which("docker")
        if found:
            return Runtime(name="docker", binary=found)
    for path in WINDOWS_DOCKER_PATHS:
        if Path(path).exists():
            return Runtime(name="docker", binary=path)

    if wsl_available():
        for distro in wsl_distros():
            runtime = _wsl_runtime(distro)
            if _shell(runtime, "command -v docker", timeout=60).returncode == 0:
                return runtime
    return None


def runtime_ready(runtime: Runtime) -> tuple[bool, str]:
    """Is the daemon actually up? Start it if we can."""

    def info() -> subprocess.CompletedProcess[str]:
        return _run(runtime, "info", "--format", "{{.ServerVersion}}", timeout=60)

    try:
        result = info()
    except (subprocess.TimeoutExpired, OSError) as error:
        return False, str(error)

    if result.returncode == 0:
        return True, result.stdout.strip()

    # A WSL distro does not run systemd by default, and it shuts down when
    # idle, so dockerd is often simply not started yet. That is normal rather
    # than an error -- start it and retry.
    #
    # This must run in the FOREGROUND. Backgrounding it with `&` looks tempting
    # but WSL tears the session down as soon as the command returns, killing
    # the daemon before it can bind its socket.
    if runtime.in_wsl:
        start = _shell(
            runtime,
            "service docker start 2>&1 || systemctl start docker 2>&1 || true",
            timeout=120,
        )
        for _ in range(20):
            try:
                retry = info()
            except (subprocess.TimeoutExpired, OSError):
                time.sleep(1.0)
                continue
            if retry.returncode == 0:
                return True, retry.stdout.strip()
            time.sleep(1.0)

        hint = (start.stdout or start.stderr).strip().splitlines()
        if hint:
            return False, f"could not start dockerd in {runtime.distro}: {hint[-1][:120]}"

    detail = (result.stderr or result.stdout).strip().splitlines()
    return False, detail[-1] if detail else "daemon not responding"


# ---------------------------------------------------------------- settings ---


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def settings_dir() -> Path:
    return config_dir() / "searxng"


def ensure_settings(force: bool = False) -> Path:
    """Write settings.yml if missing. The secret key is generated once and kept."""
    directory = settings_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "settings.yml"
    if path.exists() and not force:
        return path
    secret = secrets.token_hex(32)
    path.write_text(
        "\n".join(
            [
                "# Generated by lesstokenmoresearch. Safe to edit; delete to regenerate.",
                "use_default_settings: true",
                "",
                "general:",
                "  debug: false",
                '  instance_name: "lesstokenmoresearch"',
                "  donation_url: false",
                "",
                "server:",
                f'  secret_key: "{secret}"',
                "  limiter: false",
                "  image_proxy: false",
                "",
                "search:",
                "  safe_search: 0",
                '  autocomplete: ""',
                "  formats:",
                "    - html",
                "    - json",
                "",
                "ui:",
                "  static_use_hash: true",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def probe(url: str, timeout: float = 3.0) -> bool:
    """Does a working SearXNG with JSON enabled answer at this URL?"""
    base = url.rstrip("/")
    try:
        response = httpx.get(
            f"{base}/search",
            params={"q": "ping", "format": "json"},
            timeout=timeout,
            headers={"accept": "application/json"},
        )
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        response.json()
    except ValueError:
        return False
    return True


# --------------------------------------------------------------- container ---


def container_state(runtime: Runtime) -> str:
    """running | stopped | absent"""
    result = _run(runtime, "ps", "-a", "--filter", f"name=^{CONTAINER_NAME}$", "--format", "{{.State}}")
    state = result.stdout.strip().splitlines()
    if not state:
        return "absent"
    return "running" if state[0].startswith("running") else "stopped"


def container_url(runtime: Runtime) -> str | None:
    result = _run(runtime, "port", CONTAINER_NAME, "8080/tcp")
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    port = lines[0].strip().rsplit(":", 1)[-1]
    if not port.isdigit():
        return None
    return f"http://127.0.0.1:{port}"


def has_image(runtime: Runtime) -> bool:
    result = _run(runtime, "images", "-q", SEARXNG_IMAGE, timeout=90)
    return bool(result.stdout.strip())


def pull_image(runtime: Runtime, on_line: Callable[[str], None] | None = None) -> None:
    process = subprocess.Popen(
        [*runtime.argv, "pull", SEARXNG_IMAGE],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_env(),
    )
    assert process.stdout is not None
    for line in process.stdout:
        if on_line:
            on_line(line.rstrip())
    if process.wait() != 0:
        raise SearxngError(f"could not pull {SEARXNG_IMAGE}")


def _install_settings(runtime: Runtime) -> None:
    """Put settings.yml inside the created container.

    No bind mount: host path translation is the least portable thing about
    containers, and through WSL a Windows path is not even meaningful. For WSL
    the file is streamed into the distro first, then copied in.
    """
    settings = ensure_settings()
    text = settings.read_text(encoding="utf-8")

    if runtime.in_wsl:
        staged = _shell(runtime, f"cat > {SETTINGS_STAGING}", stdin=text)
        if staged.returncode != 0:
            raise SearxngError(f"could not stage settings inside {runtime.distro}: {staged.stderr[:200]}")
        source = SETTINGS_STAGING
    else:
        source = str(settings)

    copied = _run(runtime, "cp", source, f"{CONTAINER_NAME}:{SETTINGS_IN_CONTAINER}", timeout=60)
    if copied.returncode != 0:
        raise SearxngError(f"could not install settings into the container: {(copied.stderr or copied.stdout)[:300]}")


def start_container(runtime: Runtime, port: int) -> str:
    """Create, copy the settings in, then start."""
    _run(runtime, "rm", "-f", CONTAINER_NAME, timeout=60)

    created = _run(
        runtime,
        "create",
        "--name",
        CONTAINER_NAME,
        "-p",
        f"127.0.0.1:{port}:8080",
        "-e",
        f"SEARXNG_BASE_URL=http://localhost:{port}/",
        "-e",
        f"SEARXNG_SETTINGS_PATH={SETTINGS_IN_CONTAINER}",
        SEARXNG_IMAGE,
        timeout=120,
    )
    if created.returncode != 0:
        raise SearxngError((created.stderr or created.stdout).strip()[:400])

    try:
        _install_settings(runtime)
    except SearxngError:
        _run(runtime, "rm", "-f", CONTAINER_NAME)
        raise

    started = _run(runtime, "start", CONTAINER_NAME, timeout=120)
    if started.returncode != 0:
        detail = (started.stderr or started.stdout).strip()[:400]
        _run(runtime, "rm", "-f", CONTAINER_NAME)
        raise SearxngError(detail)
    return f"http://127.0.0.1:{port}"


def wait_ready(url: str, timeout: float = 90.0, on_tick: Callable[[float], None] | None = None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe(url, timeout=2.0):
            return
        if on_tick:
            on_tick(deadline - time.time())
        time.sleep(1.0)
    raise SearxngError(f"SearXNG did not become ready within {timeout:.0f}s")


def stop_container(runtime: Runtime) -> bool:
    if container_state(runtime) == "absent":
        return False
    _run(runtime, "rm", "-f", CONTAINER_NAME, timeout=60)
    return True


def logs(runtime: Runtime, lines: int = 40) -> str:
    result = _run(runtime, "logs", "--tail", str(lines), CONTAINER_NAME)
    return (result.stdout + result.stderr).strip()


NO_RUNTIME_HELP = (
    "No container engine found. ltms runs SearXNG in a container.\n"
    "  Windows: install.ps1 sets up Docker inside WSL2 for you, or by hand:\n"
    "             wsl --install -d Ubuntu\n"
    "             wsl -d Ubuntu -u root -- sh -c 'curl -fsSL https://get.docker.com | sh'\n"
    "  Linux:   https://docs.docker.com/engine/install/\n"
    "  macOS:   https://docs.docker.com/desktop/setup/install/mac-install/\n"
    "Already run SearXNG somewhere? `ltms init` -> 'external'."
)


@contextlib.contextmanager
def searxng(config: Config, report: Callable[[str, str], None] | None = None) -> Iterator[str]:
    """Yield a working SearXNG base URL, managing the container as configured."""

    def say(text: str, level: str = "info") -> None:
        if report:
            report(text, level)

    mode = config.search.mode
    configured = config.search.url or os.environ.get("LTMS_SEARXNG_URL", "")
    if configured and probe(configured):
        say(f"using SearXNG already running at {configured}")
        yield configured.rstrip("/")
        return

    if mode == "external":
        raise SearxngError(
            f"search.mode is 'external' but nothing answered at {configured or '(no url set)'}.\n"
            "Set search.url, or run `ltms init` to let ltms manage SearXNG itself."
        )

    runtime = find_runtime(config.search.runtime)
    if runtime is None:
        raise SearxngError(NO_RUNTIME_HELP)

    ready, detail = runtime_ready(runtime)
    if not ready:
        raise SearxngError(f"{runtime.label} is not responding ({detail}).")

    if container_state(runtime) == "running":
        url = container_url(runtime)
        if url and probe(url):
            say(f"reusing warm SearXNG at {url}")
            try:
                yield url
            finally:
                if mode == "ephemeral":
                    stop_container(runtime)
                    say("stopped SearXNG")
            return
        stop_container(runtime)

    if not has_image(runtime):
        say("pulling searxng image (~250 MB, first run only)", "warn")
        pull_image(runtime, on_line=lambda line: say(line[:80]))

    port = config.search.port or free_port()
    say(f"starting SearXNG on port {port} via {runtime.label}")
    url = start_container(runtime, port)
    try:
        wait_ready(url)
    except SearxngError:
        tail = logs(runtime)
        stop_container(runtime)
        raise SearxngError(f"SearXNG failed to start.\n{tail[-600:]}")

    say("SearXNG ready")
    try:
        yield url
    finally:
        if mode == "ephemeral":
            stop_container(runtime)
            say("stopped SearXNG")
