"""Tests for the reading and reporting stages, minus the model calls."""

import unittest

from ltms.brief import parse
from ltms.extract import (
    CONTEXT_REFUSAL, Extract, _first_json_object, build_prompt, parse_extract, rank,
    saved_tokens, thinking_share, trim,
)
from ltms.fetch import FetchStats, Page, html_to_text, tidy
from ltms.report import ROLES, numbered_evidence, fallback_report


def page(text: str = "x" * 900, **kwargs) -> Page:
    return Page(url=kwargs.pop("url", "https://example.com/a"), text=text, chars=len(text), **kwargs)


def extract(**kwargs) -> Extract:
    base = dict(url="https://example.com/a", title="A", relevance=0.5, facts=["a fact that is long enough"])
    base.update(kwargs)
    return Extract(**base)


class Tidy(unittest.TestCase):
    def test_collapses_whitespace_and_blank_runs(self):
        self.assertEqual(tidy("a  \t b\r\n\n\n\nc  "), "a b\n\nc")


class HtmlText(unittest.TestCase):
    HTML = """<html><head><title>Real Title</title></head><body>
        <nav>menu menu menu</nav>
        <article><p>SQLite WAL allows one writer at a time.</p>
        <p>Readers do not block the writer.</p></article>
        <script>var tracking = 1;</script></body></html>"""

    def test_pulls_out_the_prose(self):
        _, text = html_to_text(self.HTML, "https://example.com")
        self.assertIn("one writer at a time", text)

    def test_leaves_out_scripts(self):
        _, text = html_to_text(self.HTML, "https://example.com")
        self.assertNotIn("var tracking", text)


class PageUsability(unittest.TestCase):
    def test_a_short_page_is_not_usable(self):
        self.assertFalse(page("too short").usable)

    def test_an_errored_page_is_not_usable(self):
        self.assertFalse(page(error="http 404").usable)

    def test_stats_group_failures(self):
        stats = FetchStats()
        for item in (page(), page(error="http 404"), page(error="http 500"), page("short")):
            stats.record(item)
        self.assertEqual(stats.fetched, 4)
        self.assertEqual(stats.usable, 1)
        self.assertEqual(stats.failures["http 404"], 1)


class Trim(unittest.TestCase):
    def test_keeps_both_ends(self):
        text = "START" + ("m" * 5000) + "END"
        trimmed = trim(text, limit=1000)
        self.assertTrue(trimmed.startswith("START"))
        self.assertTrue(trimmed.endswith("END"))
        self.assertLess(len(trimmed), 1100)

    def test_short_text_untouched(self):
        self.assertEqual(trim("short", limit=1000), "short")


class JsonSalvage(unittest.TestCase):
    def test_finds_object_inside_prose(self):
        self.assertEqual(_first_json_object('thinking...\n{"a": 1}\nthanks'), {"a": 1})

    def test_handles_nesting_and_braces_in_strings(self):
        found = _first_json_object('{"facts": ["uses {braces} inside"], "n": {"deep": 2}}')
        self.assertEqual(found["n"]["deep"], 2)

    def test_skips_a_broken_object_and_finds_the_next(self):
        self.assertEqual(_first_json_object('{bad json} then {"ok": true}'), {"ok": True})

    def test_returns_none_when_absent(self):
        self.assertIsNone(_first_json_object("no object here"))


class ParseExtract(unittest.TestCase):
    def test_reads_a_well_formed_reply(self):
        reply = ('{"relevance": 0.8, "kind": "docs", "date": "2010-07-21", '
                 '"facts": ["WAL arrived in SQLite 3.7.0"], "quotes": []}')
        result = parse_extract(reply, page())
        self.assertEqual((result.relevance, result.kind, result.date), (0.8, "docs", "2010-07-21"))
        self.assertTrue(result.usable)

    def test_clamps_relevance_and_rejects_odd_fields(self):
        result = parse_extract('{"relevance": 7, "kind": "nonsense", "date": "last tuesday", "facts": ["a real fact here"]}', page())
        self.assertEqual((result.relevance, result.kind, result.date), (1.0, "other", ""))

    def test_drops_fragments_too_short_to_be_facts(self):
        result = parse_extract('{"relevance": 0.5, "facts": ["ok", "a proper fact sentence"]}', page())
        self.assertEqual(len(result.facts), 1)

    def test_reply_without_json_is_an_error_not_a_crash(self):
        result = parse_extract("I think the page says a few things.", page())
        self.assertFalse(result.usable)
        self.assertIn("no JSON", result.error)

    def test_no_facts_means_not_usable(self):
        self.assertFalse(parse_extract('{"relevance": 0.9, "facts": []}', page()).usable)


