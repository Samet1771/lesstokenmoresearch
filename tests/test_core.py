"""Tests for the pure pieces: brief parsing, URL handling, triage, event replay."""

import textwrap
import unittest

from ltms.brief import BriefError, from_topic, parse
from ltms.llm import is_embedding_model, parse_json_list, strip_thinking
from ltms.pipeline import EFFORTS
from ltms.runs import RunState
from ltms.search.base import SearchResult, canonical_url, dedupe_and_cap


def result(url: str, *, title: str = "t", score: float = 1.0) -> SearchResult:
    return SearchResult(title=title, url=url, score=score)


FULL_BRIEF = textwrap.dedent(
    """
    # postgres replication lag

    ## queries
    - postgres logical replication lag causes
    - postgres wal sender bottleneck
    - postgres logical replication lag causes

    ## questions
    - What makes lag grow under heavy writes?

    ## notes
    Prefer official docs over blog posts.
    """
)


class BriefParsing(unittest.TestCase):
    def test_reads_every_section(self):
        brief = parse(FULL_BRIEF)
        self.assertEqual(brief.topic, "postgres replication lag")
        self.assertEqual(
            brief.queries,
            ["postgres logical replication lag causes", "postgres wal sender bottleneck"],
        )
        self.assertEqual(len(brief.questions), 1)
        self.assertEqual(brief.notes, "Prefer official docs over blog posts.")

    def test_questions_and_notes_are_optional(self):
        brief = parse("# topic\n\n## queries\n- one good search\n")
        self.assertEqual(brief.questions, [])
        self.assertEqual(brief.notes, "")
        self.assertEqual(brief.instructions, "")

    def test_bare_list_of_lines_is_accepted(self):
        brief = parse("first search here\nsecond search here\n")
        self.assertEqual(len(brief.queries), 2)
        self.assertEqual(brief.topic, "first search here")

    def test_ignores_fenced_code(self):
        brief = parse("## queries\n- real search\n\n```\nnot a search\n```\n")
        self.assertEqual(brief.queries, ["real search"])

    def test_accepts_every_bullet_style(self):
        brief = parse("## queries\n1. first search\n* second search\n+ third search\n")
        self.assertEqual(len(brief.queries), 3)

    def test_section_aliases(self):
        brief = parse("## searches\n- a search\n\n## sorular\n- bir soru\n")
        self.assertEqual(brief.queries, ["a search"])
        self.assertEqual(brief.questions, ["bir soru"])

    def test_instructions_combine_questions_and_notes(self):
        instructions = parse(FULL_BRIEF).instructions
        self.assertIn("What makes lag grow", instructions)
        self.assertIn("official docs", instructions)

    def test_brief_without_queries_is_rejected(self):
        with self.assertRaises(BriefError):
            parse("# just a title\n\n## questions\n- nothing to search\n")


class InlineTopic(unittest.TestCase):
    def test_keeps_the_topic_first_and_is_unique(self):
        brief = from_topic("sqlite wal", 5)
        self.assertEqual(brief.queries[0], "sqlite wal")
        self.assertEqual(len(brief.queries), len(set(brief.queries)))
        self.assertEqual(len(brief.queries), 5)

    def test_has_no_source_file(self):
        self.assertIsNone(from_topic("anything", 3).path)


class CanonicalUrl(unittest.TestCase):
    def test_strips_tracking_and_normalises(self):
        self.assertEqual(
            canonical_url("https://WWW.Example.com/docs/?utm_source=x&id=7&gclid=z#frag"),
            "https://example.com/docs?id=7",
        )

    def test_same_page_two_ways_collapses(self):
        a = canonical_url("https://example.com/a/")
        b = canonical_url("https://www.example.com/a?utm_campaign=q")
        self.assertEqual(a, b)

    def test_leaves_non_http_alone(self):
        self.assertEqual(canonical_url("mailto:x@y.z"), "mailto:x@y.z")


