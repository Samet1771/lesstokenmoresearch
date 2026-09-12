"""The research pipeline.

Phase 0 implements: plan -> search -> filter.
Phases still to come: read (fetch + per-page extraction), rank, debate, write.
Stages that are not implemented yet are emitted as `skip` so the dashboard shows
the whole shape of the run rather than pretending it ended early.

There is no query-planning model. The caller writes the queries -- see brief.py
for why that is better than asking a small local model to guess them.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from . import docker_mgr
from .brief import Brief
from .config import Config
from .extract import EXTRACT_HEADROOM, Extract, extract_many, rank, saved_tokens
from .fetch import FetchStats, Page, fetch_many
from .llm import estimate_tokens
from .report import fallback_report, header, run_roles, write_report
from . import residency
from .runs import RunWriter
from .search import SearxngBackend, categories_for, dedupe_and_cap


@dataclass(frozen=True)
class EffortPreset:
    candidates: int
    read: int
    roles: int
    queries: int


# Reading is the long pole: everything else is a handful of calls, this is one
# per page. Measured on ten pages, a 2B model took 4 seconds each and a 27B one
# never finished a run anyone was willing to wait for. Past this, say so while
# there is still time to stop.
SLOW_PAGE_SECONDS = 30.0

EFFORTS: dict[str, EffortPreset] = {
    "low": EffortPreset(candidates=25, read=10, roles=1, queries=4),
    "medium": EffortPreset(candidates=80, read=30, roles=2, queries=7),
    "high": EffortPreset(candidates=200, read=70, roles=3, queries=12),
}


def publish(body: str, destination: Path) -> Path:
    """Put the report where it was asked for, without overwriting a neighbour.

    An agent names its own file and expects exactly that path. A person gets a
    readable name in their reports folder, and a second run on the same topic
    should sit beside the first rather than replace it.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        stem, suffix = destination.stem, destination.suffix or ".md"
        for number in range(2, 100):
            candidate = destination.with_name(f"{stem}-{number}{suffix}")
            if not candidate.exists():
                destination = candidate
                break
    destination.write_text(body, encoding="utf-8", newline="\n")
    return destination


