# LessTokenMoreSearch

**Fewer tokens. More search.**

Web research for coding agents, run entirely on your own machine. Your agent
spends ~30 tokens asking for research; local models do the reading.

```bash
ltms research.md -o notes/wal.md
```

```
done · 5 sources read · 19 facts · 360 tokens · notes/wal.md
```

That one line is the entire cost to the calling agent. The report is a file, so
the agent reads all of it, part of it, or none of it.

Measured on that run: 17,302 tokens of page text went into local models and a
360-token report came back — 48x, or 494x if the agent only needs to know the
research happened.

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
      ├─ filter   dedupe, drop noise, cap per domain    (no model, free)
      ├─ read     fetch each page, one reader per page
      ├─ rank     order by the readers' own relevance scores
      ├─ debate   role agents argue over the same evidence
      └─ write    one editor produces a plain report
```

Each page gets its own reader: one request carrying only the system prompt and
that page, one markdown note written to disk, then the reader is gone. No
reader shares a conversation with another, so one page cannot colour how the
next is read and a reader that fails takes nothing down with it.

The two model roles never overlap, so when reading finishes ltms evicts the
reading model before the report model loads. Leaving both resident on a 16 GB
card measured 17 GB, spilled the larger model into system RAM, and turned a
two-minute report into a twenty-minute one.

## Where the report goes

Whoever asked decides.

An agent names its own file with `-o` and gets exactly that path. Without `-o`
the report stays in the run directory and the printed line points at it — an
agent reads a path, not a folder.

A person working in the console gets `~/ltms/reports/<topic>.md`, under a name
they can read. A second run on the same topic lands beside the first rather
than replacing it.

Either way the run directory keeps its own copy, next to the evidence that
produced it. `reports_dir` in the config moves that folder if you want it
somewhere else.

## One folder

Everything ltms is, and everything it writes, lives in `~/ltms`:

```
~/ltms/
  bin/ltms.cmd      what PATH points at
  app/              the program
  config.toml       written by `ltms init`
  reports/          your reports
  searxng/          the generated SearXNG settings
  runs/<id>/
    brief.json        what was asked
    sources.json      every candidate the search found
    extracts/         one markdown note per page read
    findings.json     the ranked evidence
    report.md         the thing you read
    progress.jsonl    the event stream the dashboard replays
```

The platform-correct answer is three separate hidden folders — AppData for the
config, somewhere else for the data, Documents for the output — and that makes
a tool impossible to look at, back up, or delete. Deleting `~/ltms` and one
PATH entry removes ltms completely.

`LTMS_HOME` moves the whole folder. An install from an older version is pulled
into the new one the first time you run it, and ltms says so when it does.

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

## The console

```bash
ltms
```

A full-screen terminal app. Type a topic or a brief path and the run starts
there; the transcript fills in as it happens rather than arriving at the end.

```
 _   _____ __  __ ___
| | |_   _|  \/  / __|   LessTokenMoreSearch
| |__ | | | |\/| \__ \   fewer tokens, more search
|____||_| |_|  |_|___/

 14:19:31  ●  brief wal.md · 5 queries · effort low
 14:19:36  ✓  search  45 hits · 23 domains
 14:19:36  ✓  filter  25 kept  (10 duplicate · 3 domain_cap)
 14:19:38  ·  fetched 6 pages, 6 readable
 14:22:34  !  the reading model spent 76% of its output thinking
 14:22:34  ✓  read    5 pages read · 19 facts
 14:23:59  ◆  done    5 sources read · 19 facts · 360 tokens
              ~/ltms/runs/20260912-141931-sqlite-wal/report.md

 ⠴ read     ━━━━━━━━━━╸───────  7/12
   scout-1  ◉ reading   sqlite.org/wal.html
   scout-2  ◌ fetching  berthub.eu/articles/posts/…

┌────────────────────────────────────────────────────────────────────────┐
│ sqlite wal mode concurrency --low                                      │
└────────────────────────────────────────────────────────────────────────┘
 (◉ᴗ◉) · gemma-4-e4b · read minicpm5-2b · docker in WSL · saved 17k · 2:41
