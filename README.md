# LessTokenMoreSearch

**Fewer tokens. More search.**

Web research for coding agents, run entirely on your own machine. Your agent
spends ~30 tokens asking for research; local models do the reading.

```bash
ltms "how does postgres WAL replication handle network partitions"
```

```
done · 68 sources · 34 domains · ~/.config/ltms/runs/20260912-143002-how-does-postgres/report.md
```

That one line is the entire cost to the calling agent. The report is a file, so
the agent reads all of it, part of it, or none of it.

---

## Why

When Claude Code or Codex researches something on the web, every page it fetches
lands in its context window. Forty pages is a few hundred thousand tokens spent
before any thinking happens.

LessTokenMoreSearch moves that work to local models. Small agents search, read
and argue on your hardware; only a compact sourced report crosses back.

## How it works

```
  ltms "topic"
      │
      ├─ plan     a local model writes the search queries
      ├─ search   parallel SearXNG queries
      ├─ filter   dedupe, drop noise, cap per domain     (no LLM, free)
      ├─ read     fetch pages, extract per-page evidence  ← phase 1
      ├─ rank     order by the extractors' own relevance  ← phase 1
      ├─ debate   role agents argue over the same pool    ← phase 2
      └─ write    one editor produces a plain report      ← phase 2
```

While it runs, a small dashboard window shows the agents working. The research
never depends on that window being open.

## Status

Early. Phase 0 (plan, search, filter) works end to end. Reading, ranking,
debating and report writing are in progress.

## Requirements

- Python 3.10+
- **A local model server.** [LM Studio](https://lmstudio.ai) is the easiest:
  install it, download a model, and switch the local server on from the
  Developer tab. [Ollama](https://ollama.com), llama.cpp and vLLM work too.
- **Docker or Podman** — only for SearXNG, which ltms starts and stops for you.
  On Windows, Podman needs no desktop app:
  `winget install RedHat.Podman` then `podman machine init && podman machine start`.

A 14B class model at 4-bit is a good starting point on 16 GB of VRAM.

## Install

**Windows — one line, installs everything:**

```powershell
irm https://raw.githubusercontent.com/Samet1771/lesstokenmoresearch/main/install.ps1 | iex
```

From `cmd.exe`:

```bat
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Samet1771/lesstokenmoresearch/main/install.ps1 | iex"
```

It installs whatever is missing — uv, ltms, Podman (with its machine started
and the SearXNG image pulled), optionally LM Studio — then runs `ltms init`.
Re-running it is safe; every step checks first.

**Already have the pieces:**

```bash
uv tool install git+https://github.com/Samet1771/lesstokenmoresearch
ltms init
```

(Not on PyPI yet — `uv tool install lesstokenmoresearch` will work once it is.)

The wizard finds your model server, asks how SearXNG should be managed, and
downloads what is missing. `ltms status` shows the same picture any time.

## Use it from a coding agent

Add one line to your `CLAUDE.md` / `AGENTS.md`:

```markdown
For web research, run: ltms "<topic>" --effort medium
Read the report file it prints. Do not fetch pages yourself.
```

## Commands

| | |
|---|---|
| `ltms "topic"` | run research |
| `ltms init` | interactive setup |
| `ltms watch [run]` | attach the dashboard to a run |
| `ltms runs` | list recent runs |
| `ltms status` | show config and what is reachable |
| `ltms stop` | stop the managed SearXNG container |

Options: `--effort low\|medium\|high`, `--read N`, `--json`, `--no-window`, `--quiet`

## Configuration

`ltms init` writes a small TOML file (`ltms status` prints its path).

```toml
[search]
mode = "ephemeral"   # ephemeral | warm | external
url = ""             # for mode = "external"

[model]
provider = "openai-compatible"        # or "ollama"
base_url = "http://127.0.0.1:1234/v1" # LM Studio's default
name = ""                             # blank = whatever the server has loaded
fast_name = ""                        # optional: small model for the reading pass
parallel = 4

[ui]
open_window = true
```

SearXNG needs three non-default settings to be usable as an API — JSON output
on, rate limiter off, secret key set. `ltms` generates that config for you and
binds the container to `127.0.0.1` only.

Leaving `name` blank is the normal way to use LM Studio: you pick the model in
the app, and ltms uses whatever is loaded.

`fast_name` matters more than it looks. Reading forty pages is bulk work, and
throughput decides whether a run takes three minutes or an hour — on one 16 GB
machine a 4B model read at 25 tok/s while a 27B reasoning model managed 4.8.
Planning and the final report are a handful of calls where quality shows
instead. The two phases never overlap, so both models do not have to fit in
VRAM at once; the server swaps once per run.

Avoid reasoning models for the reading pass entirely: they spend their token
budget thinking and return nothing.

Extractor agents run concurrently, so raise the server's own parallel-request
limit to match `parallel` — otherwise the requests queue and nothing is gained.

## No cloud

This project does not call hosted model APIs, by design. Local servers that
happen to speak the OpenAI protocol (LM Studio, llama.cpp, vLLM) are supported
because they run on your machine.

## License

MIT
