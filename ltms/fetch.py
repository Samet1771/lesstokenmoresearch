"""Fetching pages and turning them into plain text.

This is the layer that makes a "source" mean a page that was actually read
rather than a search-result snippet. Everything here is defensive: a research
run visits dozens of sites it has never seen, and any one of them may be slow,
enormous, a login wall, or a PDF pretending to be HTML.
"""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass, field

import httpx

# A real, honest user agent. Some sites serve a stub to unknown clients, and
# pretending to be a browser we are not invites being blocked later.
USER_AGENT = (
    "Mozilla/5.0 (compatible; lesstokenmoresearch/0.1; "
    "+https://github.com/Samet1771/lesstokenmoresearch)"
)

MAX_BYTES = 5 * 1024 * 1024  # a page bigger than this is not prose
MIN_USEFUL_CHARS = 400  # below this there is nothing to extract

# One request at a time per host, with a gap between them. Research fetches
# several pages from the same documentation site, and hitting it with eight at
# once is how a run earns its own 429.
PER_HOST_GAP = 0.7
# 429 and 503 usually come with Retry-After. Waiting the asked-for time gets
# the page; ignoring it gets a longer ban. Anything beyond this is not worth
# holding a run for.
MAX_RETRY_WAIT = 12.0

WHITESPACE = re.compile(r"[ \t ]+")
BLANK_LINES = re.compile(r"\n{3,}")

# Cheap detection of pages that exist but say nothing useful.
WALL_PATTERNS = re.compile(
    r"(enable javascript|are you a robot|verify you are human|checking your browser"
    r"|access denied|subscribe to (continue|read)|create a free account to)",
    re.I,
)

# An anti-bot challenge rather than a server that is merely unhappy. Worth
# telling apart: a 403 might be fixed by asking differently, a challenge is the
# site saying it does not want automated readers, and we take it at its word.
CHALLENGE_PATTERNS = re.compile(
    r"(just a moment|cf-chl|captcha|/cdn-cgi/challenge|checking if the site connection"
    r"|enable javascript and cookies to continue|attention required)",
    re.I,
)


@dataclass
class Page:
    url: str
    title: str = ""
    text: str = ""
    kind: str = "html"  # html | pdf
    chars: int = 0
    error: str = ""
    blocked: bool = False

    @property
    def usable(self) -> bool:
        return not self.error and self.chars >= MIN_USEFUL_CHARS


@dataclass
class FetchStats:
    fetched: int = 0
    usable: int = 0
    failures: dict[str, int] = field(default_factory=dict)

    def record(self, page: Page) -> None:
        self.fetched += 1
        if page.usable:
            self.usable += 1
        elif page.error:
            key = page.error.split(":")[0][:24]
            self.failures[key] = self.failures.get(key, 0) + 1
        elif page.blocked:
            self.failures["blocked"] = self.failures.get("blocked", 0) + 1
        else:
            self.failures["too short"] = self.failures.get("too short", 0) + 1


def tidy(text: str) -> str:
    text = WHITESPACE.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.strip() for line in text.split("\n")]
    return BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(html: str, url: str) -> tuple[str, str]:
    """Return (title, text). Uses trafilatura when present, else a plain strip.

    trafilatura aims at the article and ignores everything else, which is right
    almost always and disastrous on the pages where it finds nothing: a page we
    successfully fetched is thrown away over a layout it did not recognise.
    Measured across 130 fetched pages, 30 came back under the useful threshold
    and stripping the tags by hand rescued 9 of them. So when the careful path
    returns too little, take whichever is longer.
    """
    title = ""
    extracted = ""
    try:
        import trafilatura

        extracted = tidy(
            trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
            )
            or ""
        )
        metadata = trafilatura.extract_metadata(html)
        title = (getattr(metadata, "title", "") or "") if metadata else ""
    except Exception:  # noqa: BLE001 - fall through to the crude path
        pass

    if len(extracted) >= MIN_USEFUL_CHARS:
        return title, extracted

    crude_title, crude = _strip_tags(html)
    if len(crude) > len(extracted):
        return title or crude_title, crude
    return title or crude_title, extracted


