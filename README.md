# LessTokenMoreSearch

**Fewer tokens. More search.**

Web research for coding agents, run entirely on your own machine. Your agent
spends ~30 tokens asking for research; local models do the reading.

```bash
ltms research.md
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
  ltms research.md
      │
      ├─ plan     your queries, read from the brief
      ├─ search   parallel SearXNG queries
      ├─ filter   dedupe, drop noise, cap per domain     (no LLM, free)
      ├─ read     fetch pages, extract per-page evidence  ← phase 1
      ├─ rank     order by the extractors' own relevance  ← phase 1
      ├─ debate   role agents argue over the same pool    ← phase 2
      └─ write    one editor produces a plain report      ← phase 2
```

## Write the brief

The calling agent already knows what it is looking for and which wording will
find it. It writes that down; ltms does not guess:

```markdown
# postgres logical replication lag

## queries
- postgres logical replication lag causes
- postgres wal sender bottleneck high write volume
- postgres replication slot disk growth

## questions
- What makes lag grow under heavy writes?
- Which metrics identify the bottleneck?

## notes
Prefer official docs and mailing list threads over blog posts.
```

Only `## queries` is required. `## questions` steers what the extractor agents
pull out of each page and what the final report has to answer; `## notes` is
free-form guidance. A file that is nothing but one search per line also works.

`ltms template` prints a skeleton to fill in.

For a quick one-off, `ltms "some topic"` expands the topic along a few plain
angles instead — no model, no guessing, and reliably worse than queries you
write yourself.

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
- **A container engine** — only for SearXNG, which ltms starts and stops for
  you. On Linux and macOS that is Docker or Podman as usual. On Windows no
  desktop app is needed: ltms drives Docker Engine inside WSL2, and the
  installer sets that up.

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

It installs whatever is missing — uv, ltms, WSL2 with Docker Engine inside it
(plus the SearXNG image), optionally LM Studio — then runs `ltms init`.
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

Add this to your `CLAUDE.md` / `AGENTS.md`:

```markdown
For web research, do not fetch pages yourself. Instead:
1. Write a brief: a markdown file with `## queries` (the searches to run) and
   optionally `## questions` (what you need answered). `ltms template` prints
   the shape.
2. Run: ltms <brief.md> --effort medium
3. Read the report file it prints — all of it, part of it, or none.
```

## Commands

| | |
|---|---|
| `ltms brief.md` | run research from a brief |
| `ltms "topic"` | quick one-off research |
| `ltms template` | print a brief skeleton |
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

On Windows the container runs inside WSL2 and ltms talks to it through
`wsl -d <distro> -u root -- docker`. WSL2 forwards localhost, so the published
port is reachable from Windows at the same address. The settings file is
streamed into the distro and copied into the container rather than bind-mounted
— host path translation is the least portable part of running containers, and a
Windows path means nothing inside WSL.

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