class Triage(unittest.TestCase):
    def test_removes_duplicates(self):
        outcome = dedupe_and_cap(
            [result("https://a.com/x"), result("https://www.a.com/x/?utm_source=q")], limit=10
        )
        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(outcome.dropped.get("duplicate"), 1)

    def test_drops_noise_domains(self):
        outcome = dedupe_and_cap([result("https://pinterest.com/pin/1"), result("https://a.com/x")], limit=10)
        self.assertEqual([r.domain for r in outcome.results], ["a.com"])
        self.assertEqual(outcome.dropped.get("noise"), 1)

    def test_caps_one_domain_when_alternatives_exist(self):
        flood = [result(f"https://big.com/{i}", score=10 - i) for i in range(8)]
        others = [result(f"https://other{i}.com/x", score=1) for i in range(5)]
        outcome = dedupe_and_cap(flood + others, limit=6, per_domain=3)
        domains = [r.domain for r in outcome.results]
        self.assertEqual(domains.count("big.com"), 3)
        self.assertEqual(len(outcome.results), 6)

    def test_cap_relaxes_rather_than_starving_the_budget(self):
        flood = [result(f"https://big.com/{i}", score=10 - i) for i in range(8)]
        outcome = dedupe_and_cap(flood, limit=6, per_domain=3)
        self.assertEqual(len(outcome.results), 6)

    def test_rejects_unusable_entries(self):
        outcome = dedupe_and_cap([result("not-a-url"), result("https://a.com/x", title="  ")], limit=10)
        self.assertEqual(outcome.results, [])
        self.assertEqual(outcome.dropped.get("unusable"), 2)


class EventReplay(unittest.TestCase):
    def test_rebuilds_state_from_events(self):
        state = RunState()
        for event in [
            {"t": 0, "type": "meta", "run_id": "r1", "query": "q", "effort": "low"},
            {"t": 1, "type": "stage", "name": "search", "status": "run"},
            {"t": 2, "type": "progress", "name": "search", "done": 3, "total": 4},
            {"t": 3, "type": "agent", "id": "scout-1", "state": "read", "detail": "u", "score": 0.7},
            {"t": 4, "type": "metric", "tokens_saved": 1200},
            {"t": 5, "type": "end", "status": "ok", "report": "p", "summary": "s"},
        ]:
            state.apply(event)

        self.assertEqual(state.run_id, "r1")
        self.assertEqual(state.stages["search"].done, 3)
        self.assertEqual(state.agents["scout-1"].score, 0.7)
        self.assertEqual(state.metrics["tokens_saved"], 1200)
        self.assertTrue(state.finished)
        self.assertEqual(state.elapsed, 5)

    def test_progress_starts_a_stage_that_was_never_marked_running(self):
        state = RunState()
        state.apply({"t": 1, "type": "progress", "name": "read", "done": 1, "total": 9})
        self.assertEqual(state.stages["read"].status, "run")

    def test_notes_are_bounded(self):
        state = RunState()
        for i in range(50):
            state.apply({"t": i, "type": "note", "text": str(i), "level": "info"})
        self.assertLessEqual(len(state.notes), 6)


class Efforts(unittest.TestCase):
    def test_every_effort_reads_no_more_than_it_gathers(self):
        for name, preset in EFFORTS.items():
            self.assertLessEqual(preset.read, preset.candidates, name)
            self.assertGreaterEqual(preset.queries, 1, name)


class ModelReplyParsing(unittest.TestCase):
    def test_extracts_array_from_chatty_reply(self):
        self.assertEqual(parse_json_list('Sure!\n["a b", "c d"]\nDone.'), ["a b", "c d"])

    def test_returns_empty_on_garbage(self):
        self.assertEqual(parse_json_list("no array here"), [])

    def test_strips_thinking_block(self):
        self.assertEqual(strip_thinking("<think>hmm</think>answer"), "answer")


class ModelSelection(unittest.TestCase):
    def test_rejects_non_chat_models(self):
        for name in ["text-embedding-nomic-embed-text-v1.5", "bge-m3", "jina-reranker-v2", "whisper-large-v3"]:
            self.assertTrue(is_embedding_model(name), name)

    def test_accepts_chat_models(self):
        for name in ["qwen3-14b", "gemma-3-12b-it", "llama-3.1-8b-instruct", "mistral-small"]:
            self.assertFalse(is_embedding_model(name), name)


if __name__ == "__main__":
    unittest.main()