```

The face on the status bar follows the stage in flight — searching, reading,
arguing, writing — so a long run looks alive rather than hung.

| | |
|---|---|
| `<topic>` or `<brief.md>` | start a run, `--low --medium --high` to set effort |
| `/models` | pick the reading model and the report model |
| `/runs` | past runs; press 1-9 to replay one |
| `/watch` | replay the last run into the transcript |
| `/status` | what ltms can see on this machine |
| `/template` | print a brief skeleton |
| `/stop` | stop the SearXNG container |
| `/clear` `/help` `/quit` | |

Inside `/models`: `ctrl+s` saves, `esc` cancels. Inside `/runs`: `1`-`9` replays
a run, `esc` closes.

`/models` lists every chat model on every local server it can find — LM Studio,
Ollama, llama.cpp, vLLM — and the two roles can live on different servers: the
big one loaded in LM Studio, a small fast one in Ollama. Embedding and reranker
models are filtered out, because picking one produces a baffling failure.

The same console opens by itself when an agent starts a run, attached to that
run. It is a separate read-only process: if it fails to open, or you close it,
the research carries on. `ltms watch` is the smaller non-interactive view.

## Status

Early but complete end to end: a brief goes in, a sourced report comes out.
Rough edges are in model handling rather than the pipeline — see the notes on
picking models below.

## Requirements

- Python 3.10+
- **A local model server.** [LM Studio](https://lmstudio.ai) is the easiest:
  install it, download a model, and switch the local server on from the
  Developer tab. [Ollama](https://ollama.com), llama.cpp and vLLM work too.
- **Docker** — only for SearXNG, which ltms starts and stops for you. On
  Windows no desktop app is needed: ltms drives Docker Engine inside WSL2, and
  the installer sets that up.

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
(plus the SearXNG image), optionally LM Studio — then runs `ltms init`. All of
it goes into `%USERPROFILE%\ltms`, and `~\ltms\bin` is added to your PATH.
Re-running it is safe; every step checks first.

**By hand, anywhere:**

```bash
uv venv ~/ltms/app
uv pip install --python ~/ltms/app git+https://github.com/Samet1771/lesstokenmoresearch
~/ltms/app/bin/ltms init
```

Put `~/ltms/app/bin` (Windows: `~\ltms\app\Scripts`) on your PATH, or use
`uv tool install git+https://github.com/Samet1771/lesstokenmoresearch` if you
would rather uv managed it — the data folder is `~/ltms` either way.

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
2. Run: ltms <brief.md> --effort medium -o <where you want the report>
3. Read the report file — all of it, part of it, or none.
```

## Commands

| | |
|---|---|
| `ltms brief.md` | run research from a brief |
| `ltms "topic"` | quick one-off research |
| `ltms` | open the console |
| `ltms template` | print a brief skeleton |
| `ltms gui` | the console, same thing |
| `ltms init` | interactive setup from the terminal |
| `ltms watch [run]` | attach the plain dashboard to a run |
| `ltms runs` | list recent runs |
| `ltms status` | show config and what is reachable |
| `ltms stop` | stop the managed SearXNG container |

Options: `--effort low\|medium\|high`, `-o PATH`, `--read N`, `--json`, `--no-window`, `--quiet`

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
fast_provider = ""                    # optional: reading model on another server
fast_base_url = ""
parallel = 4

[ui]
open_window = true

# reports_dir = "~/research"   # default: ~/ltms/reports
```

SearXNG needs three non-default settings to be usable as an API — JSON output
on, rate limiter off, secret key set. `ltms` generates that config for you and
binds the container to `127.0.0.1` only.

### Search engines and rate limits

Public engines rate-limit by IP, and SearXNG answers 200 with an empty list
once they all refuse. Three things keep a run working through that:

- **Category.** `search.categories` defaults to `general,it`. The `it` engines
  — stackoverflow, github, mdn, docker hub, askubuntu — are better sources for
  technical research *and* do not throttle a single machine. Measured on one
  query: `general` alone returned 20 results with two engines blocked,
  `general,it` returned 99 with the same two blocked.
- **Breadth.** The generated settings enable mojeek, qwant, bing, mwmbl and
  crowdview alongside the stock three, so no single block empties a search.
  Same query, three engines blocked: 20 results before, 38 after.
- **Pace.** Queries go out two at a time with a stagger rather than all at
  once. The burst is what trips the CAPTCHA, and a run spends minutes reading
  pages — a few seconds spent searching politely costs nothing.

When engines do refuse, ltms names them and says so, rather than reporting
"no results" and sending you looking for a bug in your query.

On Windows the container runs inside WSL2 and ltms talks to it through
`wsl -d <distro> -u root -- docker`. WSL2 forwards localhost, so the published
port is reachable from Windows at the same address. The settings file is
streamed into the distro and copied into the container rather than bind-mounted
— host path translation is the least portable part of running containers, and a
Windows path means nothing inside WSL.

Leaving `name` blank is the normal way to use LM Studio: you pick the model in
the app, and ltms uses whatever is loaded.

`fast_name` matters more than it looks. Reading pages is bulk work where
throughput decides the length of a run; writing the report is a couple of calls
where quality shows. The two phases never overlap, so both models do not have
to fit in VRAM at once — ltms unloads the reader before the writer loads.

**ltms sets no response-length limit.** Your server already has one — in LM
Studio it is the model's max response tokens — and a second number chosen here
could only be the wrong one: too low truncates an answer mid-sentence, too high
does nothing. If a model runs out of room before it answers, raise the limit
where you set it.

**Avoid reasoning models for either role.** They spend their budget thinking
before they answer, and locally that is pure latency. The same brief, same six
pages, on one 16 GB machine:

| | 27B reasoning | 4B instruct |
|---|---|---|
| pages read | 4 | 5 |
| facts extracted | 8 | 19 |
| debate | 82s | 30s |
| report | 96s, unusable | 34s, 360 tokens |

The reasoning model spent 96% of its output thinking, then ran out of room
before writing anything. ltms detects this and says so rather than shipping a
transcript of deliberation as a report — but the fix is to pick a plain
instruct model.

Extractor agents run concurrently, so raise the server's own parallel-request
limit to match `parallel` — otherwise the requests queue and nothing is gained.

## No cloud

This project does not call hosted model APIs, by design. Local servers that
happen to speak the OpenAI protocol (LM Studio, llama.cpp, vLLM) are supported
because they run on your machine.

## License

MIT
