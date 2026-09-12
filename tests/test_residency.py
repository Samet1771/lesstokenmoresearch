"""Reading what a model server has in memory, and how much room it really has."""

import unittest

from ltms.config import ModelConfig
from ltms.extract import Extract
from ltms.residency import Resident, _parse_lms_ps

# Verbatim from `lms ps` on a 16 GB machine running both pipeline models.
LMS_PS = """
IDENTIFIER                                  MODEL                                       STATUS        SIZE        CONTEXT    PARALLEL    DEVICE    TTL
minicpm5-2b-heretic-abliterated             minicpm5-2b-heretic-abliterated             IDLE          2.68 GB     8192       4           Local
qwen3.8-27b-heretic-ara-16gb-vram-xs-mtp    qwen3.8-27b-heretic-ara-16gb-vram-xs-mtp    GENERATING    14.33 GB    32000      2           Local     1h / 1h
"""


class ParseLmsPs(unittest.TestCase):
    def test_reads_every_loaded_model(self):
        rows = _parse_lms_ps(LMS_PS)
        self.assertEqual([row.name for row in rows],
                         ["minicpm5-2b-heretic-abliterated", "qwen3.8-27b-heretic-ara-16gb-vram-xs-mtp"])

    def test_reads_context_and_slots(self):
        reader = _parse_lms_ps(LMS_PS)[0]
        self.assertEqual((reader.context, reader.parallel), (8192, 4))

    def test_ignores_the_header_row(self):
        self.assertEqual(_parse_lms_ps("IDENTIFIER   MODEL   STATUS\n"), [])

    def test_survives_empty_output(self):
        self.assertEqual(_parse_lms_ps(""), [])


class ContextPerSlot(unittest.TestCase):
    def test_slots_divide_the_context(self):
        # The bug that cost an afternoon: 8192 context over 4 slots is 2048 each,
        # and a 5000-token page is rejected with an error that never says so.
        self.assertEqual(Resident("m", context=8192, parallel=4).context_per_slot, 2048)

    def test_one_slot_gets_everything(self):
        self.assertEqual(Resident("m", context=32000, parallel=1).context_per_slot, 32000)

    def test_unknown_context_reports_nothing_rather_than_guessing(self):
        self.assertEqual(Resident("m").context_per_slot, 0)

    def test_zero_slots_is_treated_as_one(self):
        self.assertEqual(Resident("m", context=4096, parallel=0).context_per_slot, 4096)


class ReaderNotes(unittest.TestCase):
    """Each reader leaves one markdown note behind."""

    def test_note_carries_the_facts_and_provenance(self):
        note = Extract(
            url="https://sqlite.org/wal.html", title="WAL", relevance=0.9, kind="docs",
            date="2010-07-21", facts=["WAL arrived in SQLite 3.7.0"], quotes=["one writer at a time"],
        ).to_markdown()
        self.assertIn("# WAL", note)
        self.assertIn("https://sqlite.org/wal.html", note)
        self.assertIn("- relevance: 0.90", note)
        self.assertIn("WAL arrived in SQLite 3.7.0", note)
        self.assertIn("> one writer at a time", note)

    def test_a_failed_reader_still_leaves_a_note_saying_why(self):
        note = Extract(url="https://x.test/a", error="http 403").to_markdown()
        self.assertIn("**failed:** http 403", note)
        self.assertNotIn("## facts", note)

    def test_shortened_pages_are_marked(self):
        note = Extract(url="https://x.test/a", relevance=0.4, facts=["a fact here"], shortened=True).to_markdown()
        self.assertIn("shortened", note)


class ReleaseRules(unittest.TestCase):
    def test_nothing_to_release_when_both_roles_share_a_model(self):
        from ltms.residency import release

        same = ModelConfig(name="one", base_url="http://x/v1")
        self.assertEqual(release(same, same), "")

    def test_nothing_to_release_when_no_model_was_pinned(self):
        from ltms.residency import release

        self.assertEqual(release(ModelConfig(name=""), ModelConfig(name="big")), "")


if __name__ == "__main__":
    unittest.main()
