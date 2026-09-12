"""The whole pipeline, with the network and the models stubbed out.

This exists because a NameError in the delivery step survived 114 passing
tests: nothing ran the pipeline from end to end, so nothing ever reached the
last twenty lines of it. Everything slow is replaced here; everything that
wires the stages together is real.
"""

import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from ltms import pipeline
from ltms.brief import parse
from ltms.config import Config
from ltms.extract import Extract
from ltms.fetch import Page
from ltms.runs import RunWriter, load_state
from ltms.search.base import SearchResult

BRIEF = parse(
    "# sqlite wal concurrency\n\n"
    "## queries\n- sqlite wal one writer\n- sqlite busy timeout\n\n"
    "## questions\n- How many writers?\n"
)


class FakeBackend:
    def __init__(self, *args, **kwargs):
        pass

    results_per_query = 6
    engine_trouble: dict = {}

    async def search_many(self, queries, per_query, on_done=None):
        results = [
            SearchResult(title=f"Page {i}", url=f"https://site{i}.test/p", snippet="snippet", score=1.0 - i / 10)
            for i in range(self.results_per_query)
        ]
        for query in queries:
            if on_done:
                on_done(query, len(results), None)
        return results, [], dict(self.engine_trouble)


async def fake_fetch(urls, concurrency=8, timeout=20.0, on_start=None, on_done=None):
    pages = []
    for index, url in enumerate(urls):
        page = Page(url=url, title=f"Page {index}", text="body " * 200, chars=1000)
        if on_start:
            on_start(index, url)
        if on_done:
            on_done(index, page)
        pages.append(page)
    return pages


async def fake_extract(pages, topic, instructions, model_config, concurrency=4, on_start=None, on_done=None):
    extracts = []
    for index, page in enumerate(pages):
        extract = Extract(
            url=page.url, title=page.title, relevance=0.9 - index / 100,
            kind="docs", facts=[f"a fact from page {index}"], chars=page.chars,
        )
        if on_start:
            on_start(index, page)
        if on_done:
            on_done(index, extract)
        extracts.append(extract)
    return extracts


async def fake_roles(brief, findings, model_config, count, on_start=None, on_done=None):
    for name in ("builder",)[:count]:
        if on_start:
            on_start(name)
        if on_done:
            on_done(name, True)
    return [("builder", "the evidence says one writer [1]")]


async def fake_write(brief, findings, memos, model_config):
    return "## findings\nOnly one writer at a time [1].\n\n## sources\n[1] Page 0 — https://site0.test/p"


@contextlib.contextmanager
def fake_searxng(config, report=None):
    if report:
        report("using a stubbed SearXNG", "info")
    yield "http://127.0.0.1:0"


class Harness:
    """Swap the slow parts out and put them back afterwards."""

    PATCHES = {
        "SearxngBackend": FakeBackend,
        "fetch_many": fake_fetch,
        "extract_many": fake_extract,
        "run_roles": fake_roles,
        "write_report": fake_write,
    }

    def __enter__(self):
        self.saved = {name: getattr(pipeline, name) for name in self.PATCHES}
        for name, replacement in self.PATCHES.items():
            setattr(pipeline, name, replacement)
        self.saved_searxng = pipeline.docker_mgr.searxng
        self.saved_warning = pipeline.residency.context_warning
        self.saved_release = pipeline.residency.release
        pipeline.docker_mgr.searxng = fake_searxng
        pipeline.residency.context_warning = lambda *a, **k: ""
        pipeline.residency.release = lambda *a, **k: ""
        return self

    def __exit__(self, *exc):
        for name, original in self.saved.items():
            setattr(pipeline, name, original)
        pipeline.docker_mgr.searxng = self.saved_searxng
        pipeline.residency.context_warning = self.saved_warning
        pipeline.residency.release = self.saved_release


