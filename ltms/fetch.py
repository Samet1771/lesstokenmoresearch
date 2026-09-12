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
# 429 and 503 come with Retry-After, or with Reddit's own x-ratelimit-reset.
# Waiting the asked-for time gets the page; ignoring it gets a longer ban.
# Measured on Reddit: no Retry-After at all, and resets of 21 to 54 seconds.
# Long, but the wait happens on that host's own lock while every other site in
# the run keeps moving, so it costs one page's latency rather than the run's.
MAX_RETRY_WAIT = 30.0

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
    kind: str = "html"  # html | pdf | feed
    chars: int = 0
    error: str = ""
    blocked: bool = False
    retry_after: float = 0.0

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


# Reddit serves every thread as a JavaScript shell -- 320 KB of markup holding
# "Welcome to Reddit. Skip to main content" and nothing else, on www and on
# old.reddit alike. The .json endpoint answers 403 to every user agent tried.
# But the thread is still published as an Atom feed, post and comments both,
# and that is a door Reddit deliberately leaves open.
REDDIT_PAGE = re.compile(r"^https?://(?:[\w-]+\.)?reddit\.com/r/[^/]+", re.I)

# The entries of an Atom feed, and what an Atom feed looks like from outside.
FEED_ENTRY = re.compile(r"<content[^>]*>(.*?)</content>", re.S | re.I)
FEED_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
FEED_TYPES = ("atom+xml", "rss+xml", "application/xml", "text/xml")


def fetch_url_for(url: str) -> str:
    """The address to actually request, which is not always the one cited.

    The host is normalised to www along the way: old.reddit.com serves its
    JavaScript shell whatever suffix you ask it for, and only www publishes
    the feed.
    """
    match = REDDIT_PAGE.match(url)
    if not match:
        return url
    path = url.split("?")[0].split("#")[0].rstrip("/")
    path = path[path.index("/r/") :]
    return f"https://www.reddit.com{path}" + ("" if path.endswith(".rss") else "/.rss")


def feed_to_text(xml: str) -> tuple[str, str]:
    """An Atom or RSS feed as plain text: the title, then every entry.

    Feed content is HTML escaped inside XML, and Reddit escapes it twice, so
    the entities have to come off in two passes before the tags can be stripped.
    """
    import html as html_module

    titles = FEED_TITLE.findall(xml)
    title = tidy(re.sub(r"<[^>]+>", " ", html_module.unescape(titles[0]))) if titles else ""

    parts: list[str] = []
    for raw in FEED_ENTRY.findall(xml):
        unescaped = html_module.unescape(html_module.unescape(raw))
        _, text = _strip_tags(unescaped)
        if text:
            parts.append(text)
    return title, tidy("\n\n".join(parts))


def looks_like_feed(payload: bytes, content_type: str) -> bool:
    if any(kind in content_type for kind in FEED_TYPES):
        return True
    head = payload[:400].lstrip().lower()
    return head.startswith(b"<?xml") and (b"<feed" in head or b"<rss" in head)


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
    """How long the server asked us to wait, clamped to something bearable.

    Retry-After is the standard answer. Reddit does not send it and instead
    reports x-ratelimit-reset, so read that too rather than guessing two
    seconds and being refused again.
    """
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        raw = (response.headers.get("x-ratelimit-reset") or "").strip()
    try:
        wait = float(raw)
    except ValueError:
        wait = 3.0 if raw else 2.0
    return min(max(wait, 0.5), MAX_RETRY_WAIT)


async def fetch_one(client: httpx.AsyncClient, url: str) -> Page:
    page = Page(url=url)
    target = fetch_url_for(url)
    try:
        response = await client.get(target)
        # 429 and 503 are "not now", not "no". Report how long the server asked
        # for and let the caller wait outside its concurrency slot, so one
        # rate-limited host does not stall the fetches that could be running.
        if response.status_code in (429, 503):
            page.retry_after = retry_delay(response)
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

    if looks_like_feed(payload, content_type):
        page.kind = "feed"
        title, text = feed_to_text(payload.decode(response.encoding or "utf-8", errors="replace"))
    elif "pdf" in content_type or url.lower().endswith(".pdf") or payload[:4] == b"%PDF":
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

        async def attempt(index: int, url: str, announce: bool) -> Page:
            async with gate:
                if announce and on_start:
                    on_start(index, url)
                try:
                    return await fetch_one(client, url)
                except Exception as error:  # noqa: BLE001 - never kill the batch
                    return Page(url=url, error=type(error).__name__)

        async def one(index: int, url: str) -> None:
            lock = hosts.setdefault(host_of(url), asyncio.Lock())
            async with lock:
                page = await attempt(index, url, announce=True)
                # "Not now" rather than "no". The wait happens outside the
                # concurrency slot: this host is asleep on its own lock while
                # every other site in the run carries on.
                if page.retry_after:
                    await asyncio.sleep(page.retry_after)
                    page = await attempt(index, url, announce=False)
                if page.retry_after and not page.error:
                    page.error = "rate limited"
                pages[index] = page
                if on_done:
                    on_done(index, page)
                # The host lock is held a moment longer, but the global slot is
                # already free: other sites keep moving while this one rests.
                await asyncio.sleep(PER_HOST_GAP)

        await asyncio.gather(*(one(i, url) for i, url in enumerate(urls)))

    return pages
