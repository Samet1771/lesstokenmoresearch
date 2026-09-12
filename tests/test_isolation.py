"""Each page is read by a reader that knows nothing about the other pages.

LM Studio shows every request to one loaded model on a single row, which makes
it look as though the pages share a conversation. They do not, and this test is
here so that stays true: it captures the actual request bodies and checks that
none of them carries another page or any prior turn.
"""

import asyncio
import json
import unittest

import httpx

from ltms.brief import from_topic
from ltms.config import ModelConfig
from ltms.extract import Extract, extract_many
from ltms.fetch import Page
from ltms.report import write_report


def pages(count: int) -> list[Page]:
    return [
        Page(url=f"https://site{i}.test/p", title=f"Page {i}", text=f"MARKER-{i} " + "body text. " * 80, chars=1000)
        for i in range(1, count + 1)
    ]


def capture(sent: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": '{"relevance":0.6,"facts":["a fact from this page"]}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    return handler


def run_extraction(page_list: list[Page], concurrency: int = 1) -> list[dict]:
    sent: list[dict] = []
    original = httpx.AsyncClient

    class Mocked(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(capture(sent))
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = Mocked
    try:
        asyncio.run(
            extract_many(page_list, "sqlite wal", "", ModelConfig(name="m"), concurrency=concurrency)
        )
    finally:
        httpx.AsyncClient = original
    # Parallel reading opens with a warm-up call, so a cold model finishes
    # loading before the readers arrive. It carries no page; the reader
    # requests are what this file is about.
    return [body for body in sent if "PAGE" in " ".join(m["content"] for m in body["messages"])]


def run_report() -> list[dict]:
    sent: list[dict] = []
    original = httpx.AsyncClient

    class Mocked(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(capture(sent))
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = Mocked
    try:
        asyncio.run(
            write_report(
                from_topic("sqlite wal", 3),
                [Extract(url="https://a.test/p", title="A", facts=["a fact"], relevance=0.8)],
                [],
                ModelConfig(name="m"),
            )
        )
    finally:
        httpx.AsyncClient = original
    return sent


class ReaderIsolation(unittest.TestCase):
    def test_one_request_per_page(self):
        self.assertEqual(len(run_extraction(pages(3))), 3)

    def test_every_request_is_system_plus_one_page(self):
        for body in run_extraction(pages(3)):
            self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])

    def test_no_request_carries_another_page(self):
        for index, body in enumerate(run_extraction(pages(3)), start=1):
            text = " ".join(message["content"] for message in body["messages"])
            self.assertIn(f"MARKER-{index}", text)
            for other in {1, 2, 3} - {index}:
                self.assertNotIn(f"MARKER-{other}", text, f"request {index} leaked page {other}")

    def test_prompt_size_does_not_grow_with_each_page(self):
        sizes = [
            sum(len(message["content"]) for message in body["messages"])
            for body in run_extraction(pages(4))
        ]
        # Identical pages, so identical prompts. A conversation would grow.
        self.assertEqual(len(set(sizes)), 1, sizes)

    def test_no_request_sets_an_output_limit(self):
        # The server decides how long an answer may be. A second limit set from
        # here can only be the wrong number: too low truncates the JSON, too
        # high does nothing.
        for body in run_extraction(pages(2)):
            self.assertNotIn("max_tokens", body)

    def test_the_warm_up_call_carries_no_page(self):
        sent = []
        original = httpx.AsyncClient

        class Mocked(original):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(capture(sent))
                super().__init__(*args, **kwargs)

        httpx.AsyncClient = Mocked
        try:
            asyncio.run(extract_many(pages(2), "sqlite wal", "", ModelConfig(name="m"), concurrency=2))
        finally:
            httpx.AsyncClient = original

        self.assertEqual(len(sent), 3, "expected one warm-up plus two readers")
        warm_up = sent[0]
        text = " ".join(message["content"] for message in warm_up["messages"])
        self.assertNotIn("MARKER-", text)
        self.assertLess(len(text), 40)

    def test_isolation_holds_when_readers_run_in_parallel(self):
        bodies = run_extraction(pages(4), concurrency=4)
        self.assertEqual(len(bodies), 4)
        for body in bodies:
            self.assertEqual(len(body["messages"]), 2)


class NoOutputCaps(unittest.TestCase):
    """ltms sets no response-length limit anywhere. LM Studio (or whichever
    server is in use) already has one configured, and a second number chosen
    here could only be the wrong one."""

    def test_the_editor_does_not_cap_the_report(self):
        for body in run_report():
            self.assertNotIn("max_tokens", body)


if __name__ == "__main__":
    unittest.main()