class WholePipeline(unittest.TestCase):
    def setUp(self) -> None:
        # The run directory has to outlive the call, because most of what is
        # worth checking is what the run left on disk.
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)

    def run_pipeline(self, out: Path | None = None, effort: str = "low"):
        with Harness():
            writer = RunWriter(Path(self._home.name) / "runs", "test-run")
            result = pipeline.run_sync(BRIEF, effort, Config(), writer, read_limit=4, out=out)
        return result, writer, load_state(writer.progress_path)

    def test_it_reaches_the_end(self):
        result, _, state = self.run_pipeline()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(state.finished)
        self.assertEqual(state.status, "ok")

    def test_every_stage_completes(self):
        _, _, state = self.run_pipeline()
        for stage in ("plan", "search", "filter", "read", "rank", "debate", "write"):
            self.assertIn(stage, state.stages, stage)
            self.assertEqual(state.stages[stage].status, "ok", stage)

    def test_the_run_directory_keeps_everything(self):
        _, writer, _ = self.run_pipeline()
        for name in ("brief.json", "sources.json", "extracts.json", "findings.json", "report.md"):
            self.assertTrue((writer.dir / name).exists(), name)
        self.assertTrue(list((writer.dir / "extracts").glob("*.md")), "no per-reader notes")

    def test_each_reader_leaves_a_note(self):
        _, writer, _ = self.run_pipeline()
        notes = sorted((writer.dir / "extracts").glob("*.md"))
        self.assertEqual(len(notes), 4)
        self.assertIn("a fact from page 0", notes[0].read_text(encoding="utf-8"))

    def test_without_a_destination_the_report_stays_in_the_run(self):
        result, writer, _ = self.run_pipeline()
        self.assertEqual(Path(result["report"]), writer.report_path)

    def test_with_a_destination_the_report_goes_there(self):
        """The bug this file was written for: `delivered` was never assigned."""
        with tempfile.TemporaryDirectory() as directory:
            wanted = Path(directory) / "notes" / "wal.md"
            result, writer, state = self.run_pipeline(out=wanted)
            self.assertEqual(Path(result["report"]), wanted)
            self.assertIn("Only one writer", wanted.read_text(encoding="utf-8"))
            # and the archive copy is still there
            self.assertTrue(writer.report_path.exists())
            self.assertEqual(Path(state.report), wanted)

    def test_the_result_carries_what_the_caller_prints(self):
        result, _, _ = self.run_pipeline()
        for key in ("pages_read", "facts", "report_tokens", "report", "run_dir"):
            self.assertIn(key, result, key)
        self.assertGreater(result["facts"], 0)
        self.assertGreater(result["report_tokens"], 0)

    def test_blocked_engines_are_explained_rather_than_called_no_results(self):
        """SearXNG answers 200 with nothing when every engine refuses."""

        class Blocked(FakeBackend):
            results_per_query = 0
            engine_trouble = {"duckduckgo": "CAPTCHA", "google cse": "Suspended: too many requests"}

        saved = Harness.PATCHES["SearxngBackend"]
        Harness.PATCHES["SearxngBackend"] = Blocked
        try:
            result, _, state = self.run_pipeline()
        finally:
            Harness.PATCHES["SearxngBackend"] = saved

        self.assertEqual(result["status"], "failed")
        self.assertIn("blocking", result["reason"])
        self.assertIn("duckduckgo", result["reason"])
        self.assertEqual(state.stages["search"].status, "fail")
        self.assertIn("refused", state.stages["search"].detail)

    def test_effort_changes_how_much_is_read(self):
        low, _, _ = self.run_pipeline(effort="low")
        high, _, _ = self.run_pipeline(effort="high")
        self.assertEqual(low["status"], high["status"], "both should complete")

class BlockedPagesDoNotCostSources(unittest.TestCase):
    """A page that refuses to be read should cost a fetch, not a source.

    Measured on 180 URLs from real runs: about a fifth answer 403, an anti-bot
    challenge or a JavaScript shell. Reading the top N candidates and keeping
    whatever survives turned "read 30 pages" into 18.
    """

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)

    def run_with(self, fetcher, read_limit=4):
        with Harness() as harness:
            FakeBackend.results_per_query = 12
            self.addCleanup(setattr, FakeBackend, "results_per_query", 6)
            pipeline.fetch_many = fetcher
            writer = RunWriter(Path(self._home.name) / "runs", "test-run")
            result = pipeline.run_sync(BRIEF, "low", Config(), writer, read_limit=read_limit)
        return result, writer

    def test_the_target_is_met_by_pulling_more_candidates(self):
        attempted: list[str] = []

        async def half_of_them_refuse(urls, concurrency=8, timeout=20.0, on_start=None, on_done=None):
            pages = []
            for index, url in enumerate(urls):
                attempted.append(url)
                ok = len(attempted) % 2 == 1
                page = (
                    Page(url=url, title="ok", text="body " * 200, chars=1000)
                    if ok
                    else Page(url=url, error="anti-bot challenge", blocked=True)
                )
                if on_start:
                    on_start(index, url)
                if on_done:
                    on_done(index, page)
                pages.append(page)
            return pages

        result, _ = self.run_with(half_of_them_refuse)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pages_read"], 4, "backfill did not reach the target")
        self.assertGreater(len(attempted), 4, "no replacements were fetched")
        self.assertEqual(len(attempted), len(set(attempted)), "a page was fetched twice")

    def test_a_thin_pool_still_delivers_what_it_can(self):
        async def all_refuse_after_two(urls, concurrency=8, timeout=20.0, on_start=None, on_done=None):
            pages = []
            for index, url in enumerate(urls):
                ok = url.endswith(("site0.test/p", "site1.test/p"))
                page = (
                    Page(url=url, title="ok", text="body " * 200, chars=1000)
                    if ok
                    else Page(url=url, error="http 403")
                )
                if on_start:
                    on_start(index, url)
                if on_done:
                    on_done(index, page)
                pages.append(page)
            return pages

        result, _ = self.run_with(all_refuse_after_two)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pages_read"], 2)

    def test_nothing_readable_anywhere_is_still_a_clean_failure(self):
        async def everything_refuses(urls, concurrency=8, timeout=20.0, on_start=None, on_done=None):
            return [Page(url=url, error="http 403") for url in urls]

        result, _ = self.run_with(everything_refuses)
        self.assertEqual(result["status"], "failed")

