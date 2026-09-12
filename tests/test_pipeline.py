"""Tests for the reading and reporting stages, minus the model calls."""

import unittest

from ltms.brief import parse
from ltms.extract import (
    CONTEXT_REFUSAL, Extract, _first_json_object, build_prompt, parse_extract, rank,
    saved_tokens, thinking_share, trim,
)
import httpx

import ltms.fetch
from ltms.fetch import (
    MAX_RETRY_WAIT, MIN_USEFUL_CHARS, FetchStats, Page, feed_to_text,
    fetch_many, fetch_url_for, html_to_text, looks_like_feed, retry_delay, tidy,
)
from ltms.search.base import SearchResult, dedupe_and_cap
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

    # trafilatura goes for the article and ignores the rest, which is right
    # until it recognises nothing and hands back a page we already paid to
    # fetch as an empty string.
    UNRECOGNISED = (
        "<html><head><title>Odd Layout</title></head><body>"
        + "".join(f"<span>fact number {i} about sqlite wal journalling</span>" for i in range(40))
        + "</body></html>"
    )

    def test_a_layout_trafilatura_misses_is_not_thrown_away(self):
        _, text = html_to_text(self.UNRECOGNISED, "https://example.com")
        self.assertGreaterEqual(len(text), MIN_USEFUL_CHARS)
        self.assertIn("fact number 7", text)

    def test_the_careful_extraction_still_wins_when_it_works(self):
        _, text = html_to_text(self.HTML, "https://example.com")
        self.assertNotIn("menu menu menu", text)


class RetryAfter(unittest.TestCase):
    """429 and 503 are 'not now'. The server usually says how long."""

    def response(self, value):
        return httpx.Response(429, headers={"retry-after": value} if value is not None else {})

    def test_a_plain_number_of_seconds_is_honoured(self):
        self.assertEqual(retry_delay(self.response("5")), 5.0)

    def test_an_absurd_wait_is_clamped(self):
        self.assertEqual(retry_delay(self.response("600")), MAX_RETRY_WAIT)

    def test_a_date_or_no_header_still_waits_a_little(self):
        self.assertGreater(retry_delay(self.response("Wed, 21 Oct 2026 07:28:00 GMT")), 0)
        self.assertGreater(retry_delay(self.response(None)), 0)


class HostPoliteness(unittest.TestCase):
    """Several pages from one site is normal in research. Eight at once is how
    a run earns its own 429."""

    def test_one_host_is_fetched_one_page_at_a_time(self):
        import asyncio

        overlap = {"now": 0, "peak": 0}
        order: list[str] = []

        async def handler(request):
            overlap["now"] += 1
            overlap["peak"] = max(overlap["peak"], overlap["now"])
            order.append(str(request.url))
            await asyncio.sleep(0.02)
            overlap["now"] -= 1
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text="<html><body><p>" + "word " * 200 + "</p></body></html>")

        original = httpx.AsyncClient

        class Mocked(original):
            def __init__(self, *args, **kwargs):
                kwargs.pop("http2", None)
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        urls = [f"https://one.test/page{i}" for i in range(4)]
        httpx.AsyncClient = Mocked
        try:
            # PER_HOST_GAP would make this take three seconds of real time.
            saved, ltms.fetch.PER_HOST_GAP = ltms.fetch.PER_HOST_GAP, 0.0
            try:
                pages = asyncio.run(fetch_many(urls, concurrency=8))
            finally:
                ltms.fetch.PER_HOST_GAP = saved
        finally:
            httpx.AsyncClient = original

        self.assertEqual(overlap["peak"], 1, "two requests hit the same host at once")
        self.assertEqual(len(pages), 4)
        self.assertTrue(all(p.usable for p in pages))


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

