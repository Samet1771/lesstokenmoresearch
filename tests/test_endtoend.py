"""The whole pipeline, with the network and the models stubbed out.

This exists because a NameError in the delivery step survived 114 passing
tests: nothing ran the pipeline from end to end, so nothing ever reached the
last twenty lines of it. Everything slow is replaced here; everything that
wires the stages together is real.
"""

import contextlib
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


if __name__ == "__main__":
    unittest.main()
