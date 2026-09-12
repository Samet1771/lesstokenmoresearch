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
from dataclasses import dataclass

from . import docker_mgr
from .brief import Brief
from .config import Config
from .llm import estimate_tokens
from .runs import RunWriter
from .search import SearxngBackend, dedupe_and_cap


@dataclass(frozen=True)
class EffortPreset:
    candidates: int
    read: int
    roles: int
    queries: int


EFFORTS: dict[str, EffortPreset] = {
    "low": EffortPreset(candidates=25, read=10, roles=1, queries=4),
    "medium": EffortPreset(candidates=80, read=30, roles=2, queries=7),
    "high": EffortPreset(candidates=200, read=70, roles=3, queries=12),
}


async def run(
    brief: Brief,
    effort: str,
    config: Config,
    writer: RunWriter,
    read_limit: int | None = None,
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

    # ---------------------------------------------------------------- search
    writer.stage("search", "run")
    with docker_mgr.searxng(config, report=lambda text, level: writer.note(text, level)) as base_url:
        backend = SearxngBackend(base_url, concurrency=min(6, len(queries)))
        per_query = max(6, min(20, preset.candidates // max(1, len(queries)) + 4))

        done = 0
        writer.progress("search", 0, len(queries))

        def on_done(query: str, found: int, error: str | None) -> None:
            nonlocal done
            done += 1
            writer.progress("search", done, len(queries))
            if error:
                writer.note(f"{query[:48]}: {error[:60]}", "warn")

        results, warnings = await backend.search_many(queries, per_query, on_done=on_done)

    for warning in warnings[:3]:
        writer.note(warning, "warn")

    if not results:
        writer.stage("search", "fail", "no results")
        writer.end("failed", summary="search returned nothing")
        return {"status": "failed", "reason": "no search results"}

    domains = len({result.domain for result in results})
    writer.stage("search", "ok", f"{len(results)} hits · {domains} domains")

    # ---------------------------------------------------------------- filter
    writer.stage("filter", "run")
    outcome = dedupe_and_cap(results, limit=preset.candidates, per_domain=3)
    dropped = " · ".join(f"{count} {name}" for name, count in outcome.dropped.items())
    writer.stage("filter", "ok", f"{len(outcome.results)} kept" + (f"  ({dropped})" if dropped else ""))

    writer.write_json("sources.json", [result.to_dict() for result in outcome.results])

    snippet_tokens = sum(estimate_tokens(r.title + r.snippet) for r in outcome.results)
    writer.metric(candidates=len(outcome.results), tokens_saved=snippet_tokens)

    # -------------------------------------------------------- not yet built
    for stage in ("read", "rank", "debate", "write"):
        writer.stage(stage, "skip", "phase 1+")

    writer.note(f"sources written to {writer.dir / 'sources.json'}")
    summary = f"{len(outcome.results)} sources from {domains} domains"
    writer.end("ok", report=str(writer.dir / "sources.json"), summary=summary)

    return {
        "status": "ok",
        "run_id": writer.run_id,
        "topic": brief.topic,
        "queries": len(queries),
        "questions": len(brief.questions),
        "candidates": len(outcome.results),
        "domains": domains,
        "sources_file": str(writer.dir / "sources.json"),
    }


def run_sync(brief: Brief, effort: str, config: Config, writer: RunWriter, read_limit: int | None = None) -> dict:
    return asyncio.run(run(brief, effort, config, writer, read_limit))
