"""The research pipeline.

Phase 0 implements: plan -> search -> filter.
Phases still to come: read (fetch + per-page extraction), rank, debate, write.
Stages that are not implemented yet are emitted as `skip` so the dashboard shows
the whole shape of the run rather than pretending it ended early.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from . import docker_mgr
from .config import Config
from .llm import LocalModel, ModelError, estimate_tokens, parse_json_list
from .runs import RunWriter
from .search import SearxngBackend, dedupe_and_cap

PLANNER_SYSTEM = (
    "You turn a research topic into web search queries. "
    "Output a JSON array of strings and nothing else. "
    "Each query is 3-10 words, plain keywords, no boolean operators, no quotes. "
    "Cover: the core question, primary/official sources, concrete data, "
    "criticism and failure modes, and comparisons. "
    "Write queries in the language most likely used by the best sources on this topic."
)


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


def fallback_queries(topic: str, count: int) -> list[str]:
    """Used when no model server is reachable. Deliberately plain."""
    angles = [
        "",
        "documentation",
        "benchmark data",
        "problems limitations",
        "comparison alternatives",
        "best practices",
        "case study",
        "how it works internally",
        "official specification",
        "known issues",
        "performance tuning",
        "migration experience",
    ]
    seen: list[str] = []
    for angle in angles:
        query = f"{topic} {angle}".strip()
        if query not in seen:
            seen.append(query)
        if len(seen) >= count:
            break
    return seen


async def plan_queries(model: LocalModel, topic: str, count: int) -> tuple[list[str], str]:
    """Returns (queries, source) where source is 'model' or 'fallback'."""
    try:
        if not await model.available():
            return fallback_queries(topic, count), "fallback"
        reply = await model.chat(
            system=PLANNER_SYSTEM,
            user=f"Topic: {topic}\nProduce exactly {count} queries.",
            max_tokens=500,
            temperature=0.3,
        )
    except ModelError:
        return fallback_queries(topic, count), "fallback"

    queries = parse_json_list(reply.text)[:count]
    if len(queries) < 2:
        return fallback_queries(topic, count), "fallback"
    if topic.lower() not in {q.lower() for q in queries}:
        queries.insert(0, topic)
    return queries[:count], "model"


async def run(
    topic: str,
    effort: str,
    config: Config,
    writer: RunWriter,
    read_limit: int | None = None,
) -> dict:
    preset = EFFORTS[effort]
    read_target = read_limit or preset.read

    writer.meta(topic, effort, candidates=preset.candidates, read_target=read_target)
    for stage in ("plan", "search", "filter", "read", "rank", "debate", "write"):
        writer.stage(stage, "pending")

    model = LocalModel(config.model)

    # ------------------------------------------------------------------ plan
    writer.stage("plan", "run")
    queries, source = await plan_queries(model, topic, preset.queries)
    if source == "fallback":
        writer.note("no model server reachable — using keyword expansion for queries", "warn")
    writer.stage("plan", "ok", f"{len(queries)} queries · {source}")
    writer.write_json("queries.json", queries)

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
        "queries": len(queries),
        "candidates": len(outcome.results),
        "domains": domains,
        "sources_file": str(writer.dir / "sources.json"),
    }


def run_sync(topic: str, effort: str, config: Config, writer: RunWriter, read_limit: int | None = None) -> dict:
    return asyncio.run(run(topic, effort, config, writer, read_limit))