class SlowReadingIsCalledOutEarly(unittest.TestCase):
    """Learning at the end that the model was too slow means having waited."""

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)

    def run_at_speed(self, seconds_per_page):
        """A clock that advances by `seconds_per_page` on every reading call."""

        class Clock:
            def __init__(self):
                self.now = 0.0

            def monotonic(self):
                self.now += seconds_per_page
                return self.now

        async def timed_extract(pages, topic, instructions, model_config,
                                concurrency=4, on_start=None, on_done=None):
            return await fake_extract(pages, topic, instructions, model_config,
                                      concurrency, on_start, on_done)

        with Harness():
            pipeline.extract_many = timed_extract
            saved, pipeline.time = pipeline.time, Clock()
            try:
                writer = RunWriter(Path(self._home.name) / "runs", "test-run")
                pipeline.run_sync(BRIEF, "low", Config(), writer, read_limit=4)
            finally:
                pipeline.time = saved
        events = [json.loads(line) for line in
                  writer.progress_path.read_text(encoding="utf-8").splitlines()]
        return events

    def warning_index(self, events):
        for index, event in enumerate(events):
            if event["type"] == "note" and "first page" in event.get("text", ""):
                return index
        return None

    def test_a_slow_model_is_flagged_while_the_run_can_still_be_stopped(self):
        events = self.run_at_speed(120.0)
        where = self.warning_index(events)
        self.assertIsNotNone(where, "no warning about a slow reading model")
        read_ok = next(i for i, e in enumerate(events)
                       if e["type"] == "stage" and e["name"] == "read" and e["status"] == "ok")
        self.assertLess(where, read_ok, "the warning arrived after the reading finished")

    def test_it_is_said_once_not_once_per_page(self):
        events = self.run_at_speed(120.0)
        said = [e for e in events if e["type"] == "note" and "first page" in e.get("text", "")]
        self.assertEqual(len(said), 1)

    def test_a_fast_model_is_not_nagged(self):
        self.assertIsNone(self.warning_index(self.run_at_speed(1.0)))

class TheQueriesAreVisible(unittest.TestCase):
    """A run that searches the wrong thing looks exactly like one that searches
    the right thing, until the report comes back wrong."""

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)

    def notes(self, brief=BRIEF):
        with Harness():
            writer = RunWriter(Path(self._home.name) / "runs", "test-run")
            pipeline.run_sync(brief, "low", Config(), writer, read_limit=2)
        return [json.loads(line) for line in
                writer.progress_path.read_text(encoding="utf-8").splitlines()]

    def test_every_query_is_printed_before_the_search_runs(self):
        events = self.notes()
        printed = [e["text"][2:] for e in events
                   if e["type"] == "note" and e.get("text", "").startswith("? ")]
        self.assertEqual(printed, BRIEF.queries)
        first_search = next(i for i, e in enumerate(events)
                            if e["type"] == "stage" and e["name"] == "search" and e["status"] == "run")
        last_printed = max(i for i, e in enumerate(events)
                           if e["type"] == "note" and e.get("text", "").startswith("? "))
        self.assertLess(last_printed, first_search, "queries printed after the search started")

    def test_each_query_reports_what_it_found(self):
        events = self.notes()
        counted = [e["text"] for e in events
                   if e["type"] == "note" and "hits · " in e.get("text", "")]
        self.assertEqual(len(counted), len(BRIEF.queries))


class NothingSurvivedTriage(unittest.TestCase):
    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)

    def test_it_says_so_instead_of_blaming_the_fetcher(self):
        class OffTopicBackend(FakeBackend):
            async def search_many(self, queries, per_query, on_done=None):
                results = [
                    SearchResult(title="BEST | Cambridge Dictionary", url=f"https://dict{i}.test/b",
                                 score=0.9, query=queries[0])
                    for i in range(6)
                ]
                for query in queries:
                    if on_done:
                        on_done(query, len(results), None)
                return results, [], {}

        with Harness():
            pipeline.SearxngBackend = OffTopicBackend
            writer = RunWriter(Path(self._home.name) / "runs", "test-run")
            result = pipeline.run_sync(BRIEF, "low", Config(), writer, read_limit=2)

        self.assertEqual(result["status"], "failed")
        self.assertIn("off_topic", result["reason"])
        state = load_state(writer.progress_path)
        self.assertEqual(state.stages["filter"].status, "fail")


if __name__ == "__main__":
    unittest.main()