async def run(
    brief: Brief,
    effort: str,
    config: Config,
    writer: RunWriter,
    read_limit: int | None = None,
    out: Path | None = None,
) -> dict:
    preset = EFFORTS[effort]
    read_target = read_limit or preset.read
    queries = brief.queries[:30]

    writer.meta(
        brief.topic,
        effort,
        candidates=preset.candidates,
        read_target=read_target,
        brief=str(brief.path) if brief.path else "",
    )
    for stage in ("plan", "search", "filter", "read", "rank", "debate", "write"):
        writer.stage(stage, "pending")

    # ------------------------------------------------------------------ plan
    writer.stage("plan", "run")
    origin = "brief" if brief.path else "topic"
    detail = f"{len(queries)} queries · {origin}"
    if brief.questions:
        detail += f" · {len(brief.questions)} questions"
    writer.stage("plan", "ok", detail)
    writer.write_json(
        "brief.json",
        {
            "topic": brief.topic,
            "queries": queries,
            "questions": brief.questions,
            "notes": brief.notes,
            "source": str(brief.path) if brief.path else "",
        },
    )
    if not brief.path:
        writer.note("no brief file — queries expanded from the topic; a brief finds better sources", "warn")

    # Print them. A run that searches the wrong thing looks exactly like a run
    # that searches the right thing until the report comes back wrong, and the
    # queries are the one place where that is visible up front.
    for query in queries:
        writer.note(f"? {query}")

    # ---------------------------------------------------------------- search
    writer.stage("search", "run")
    categories = categories_for(queries, config.search.categories)
    if categories != config.search.categories:
        writer.note("nothing technical in these queries — not asking the developer engines")
    with docker_mgr.searxng(config, report=lambda text, level: writer.note(text, level)) as base_url:
        backend = SearxngBackend(base_url, concurrency=2, categories=categories)
        per_query = max(6, min(20, preset.candidates // max(1, len(queries)) + 4))

        done = 0
        writer.progress("search", 0, len(queries))

        def on_done(query: str, found: int, error: str | None) -> None:
            nonlocal done
            done += 1
            writer.progress("search", done, len(queries))
            if error:
                writer.note(f"{query[:48]}: {error[:60]}", "warn")
            else:
                writer.note(f"{found:3d} hits · {query}")

        results, warnings, engine_trouble = await backend.search_many(queries, per_query, on_done=on_done)

    for warning in warnings[:3]:
        writer.note(warning, "warn")

    # SearXNG answers 200 with nothing when every engine it tried was blocked.
    # Saying "no results" there sends people hunting for a bug in their query.
    blocked = " · ".join(f"{name}: {reason}" for name, reason in list(engine_trouble.items())[:4])
    if engine_trouble:
        writer.note(f"search engines refusing — {blocked}", "warn" if results else "error")

    if not results:
        detail = "every engine refused" if engine_trouble else "no results"
        writer.stage("search", "fail", detail)
        reason = (
            f"search engines are blocking this instance ({blocked}). "
            "They rate-limit by IP; wait a few minutes, or enable more engines in "
            "the generated searxng/settings.yml."
            if engine_trouble
            else "search returned nothing for these queries"
        )
        writer.end("failed", summary=detail)
        return {"status": "failed", "reason": reason}

    domains = len({result.domain for result in results})
    writer.stage("search", "ok", f"{len(results)} hits · {domains} domains")

    # ---------------------------------------------------------------- filter
    writer.stage("filter", "run")
    outcome = dedupe_and_cap(results, limit=preset.candidates, per_domain=3)
    dropped = " · ".join(f"{count} {name}" for name, count in outcome.dropped.items())
    writer.stage("filter", "ok", f"{len(outcome.results)} kept" + (f"  ({dropped})" if dropped else ""))

    # Everything found, nothing kept. Without this the run limps on to fetch an
    # empty shortlist and reports "no readable pages", which describes neither
    # what happened nor what to do about it.
    if not outcome.results:
        why = " · ".join(f"{count} {name}" for name, count in outcome.dropped.items())
        writer.stage("filter", "fail", f"nothing survived triage ({why})")
        writer.end("failed", summary="every result was filtered out")
        return {
            "status": "failed",
            "reason": (
                f"the search found {len(results)} results and none of them survived triage "
                f"({why}). off_topic means the engines answered a different question; "
                "noise means the sources were sites nothing can read, like reddit or "
                "instagram. Try wording the queries the way the pages you want are written."
            ),
        }

    writer.write_json("sources.json", [result.to_dict() for result in outcome.results])

    snippet_tokens = sum(estimate_tokens(r.title + r.snippet) for r in outcome.results)
    writer.metric(candidates=len(outcome.results), tokens_saved=snippet_tokens)

    # ------------------------------------------------------------------ read
    writer.stage("read", "run")
    writer.progress("read", 0, read_target * 2)

    readers = max(1, config.model.parallel)
    stats = FetchStats()
    fetched = 0

    def fetch_started(index: int, url: str) -> None:
        writer.agent(f"scout-{index % readers + 1}", "fetch", url)

    def fetch_finished(index: int, page: Page) -> None:
        nonlocal fetched
        fetched += 1
        stats.record(page)
        writer.progress("read", min(fetched, read_target), read_target * 2)

    # Roughly a fifth of the web refuses an automated reader -- 403s, anti-bot
    # challenges, paywalls, pages that are a JavaScript shell. Taking the top
    # `read_target` candidates and accepting whatever survives means a run asked
    # for 30 pages and read 18. The run does not care which pages it reads, only
    # that it reads enough of them, so keep pulling from the candidate pool
    # until the target is met or the pool is exhausted.
    pages: list[Page] = []
    by_url = {source.url: source for source in outcome.results}
    attempted = 0
    ceiling = min(len(outcome.results), read_target * 3)
    while len(pages) < read_target and attempted < ceiling:
        wave = outcome.results[attempted : attempted + (read_target - len(pages))]
        if not wave:
            break
        attempted += len(wave)
        found = await fetch_many(
            [source.url for source in wave],
            concurrency=min(8, max(4, readers * 2)),
            on_start=fetch_started,
            on_done=fetch_finished,
        )
        for page in found:
            source = by_url.get(page.url)
            if source and not page.title:
                page.title = source.title
            if page.usable:
                pages.append(page)

    trouble = " · ".join(f"{count} {name}" for name, count in stats.failures.items())
    writer.note(f"fetched {stats.fetched} pages, {stats.usable} readable" + (f" ({trouble})" if trouble else ""))
    if stats.fetched > len(pages) and pages:
        writer.note(f"replaced {stats.fetched - len(pages)} unreadable pages from the candidate pool")

    if stats.usable == 0:
        writer.stage("read", "fail", "nothing readable")
        writer.end("failed", summary="every page failed to fetch")
        return {"status": "failed", "reason": "no readable pages"}

    read_done = 0
    projected = False
    reading_started = time.monotonic()

    def read_started(index: int, page: Page) -> None:
        writer.agent(f"scout-{index % readers + 1}", "read", page.url)

    def read_finished(index: int, extract: Extract) -> None:
        nonlocal read_done, projected
        read_done += 1
        # The reader's whole output, on disk, before it goes away.
        writer.write_note(index, extract.url, extract.to_markdown())
        writer.agent(f"scout-{index % readers + 1}", "idle", "", extract.relevance if extract.usable else None)
        writer.progress("read", read_target + read_done, read_target * 2)

        # Say it now, not in the summary. Learning at the end that the model
        # was too slow means having already waited; the whole point of saying
        # anything is that there is still time to stop and pick another one.
        if projected:
            return
        projected = True
        elapsed = time.monotonic() - reading_started
        rate = elapsed / max(1, min(read_done, readers))
        if rate > SLOW_PAGE_SECONDS:
            minutes = rate * len(pages) / max(1, readers) / 60
            writer.note(
                f"{rate:.0f}s for the first page — these {len(pages)} will take about "
                f"{minutes:.0f} min. A smaller model for the reading role is usually "
                "the difference between minutes and an hour.",
                "warn",
            )

    warning = residency.context_warning(config.model.for_role("fast"), EXTRACT_HEADROOM + 7000)
    if warning:
        writer.note(warning, "warn")

    extracts = await extract_many(
        pages,
        topic=brief.topic,
        instructions=brief.instructions,
        model_config=config.model.for_role("fast"),
        concurrency=readers,
        on_start=read_started,
        on_done=read_finished,
    )

    for agent in range(1, readers + 1):
        writer.agent(f"scout-{agent}", "done", "")

    failed = [extract for extract in extracts if extract.error]
    for extract in failed[:2]:
        writer.note(f"read failed: {extract.error}", "warn")

    usable = [extract for extract in extracts if extract.usable]
    if not usable:
        writer.stage("read", "fail", f"{len(extracts)} pages, none produced facts")
        writer.end("failed", summary="the reading model returned nothing usable")
        return {"status": "failed", "reason": "extraction produced nothing"}


    facts = sum(len(extract.facts) for extract in usable)
    writer.stage("read", "ok", f"{len(usable)} pages read · {facts} facts")
    writer.write_json("extracts.json", [extract.to_dict() for extract in extracts])

    # ------------------------------------------------------------------ rank
    writer.stage("rank", "run")
    ordered = rank(extracts)
    top = ordered[0].relevance if ordered else 0.0
    writer.stage("rank", "ok", f"{len(ordered)} kept · best {top:.1f}")
    writer.metric(tokens_saved=snippet_tokens + saved_tokens(pages, extracts), facts=facts)

    writer.write_json("findings.json", [extract.to_dict() for extract in ordered])

    # ---------------------------------------------------------------- debate
    # Everything below runs on the report model. On one GPU that usually means
    # the server swaps models here -- once, because reading is finished.
    report_model = config.model.for_role("report")

    # Reading is over. Evict the reader before the writer loads, or both sit in
    # VRAM at once and the larger one spills into system memory.
    freed = residency.release(config.model.for_role("fast"), report_model)
    if freed:
        writer.note(freed)

    writer.stage("debate", "run")
    writer.progress("debate", 0, preset.roles)
    roles_done = 0

    def role_started(role_id: str) -> None:
        writer.agent(role_id, "think", "weighing the evidence")

    def role_finished(role_id: str, ok: bool) -> None:
        nonlocal roles_done
        roles_done += 1
        writer.agent(role_id, "done" if ok else "fail", "")
        writer.progress("debate", roles_done, preset.roles)

    memos = await run_roles(
        brief, ordered, report_model, preset.roles, on_start=role_started, on_done=role_finished
    )
    if memos:
        writer.stage("debate", "ok", f"{len(memos)} roles")
    else:
        writer.stage("debate", "warn", "no analyst memos — writing from evidence alone")

    # ----------------------------------------------------------------- write
    writer.stage("write", "run")
    writer.agent("editor", "write", "report.md")
    try:
        body = await write_report(brief, ordered, memos, report_model)
    except Exception as error:  # noqa: BLE001 - the evidence is worth delivering regardless
        writer.note(f"editor failed: {type(error).__name__} — writing raw findings", "warn")
        body = ""

    if not body:
        body = fallback_report(brief, ordered)
        writer.stage("write", "warn", "raw findings (no report model)")
    else:
        body = header(brief, ordered, writer.run_id) + body
        writer.stage("write", "ok", f"{estimate_tokens(body)} tokens")
    writer.agent("editor", "done", "")

    # The run directory always keeps a copy: it is the archive, sitting next to
    # the evidence that produced it.
    writer.report_path.write_text(body, encoding="utf-8", newline="\n")
    delivered = publish(body, out) if out else writer.report_path
    if out:
        writer.note(f"report written to {delivered}")

    report_tokens = estimate_tokens(body)
    writer.metric(
        tokens_saved=snippet_tokens + saved_tokens(pages, extracts),
        report_tokens=report_tokens,
        facts=facts,
    )

    summary = f"{len(usable)} sources read · {facts} facts · {report_tokens} tokens"
    writer.end("ok", report=str(delivered), summary=summary)

    return {
        "status": "ok",
        "run_id": writer.run_id,
        "topic": brief.topic,
        "candidates": len(outcome.results),
        "domains": domains,
        "pages_read": len(usable),
        "facts": facts,
        "report_tokens": report_tokens,
        "report": str(delivered),
        "run_dir": str(writer.dir),
    }


def run_sync(
    brief: Brief,
    effort: str,
    config: Config,
    writer: RunWriter,
    read_limit: int | None = None,
    out: Path | None = None,
) -> dict:
    return asyncio.run(run(brief, effort, config, writer, read_limit, out))