class ContextRefusal(unittest.TestCase):
    """A page that overflows a parallel slot gets one shorter retry."""

    def test_recognises_the_shapes_servers_use(self):
        for message in [
            'model server 400: {"error":"Engine protocol predict stream returned an error"}',
            "context length exceeded",
            "prompt is too long for this model",
            "max_position_embeddings reached",
        ]:
            self.assertTrue(CONTEXT_REFUSAL.search(message), message)

    def test_leaves_unrelated_failures_alone(self):
        for message in ["cannot reach model server at http://x", "no chat model available"]:
            self.assertIsNone(CONTEXT_REFUSAL.search(message), message)

    def test_the_shorter_prompt_is_actually_shorter(self):
        long_page = page("sentence about wal mode. " * 4000)
        full = build_prompt("topic", "", long_page)
        short = build_prompt("topic", "", long_page, 24000 // 3)
        self.assertLess(len(short), len(full) // 2)


class Ranking(unittest.TestCase):
    def test_orders_by_relevance(self):
        ordered = rank([extract(relevance=0.2, url="u1"), extract(relevance=0.9, url="u2")])
        self.assertEqual(ordered[0].url, "u2")

    def test_official_docs_beat_marketing_at_equal_relevance(self):
        ordered = rank([extract(kind="marketing", url="ad"), extract(kind="docs", url="doc")])
        self.assertEqual(ordered[0].url, "doc")

    def test_unusable_extracts_are_dropped(self):
        ordered = rank([extract(), Extract(url="broken", error="http 500")])
        self.assertEqual(len(ordered), 1)

    def test_keep_limits_the_list(self):
        self.assertEqual(len(rank([extract(url=f"u{i}") for i in range(9)], keep=3)), 3)


class TokenAccounting(unittest.TestCase):
    def test_saving_is_pages_read_minus_facts_kept(self):
        saved = saved_tokens([page("word " * 4000)], [extract()])
        self.assertGreater(saved, 1000)

    def test_thinking_share_flags_a_reasoning_model(self):
        share = thinking_share([extract(reasoning_tokens=1000, facts=["a short fact here"])])
        self.assertGreater(share, 0.8)

    def test_thinking_share_is_zero_without_reasoning(self):
        self.assertEqual(thinking_share([extract(reasoning_tokens=0)]), 0.0)

    def test_thinking_share_survives_an_empty_run(self):
        self.assertEqual(thinking_share([]), 0.0)


class Evidence(unittest.TestCase):
    def test_numbers_every_source(self):
        text = numbered_evidence([extract(url="https://a.com/1"), extract(url="https://b.com/2")])
        self.assertIn("[1]", text)
        self.assertIn("[2]", text)
        self.assertIn("https://b.com/2", text)

    def test_fallback_report_still_delivers_facts_and_sources(self):
        brief = parse("# topic here\n\n## queries\n- a search\n")
        text = fallback_report(brief, [extract(facts=["a fact worth keeping"])])
        self.assertIn("a fact worth keeping", text)
        self.assertIn("## sources", text)

    def test_roles_are_distinct(self):
        self.assertEqual(len({role.id for role in ROLES}), len(ROLES))


if __name__ == "__main__":
    unittest.main()


class ReportDestination(unittest.TestCase):
    """Where a report lands depends on who asked for it."""

    def test_an_agent_gets_exactly_the_path_it_named(self):
        import tempfile
        from pathlib import Path

        from ltms.pipeline import publish

        with tempfile.TemporaryDirectory() as directory:
            wanted = Path(directory) / "notes" / "wal-research.md"
            written = publish("# report", wanted)
            self.assertEqual(written, wanted)
            self.assertEqual(written.read_text(encoding="utf-8"), "# report")

    def test_a_second_run_sits_beside_the_first(self):
        import tempfile
        from pathlib import Path

        from ltms.pipeline import publish

        with tempfile.TemporaryDirectory() as directory:
            wanted = Path(directory) / "sqlite-wal.md"
            first = publish("one", wanted)
            second = publish("two", wanted)
            self.assertEqual(first.name, "sqlite-wal.md")
            self.assertEqual(second.name, "sqlite-wal-2.md")
            self.assertEqual(first.read_text(encoding="utf-8"), "one")

    def test_an_empty_file_is_not_treated_as_a_neighbour(self):
        import tempfile
        from pathlib import Path

        from ltms.pipeline import publish

        with tempfile.TemporaryDirectory() as directory:
            wanted = Path(directory) / "x.md"
            wanted.touch()
            self.assertEqual(publish("body", wanted).name, "x.md")

    def test_a_person_gets_a_reports_folder_in_the_ltms_home(self):
        from ltms.config import Config, config_dir

        self.assertEqual(Config().reports_path, config_dir() / "reports")

    def test_the_reports_folder_can_be_moved(self):
        from ltms.config import Config

        self.assertEqual(str(Config(reports_dir="/tmp/elsewhere").reports_path).replace("\\", "/"),
                         "/tmp/elsewhere")
