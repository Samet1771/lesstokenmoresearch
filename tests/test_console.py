"""Driving the console the way a person does.

Every test here exists because using it by hand found something: buttons laid
out past the bottom of the screen, a timer that raised once a modal was open,
a status bar that ran off the edge.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from rich.text import Text
from textual.widgets import Button, Input, Select, Static

from ltms import config as config_mod
from ltms.config import Config, ModelConfig
from ltms.gui import MOODS, Console, ModelsScreen, RunsScreen, _short_model
from ltms.llm import DetectedServer

SERVERS = [
    DetectedServer("LM Studio", "openai-compatible", "http://127.0.0.1:1234/v1", ["big-model", "small-model"]),
    DetectedServer("Ollama", "ollama", "http://127.0.0.1:11434", ["tiny-model"]),
]


def console(home: str) -> Console:
    os.environ["LTMS_HOME"] = home
    app = Console(config_mod.load())
    app.servers = SERVERS  # skip the network probe
    return app


def drive(body, size=(100, 34)):
    """Run an async body against a fresh console in a throwaway config home."""

    async def runner():
        with tempfile.TemporaryDirectory() as home:
            app = console(home)
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                return await body(app, pilot, Path(home))

    previous = os.environ.get("LTMS_HOME")
    try:
        return asyncio.run(runner())
    finally:
        if previous is None:
            os.environ.pop("LTMS_HOME", None)
        else:
            os.environ["LTMS_HOME"] = previous


async def open_models(app, pilot):
    app.query_one("#command", Input).value = "/models"
    await pilot.press("enter")
    await pilot.pause()
    return app.screen


class Layout(unittest.TestCase):
    def test_the_console_has_its_four_regions(self):
        async def body(app, pilot, home):
            return [bool(app.query(f"#{name}")) for name in ("banner", "transcript", "command", "statusbar")]

        self.assertEqual(drive(body), [True, True, True, True])

    def test_the_save_buttons_are_on_screen(self):
        """They were laid out at y=48 of a 34-row screen, so nothing could save."""

        async def body(app, pilot, home):
            screen = await open_models(app, pilot)
            return {
                button.id: (button.region.y, button.region.height)
                for button in screen.query(Button)
            }

        buttons = drive(body, size=(100, 34))
        self.assertEqual(set(buttons), {"save", "rescan", "cancel"})
        for name, (y, height) in buttons.items():
            self.assertGreater(height, 0, name)
            self.assertLess(y + height, 34, f"{name} is off the bottom of the screen")

    def test_the_buttons_stay_on_screen_when_the_terminal_is_short(self):
        async def body(app, pilot, home):
            screen = await open_models(app, pilot)
            return [b.region.y + b.region.height for b in screen.query(Button)]

        for bottom in drive(body, size=(100, 24)):
            self.assertLessEqual(bottom, 24)


class Saving(unittest.TestCase):
    def test_ctrl_s_writes_the_config(self):
        async def body(app, pilot, home):
            screen = await open_models(app, pilot)
            screen.query_one("#report-model", Select).value = "0::big-model"
            screen.query_one("#fast-model", Select).value = "1::tiny-model"
            screen.query_one("#parallel", Input).value = "6"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            return not isinstance(app.screen, ModelsScreen), config_mod.load()

        closed, saved = drive(body)
        self.assertTrue(closed)
        self.assertEqual(saved.model.name, "big-model")
        self.assertEqual(saved.model.fast_name, "tiny-model")
        self.assertEqual(saved.model.parallel, 6)

    def test_the_reading_model_records_its_own_server(self):
        async def body(app, pilot, home):
            screen = await open_models(app, pilot)
            screen.query_one("#report-model", Select).value = "0::big-model"
            screen.query_one("#fast-model", Select).value = "1::tiny-model"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            return config_mod.load()

        saved = drive(body)
        self.assertEqual(saved.model.base_url, "http://127.0.0.1:1234/v1")
        self.assertEqual(saved.model.fast_base_url, "http://127.0.0.1:11434")
        self.assertEqual(saved.model.for_role("fast").base_url, "http://127.0.0.1:11434")

    def test_escape_writes_nothing(self):
        async def body(app, pilot, home):
            screen = await open_models(app, pilot)
            screen.query_one("#parallel", Input).value = "9"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return (home / "config.toml").exists(), not isinstance(app.screen, ModelsScreen)

        written, closed = drive(body)
        self.assertFalse(written)
        self.assertTrue(closed)


class Timers(unittest.TestCase):
    def test_the_status_timer_survives_a_modal(self):
        """App.query_one searches the active screen, so once a modal was open
        the refresh timer raised eight times a second."""

        async def body(app, pilot, home):
            await open_models(app, pilot)
            for _ in range(6):
                app.pump()  # what the interval calls
            await pilot.press("escape")
            await pilot.pause()
            app.pump()
            return True

        self.assertTrue(drive(body))

    def test_runs_screen_opens_and_closes(self):
        async def body(app, pilot, home):
            app.query_one("#command", Input).value = "/runs"
            await pilot.press("enter")
            await pilot.pause()
            opened = isinstance(app.screen, RunsScreen)
            await pilot.press("escape")
            await pilot.pause()
            return opened, isinstance(app.screen, RunsScreen)

        opened, still_open = drive(body)
        self.assertTrue(opened)
        self.assertFalse(still_open)


class StatusBar(unittest.TestCase):
    def line(self, width: int) -> str:
        async def body(app, pilot, home):
            app.busy = True
            app.engine = "docker in WSL (Ubuntu)"
            app.config.model.name = "a-rather-long-model-name-here"
            app.config.model.fast_name = "another-long-model-name"
            app.state.metrics["tokens_saved"] = 17302
            app.state.elapsed = 161
            await pilot.pause()
            return Text.from_markup(app.status_line()).plain

        return drive(body, size=(width, 24))

    def test_fits_every_terminal_width(self):
        for width in (50, 60, 80, 100, 140):
            self.assertLessEqual(len(self.line(width)), width - 4, f"width {width}")

    def test_keeps_what_matters_when_narrow(self):
        narrow = self.line(56)
        self.assertIn("saved", narrow)
        self.assertIn("2:41", narrow)

    def test_shows_more_when_there_is_room(self):
        wide = self.line(160)
        self.assertIn("docker in WSL", wide)
        self.assertIn("read", wide)

    def test_the_engine_label_goes_first_when_space_runs_out(self):
        tight = self.line(96)
        self.assertNotIn("docker in WSL", tight)
        self.assertIn("2:41", tight)

    def test_how_to_use_it_lives_in_the_banner_not_the_status_bar(self):
        from ltms.gui import BANNER

        self.assertIn("/help", BANNER)
        self.assertIn("ctrl+c", BANNER)
        self.assertNotIn("/help", self.line(160))

    def test_long_model_names_are_shortened(self):
        self.assertLessEqual(len(_short_model("x" * 60)), 22)
        self.assertEqual(_short_model("short"), "short")


class Mood(unittest.TestCase):
    def test_every_stage_has_a_face(self):
        for stage in ("plan", "search", "filter", "read", "rank", "debate", "write"):
            self.assertIn(stage, MOODS, stage)

    def test_the_face_follows_the_stage_in_flight(self):
        async def body(app, pilot, home):
            app.busy = True
            app.state.apply({"t": 1, "type": "stage", "name": "read", "status": "run"})
            reading = app.mood()[0]
            app.state.apply({"t": 2, "type": "stage", "name": "read", "status": "ok"})
            app.state.apply({"t": 3, "type": "stage", "name": "write", "status": "run"})
            return reading, app.mood()[0]

        reading, writing = drive(body)
        self.assertEqual(reading, MOODS["read"][0])
        self.assertEqual(writing, MOODS["write"][0])

    def test_a_finished_run_smiles_and_a_failed_one_does_not(self):
        async def body(app, pilot, home):
            app.busy = False
            app.state.apply({"t": 1, "type": "end", "status": "ok"})
            good = app.mood()[0]
            app.state.status = "failed"
            return good, app.mood()[0]

        good, bad = drive(body)
        self.assertEqual(good, MOODS["done"][0])
        self.assertEqual(bad, MOODS["fail"][0])


class Commands(unittest.TestCase):
    def test_unknown_command_does_not_start_a_run(self):
        async def body(app, pilot, home):
            app.query_one("#command", Input).value = "/nonsense"
            await pilot.press("enter")
            await pilot.pause()
            return app.busy

        self.assertFalse(drive(body))

    def test_a_missing_brief_path_does_not_start_a_run(self):
        async def body(app, pilot, home):
            app.query_one("#command", Input).value = "./nowhere/brief.md"
            await pilot.press("enter")
            await pilot.pause()
            return app.busy

        self.assertFalse(drive(body))

    def test_the_prompt_clears_after_submitting(self):
        async def body(app, pilot, home):
            field = app.query_one("#command", Input)
            field.value = "/help"
            await pilot.press("enter")
            await pilot.pause()
            return field.value

        self.assertEqual(drive(body), "")


if __name__ == "__main__":
    unittest.main()