def _strip_tags(html: str) -> tuple[str, str]:
    """Last resort when trafilatura is unavailable or returns nothing."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = tidy(re.sub(r"<[^>]+>", " ", title_match.group(1))) if title_match else ""
    body = re.sub(r"(?is)<(script|style|nav|footer|header|noscript|svg)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", body)
    body = re.sub(r"<[^>]+>", " ", body)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        body = body.replace(entity, char)
    return title, tidy(body)


def pdf_to_text(payload: bytes) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", ""
    try:
        reader = PdfReader(io.BytesIO(payload))
        title = (reader.metadata or {}).get("/Title", "") or ""
        # Research PDFs can run to hundreds of pages; the first 40 carry the
        # argument, and the rest is references and appendices.
        pages = [page.extract_text() or "" for page in reader.pages[:40]]
    except Exception as error:  # noqa: BLE001 - malformed PDFs are common
        return "", f"pdf: {type(error).__name__}"
    return str(title), tidy("\n\n".join(pages))


def retry_delay(response: httpx.Response) -> float:
    """How long the server asked us to wait, clamped to something bearable."""
    raw = (response.headers.get("retry-after") or "").strip()
    try:
        wait = float(raw)
    except ValueError:
        wait = 3.0 if raw else 2.0
    return min(max(wait, 0.5), MAX_RETRY_WAIT)


async def fetch_one(client: httpx.AsyncClient, url: str) -> Page:
    page = Page(url=url)
    try:
        response = await client.get(url)
        # 429 and 503 are "not now", not "no". The server usually says how long
        # to wait; one patient retry costs a few seconds and often gets a page
        # that would otherwise be written off.
        if response.status_code in (429, 503):
            wait = retry_delay(response)
            if wait <= MAX_RETRY_WAIT:
                await asyncio.sleep(wait)
                response = await client.get(url)
    except httpx.HTTPError as error:
        page.error = f"{type(error).__name__}"
        return page

    if response.status_code >= 400:
        looks_like_html = "html" in response.headers.get("content-type", "").lower()
        if looks_like_html and CHALLENGE_PATTERNS.search(response.text[:4000]):
            page.blocked = True
            page.error = "anti-bot challenge"
        else:
            page.error = f"http {response.status_code}"
        return page

    content_type = response.headers.get("content-type", "").lower()
    payload = response.content[:MAX_BYTES]

    if "pdf" in content_type or url.lower().endswith(".pdf") or payload[:4] == b"%PDF":
        page.kind = "pdf"
        title, text = pdf_to_text(payload)
        if text.startswith("pdf: "):
            page.error = text
            return page
    else:
        encoding = response.encoding or "utf-8"
        try:
            html = payload.decode(encoding, errors="replace")
        except LookupError:
            html = payload.decode("utf-8", errors="replace")
        title, text = html_to_text(html, url)

    page.title = tidy(title)[:200]
    page.text = text
    page.chars = len(text)
    # A short page full of "enable javascript" is a wall, not a source.
    if page.chars < MIN_USEFUL_CHARS and WALL_PATTERNS.search(text):
        page.blocked = True
    return page


async def fetch_many(
    urls: list[str],
    concurrency: int = 8,
    timeout: float = 20.0,
    on_start=None,
    on_done=None,
) -> list[Page]:
    """Fetch in parallel, never letting one bad host hold up the run."""
    gate = asyncio.Semaphore(max(1, concurrency))
    # One lock per host: parallelism across sites, politeness within one.
    hosts: dict[str, asyncio.Lock] = {}
    pages: list[Page] = [Page(url=url) for url in urls]

    def host_of(url: str) -> str:
        return url.split("/")[2].lower() if "://" in url else url

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=8.0),
        follow_redirects=True,
        headers={
            "user-agent": USER_AGENT,
            "accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
            "accept-language": "en;q=0.9,*;q=0.5",
        },
        max_redirects=4,
        # Browsers speak HTTP/2; a client that only speaks 1.1 stands out to
        # the protections sitting in front of these sites.
        http2=True,
    ) as client:

        async def one(index: int, url: str) -> None:
            lock = hosts.setdefault(host_of(url), asyncio.Lock())
            async with lock:
                async with gate:
                    if on_start:
                        on_start(index, url)
                    try:
                        pages[index] = await fetch_one(client, url)
                    except Exception as error:  # noqa: BLE001 - never kill the batch
                        pages[index] = Page(url=url, error=type(error).__name__)
                    if on_done:
                        on_done(index, pages[index])
                # The host lock is held a moment longer, but the global slot is
                # already free: other sites keep moving while this one rests.
                await asyncio.sleep(PER_HOST_GAP)

        await asyncio.gather(*(one(i, url) for i, url in enumerate(urls)))

    return pages