class RedditIsReadable(unittest.TestCase):
    """Reddit's HTML is a JavaScript shell on every subdomain, and its .json
    endpoint answers 403 to every user agent. The Atom feed is the way in."""

    def test_a_thread_is_asked_for_as_a_feed(self):
        self.assertEqual(
            fetch_url_for("https://www.reddit.com/r/buildapc/comments/abc/should_i/"),
            "https://www.reddit.com/r/buildapc/comments/abc/should_i/.rss",
        )

    def test_old_and_bare_reddit_are_normalised_to_www(self):
        # old.reddit serves its shell whatever suffix you ask for; only www
        # publishes the feed.
        for url in ("https://old.reddit.com/r/x/comments/abc/",
                    "https://reddit.com/r/x/comments/abc/",
                    "https://new.reddit.com/r/x/comments/abc/"):
            self.assertEqual(fetch_url_for(url), "https://www.reddit.com/r/x/comments/abc/.rss", url)

    def test_tracking_parameters_do_not_reach_the_feed(self):
        self.assertEqual(
            fetch_url_for("https://www.reddit.com/r/x/comments/abc/?utm_source=share#c1"),
            "https://www.reddit.com/r/x/comments/abc/.rss",
        )

    def test_asking_twice_does_not_double_the_suffix(self):
        once = fetch_url_for("https://www.reddit.com/r/x/comments/abc/")
        self.assertEqual(fetch_url_for(once), once)

    def test_everything_else_is_left_exactly_as_it_is(self):
        for url in ("https://example.com/page", "https://sqlite.org/wal.html",
                    "https://reddithelp.com/r/x"):
            self.assertEqual(fetch_url_for(url), url, url)


class FeedText(unittest.TestCase):
    FEED = """<?xml version="1.0" encoding="UTF-8"?>
      <feed xmlns="http://www.w3.org/2005/Atom">
        <title>Should I buy an OLED?</title>
        <entry><title>post</title>
          <content type="html">&amp;lt;p&amp;gt;Burn in is still a thing on OLED panels.&amp;lt;/p&amp;gt;</content>
        </entry>
        <entry><title>comment</title>
          <content type="html">&amp;lt;p&amp;gt;I have used mine for three years with no burn in.&amp;lt;/p&amp;gt;</content>
        </entry>
      </feed>"""

    def test_the_post_and_every_comment_come_through(self):
        title, text = feed_to_text(self.FEED)
        self.assertEqual(title, "Should I buy an OLED?")
        self.assertIn("Burn in is still a thing", text)
        self.assertIn("three years with no burn in", text)

    def test_the_double_escaping_is_undone(self):
        _, text = feed_to_text(self.FEED)
        self.assertNotIn("&lt;", text)
        self.assertNotIn("<p>", text)

    def test_a_feed_is_recognised_without_a_content_type(self):
        self.assertTrue(looks_like_feed(self.FEED.encode(), "text/plain"))
        self.assertTrue(looks_like_feed(b"<html><body>hi", "application/atom+xml"))
        self.assertFalse(looks_like_feed(b"<!DOCTYPE html><html>", "text/html"))


class RateLimitWait(unittest.TestCase):
    def test_reddits_own_header_is_read_when_retry_after_is_missing(self):
        # Measured on reddit: no Retry-After at all, x-ratelimit-reset of 21-54.
        response = httpx.Response(429, headers={"x-ratelimit-reset": "24"})
        self.assertEqual(retry_delay(response), 24.0)

    def test_retry_after_still_wins_when_both_are_sent(self):
        response = httpx.Response(429, headers={"retry-after": "5", "x-ratelimit-reset": "40"})
        self.assertEqual(retry_delay(response), 5.0)


class StrictHosts(unittest.TestCase):
    def test_reddit_is_capped_at_one_page_however_many_it_offers(self):
        results = [
            SearchResult(title=f"mechanical keyboard thread {i}",
                         url=f"https://www.reddit.com/r/kb/comments/{i}/x/",
                         score=1.0 - i / 100, query="mechanical keyboard advice")
            for i in range(6)
        ]
        outcome = dedupe_and_cap(results, limit=20, per_domain=3)
        self.assertEqual(len(outcome.results), 1)

    def test_a_thin_search_does_not_top_up_from_a_strict_host(self):
        results = [
            SearchResult(title=f"mechanical keyboard thread {i}",
                         url=f"https://www.reddit.com/r/kb/comments/{i}/x/",
                         score=0.9, query="mechanical keyboard advice")
            for i in range(6)
        ] + [
            SearchResult(title="mechanical keyboard review", url="https://blog.test/kb",
                         score=1.0, query="mechanical keyboard advice")
        ]
        outcome = dedupe_and_cap(results, limit=20, per_domain=3)
        self.assertEqual(sorted(r.domain for r in outcome.results), ["blog.test", "reddit.com"])


if __name__ == "__main__":
    unittest.main()
