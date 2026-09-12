"""Tests for the pure pieces: brief parsing, URL handling, triage, event replay."""

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from ltms.brief import BriefError, from_topic, looks_like_brief, missing_brief, parse
from ltms.config import ModelConfig
from ltms.gui import _decode, _match, _model_options
from ltms.llm import DetectedServer
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


class BriefPathGuard(unittest.TestCase):
    """A mistyped path must fail loudly, not get researched as a topic."""

    def test_a_plain_topic_is_not_a_path(self):
        for topic in ["sqlite wal mode concurrency", "postgres vs mysql", "rust async"]:
            self.assertFalse(missing_brief(topic), topic)
            self.assertFalse(looks_like_brief(topic), topic)

    def test_a_missing_markdown_file_is_caught(self):
        self.assertTrue(missing_brief("research.md"))
        self.assertTrue(missing_brief("notes/plan.markdown"))

    def test_anything_shaped_like_a_path_is_caught(self):
        for argument in ["/c/Users/me/wal.md", "C:\\notes\\wal.md", "./briefs/x"]:
            self.assertTrue(missing_brief(argument), argument)

    def test_an_existing_file_is_a_brief_not_a_miss(self):
        import pathlib as pl
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = pl.Path(directory) / "b.md"
            path.write_text("## queries\n- a search\n", encoding="utf-8")
            self.assertTrue(looks_like_brief(str(path)))
            self.assertFalse(missing_brief(str(path)))


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



class GuiModelPicking(unittest.TestCase):
    """The pure helpers behind the two model dropdowns."""

    SERVERS = [
        DetectedServer("LM Studio", "openai-compatible", "http://127.0.0.1:1234/v1",
                       ["qwen3-27b", "gemma-4-e4b", "text-embedding-nomic-embed-text-v1.5"]),
        DetectedServer("Ollama", "ollama", "http://127.0.0.1:11434", ["qwen3:4b"]),
    ]

    def test_lists_chat_models_from_every_server(self):
        options = _model_options(self.SERVERS)
        self.assertEqual(len(options), 3)
        self.assertIn("(LM Studio)", options[0][0])
        self.assertIn("(Ollama)", options[2][0])

    def test_hides_embedding_models(self):
        labels = " ".join(label for label, _ in _model_options(self.SERVERS))
        self.assertNotIn("embedding", labels)

    def test_round_trips_a_choice(self):
        _, value = _model_options(self.SERVERS)[2]
        self.assertEqual(_decode(value), (1, "qwen3:4b"))

    def test_decodes_nothing_selected(self):
        for value in ["Select.BLANK", "Select.NULL", "", None, "no-separator"]:
            self.assertIsNone(_decode(value), repr(value))

    def test_finds_the_saved_choice_again(self):
        self.assertEqual(_match(self.SERVERS, "http://127.0.0.1:11434", "qwen3:4b"), "1::qwen3:4b")
        self.assertIsNone(_match(self.SERVERS, "http://127.0.0.1:11434", "not-loaded"))
        self.assertIsNone(_match(self.SERVERS, "", ""))


class ModelRoles(unittest.TestCase):
    def test_reading_model_can_live_on_another_server(self):
        config = ModelConfig(
            provider="openai-compatible", base_url="http://127.0.0.1:1234/v1", name="big",
            fast_name="small", fast_provider="ollama", fast_base_url="http://127.0.0.1:11434",
        )
        fast = config.for_role("fast")
        self.assertEqual((fast.name, fast.provider, fast.base_url),
                         ("small", "ollama", "http://127.0.0.1:11434"))
        report = config.for_role("report")
        self.assertEqual((report.name, report.base_url), ("big", "http://127.0.0.1:1234/v1"))

    def test_without_an_override_both_roles_share_the_server(self):
        config = ModelConfig(base_url="http://x/v1", name="big", fast_name="small")
        self.assertEqual(config.for_role("fast").base_url, "http://x/v1")

    def test_no_fast_model_means_one_model_for_everything(self):
        config = ModelConfig(name="only")
        self.assertEqual(config.for_role("fast").name, "only")

class HomeFolder(unittest.TestCase):
    """One folder holds everything ltms owns, and it is in the user's home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "user"
        self.home.mkdir()
        self.env = mock.patch.dict(os.environ, {"APPDATA": str(self.home / "AppData" / "Roaming")})
        self.env.start()
        os.environ.pop("LTMS_HOME", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
        self.home_patch = mock.patch.object(Path, "home", staticmethod(lambda: self.home))
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_config_runs_and_reports_all_sit_in_one_folder(self):
        from ltms.config import Config, config_dir, config_path

        root = config_dir()
        self.assertEqual(root, self.home / "ltms")
        for path in (config_path(), Config().runs_path, Config().reports_path):
            self.assertEqual(path.parent, root, path)

    def test_ltms_home_still_overrides_everything(self):
        from ltms.config import config_dir

        with mock.patch.dict(os.environ, {"LTMS_HOME": str(self.home / "elsewhere")}):
            self.assertEqual(config_dir(), self.home / "elsewhere")

    def test_an_old_appdata_install_is_moved_once(self):
        from ltms.config import config_dir, migrate_legacy

        old = self.home / "AppData" / "Roaming" / "ltms"
        (old / "runs").mkdir(parents=True)
        (old / "config.toml").write_text("x = 1", encoding="utf-8")

        self.assertEqual(migrate_legacy(), [old])
        self.assertTrue((config_dir() / "config.toml").exists())
        self.assertTrue((config_dir() / "runs").is_dir())
        self.assertFalse(old.exists())
        # Second call has nothing left to do.
        self.assertEqual(migrate_legacy(), [])

    def test_reports_come_back_from_the_documents_folder(self):
        from ltms.config import Config, migrate_legacy

        old = self.home / "Documents" / "ltms"
        old.mkdir(parents=True)
        (old / "geckos.md").write_text("# geckos", encoding="utf-8")

        self.assertIn(old, migrate_legacy())
        self.assertEqual((Config().reports_path / "geckos.md").read_text(encoding="utf-8"), "# geckos")
        self.assertFalse(old.exists())

    def test_an_existing_home_is_never_overwritten_by_the_old_one(self):
        from ltms.config import config_dir, migrate_legacy

        old = self.home / "AppData" / "Roaming" / "ltms"
        old.mkdir(parents=True)
        (old / "config.toml").write_text("old = 1", encoding="utf-8")
        config_dir().mkdir(parents=True)
        (config_dir() / "config.toml").write_text("new = 1", encoding="utf-8")

        self.assertEqual(migrate_legacy(), [])
        self.assertEqual((config_dir() / "config.toml").read_text(encoding="utf-8"), "new = 1")
        self.assertTrue(old.exists())

    def test_a_custom_home_is_left_alone(self):
        from ltms.config import migrate_legacy

        (self.home / "AppData" / "Roaming" / "ltms").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"LTMS_HOME": str(self.home / "elsewhere")}):
            self.assertEqual(migrate_legacy(), [])


if __name__ == "__main__":
    unittest.main()
