"""Search backend interface and result shape.

Only SearXNG is implemented today. The interface exists so a second backend is
one file rather than a refactor.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TRACKING_PARAMS = re.compile(r"^(utm_|ref_|mc_|pk_|yclid|gclid|fbclid|igshid|si|spm)", re.I)

# Domains that reliably produce noise for research: aggregators of aggregators,
# link farms, and walled gardens whose content we cannot read anyway.
#
# The second group is measured rather than assumed. Fetching 130 pages from
# past runs, every one of these returned a JavaScript shell or a login wall --
# between 6 and 400 characters of text, none of it the content. They are worth
# dropping before the fetch rather than after: a candidate that cannot be read
# occupies a reading slot that a readable page could have had.
NOISE_DOMAINS = {
    "pinterest.com",
    "quora.com",
    "slideshare.net",
    "scribd.com",
    "coursehero.com",
    "researchgate.net",  # blocks automated reads behind an interstitial
    "academia.edu",
    # JavaScript applications and login walls -- nothing to extract.
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "reddit.com",  # old.reddit is blocked too; measured 39 characters
    "spotify.com",
    "music.apple.com",
}


# SearXNG's `it` category is stackoverflow, github, MDN, docker hub and friends.
# They answer *every* query, including ones about keyboards or recipes, and
# because the general web engines are the ones being rate-limited, the answers
# they give end up dominating. One real run for "best mechanical keyboards in
# turkey" came back with 14 of its 20 candidates from MDN.
TECHNICAL = re.compile(
    r"\b("
    r"api|apis|sdk|cli|http[s]?|tcp|tls|ssl|dns|url|uri|json|xml|yaml|csv|regex"
    r"|sql|nosql|database|schema|query|index|migration|orm|transaction"
    r"|python|javascript|typescript|java|kotlin|swift|rust|golang|ruby|php|perl"
    r"|c\+\+|c#|bash|shell|powershell|assembly"
    r"|linux|unix|kernel|ubuntu|debian|windows server|macos"
    r"|docker|kubernetes|container|vm|virtual machine|terraform|ansible"
    r"|git|github|gitlab|npm|pip|cargo|maven|gradle|webpack|vite"
    r"|server|client|backend|frontend|compiler|runtime|framework|library|package"
    r"|thread|async|concurrency|mutex|deadlock|race condition|memory leak"
    r"|cache|caching|latency|throughput|benchmark|profiling|optimisation|optimization"
    r"|auth|oauth|jwt|token|encryption|hash|certificate"
    r"|exception|stack trace|segfault|debug|debugging|unit test|ci/cd|pipeline"
    r"|html|css|dom|react|vue|angular|django|flask|rails|spring"
    r"|postgres|postgresql|mysql|sqlite|redis|mongodb|kafka|elasticsearch"
    r"|aws|azure|gcp|s3|lambda|serverless"
    r"|algorithm|data structure|protocol|specification|rfc|syntax|config|configuration"
    r")\b"
    # An HTTP status code, a version number, a file extension, a dotted module
    # path -- all things only a technical question contains.
    r"|\b[45]\d{2}\b|\bv?\d+\.\d+(\.\d+)?\b|\.(py|js|ts|go|rs|rb|java|c|cpp|h|sh|yml|yaml|json|toml)\b",
    re.I,
)


def categories_for(queries: list[str], configured: str) -> str:
    """Ask the developer engines only when the question is a developer's.

    They are on by default because they are the part of SearXNG nothing
    rate-limits, and for technical research they are the better sources. For
    anything else they are a firehose of confidently irrelevant documentation.
    """
    if "it" not in configured:
        return configured
    if any(TECHNICAL.search(query) for query in queries):
        return configured
    return ",".join(part for part in configured.split(",") if part.strip() != "it") or "general"


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    score: float = 0.0
    published: str = ""
    query: str = ""

    @property
    def domain(self) -> str:
        host = urlsplit(self.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["domain"] = self.domain
        return data


@dataclass
class SearchOutcome:
    results: list[SearchResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_count: int = 0
    dropped: dict[str, int] = field(default_factory=dict)


class SearchBackend(Protocol):
    name: str

    async def search(self, query: str, limit: int) -> list[SearchResult]: ...


def canonical_url(raw: str) -> str:
    """Strip tracking noise so the same page from two engines dedupes to one key."""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if parts.scheme not in ("http", "https"):
        return raw
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, netloc, path, urlencode(query), ""))


STOPWORDS = frozenset(
    "a an and are as at be best by can do does for from good has have how i in into is it its"
    " may more most my new no not of on or our so than that the their there these this to top"
    " under up use using vs was what when where which who why will with you your"
    " bir bu da de en icin için ile mi mu nasil nasıl ne ve ya".split()
)


def content_words(text: str) -> set[str]:
    """The words in a query that a result has to have something to do with."""
    words = re.findall(r"[\wÀ-ɏ]+", text.lower())
    return {word for word in words if len(word) > 2 and word not in STOPWORDS}


def matches_query(result: "SearchResult") -> bool:
    """Does this result have anything to do with what was asked?

    Weak indexes answer a query by matching one word out of it. Measured:
    "best mechanical keyboards in turkey under 5000 tl" returned a radio
    station called Best FM, three dictionary entries for "best", and
    Wikipedia on mechanical engineering. "mekanik klavye tavsiye" returned
    four pizza chains.

    So: two of the query's content words have to appear somewhere in the
    title, the snippet or the URL -- one word is what the junk matches on, two
    is already about the right subject. Not a proportion: a nine-word question
    asked of a small index would then need three matches and keep nothing,
    which is how this filter first emptied a search completely.

    It is a crude test and it is meant to be. It removes results about a
    different subject entirely and leaves the judgement of what is *useful* to
    the reader agents, which is their job.
    """
    wanted = content_words(result.query)
    if len(wanted) < 2:
        return True  # too short to judge; let the reader decide
    haystack = f"{result.title} {result.snippet} {result.url}".lower()
    needed = 1 if len(wanted) <= 3 else 2
    hits = 0
    for word in wanted:
        # A page about a keyboard answers a question about keyboards. Matching
        # on a substring already covers the other direction (a query for
        # "klavye" finds "klavyesi"); this covers the plural asking after the
        # singular, which is how people write queries.
        stem = word[:-1] if len(word) > 4 and word.endswith("s") else word
        if stem in haystack:
            hits += 1
            if hits >= needed:
                return True
    return False


def dedupe_and_cap(
    results: list[SearchResult],
    limit: int,
    per_domain: int = 3,
) -> SearchOutcome:
    """Cheap, LLM-free triage: drop duplicates, noise domains, and domain floods.

    Per-domain capping matters more than it sounds: without it a single
    documentation site or content farm can occupy half the budget and the report
    ends up reflecting one source's opinion.
    """
    outcome = SearchOutcome(raw_count=len(results))
    seen: dict[str, SearchResult] = {}
    dropped = {"duplicate": 0, "noise": 0, "off_topic": 0, "domain_cap": 0, "unusable": 0}

    for result in results:
        if not result.url.startswith(("http://", "https://")) or not result.title.strip():
            dropped["unusable"] += 1
            continue
        key = canonical_url(result.url)
        if key in seen:
            dropped["duplicate"] += 1
            # Keep whichever engine scored it higher.
            if result.score > seen[key].score:
                seen[key].score = result.score
            continue
        if any(result.domain == bad or result.domain.endswith("." + bad) for bad in NOISE_DOMAINS):
            dropped["noise"] += 1
            continue
        if not matches_query(result):
            dropped["off_topic"] += 1
            continue
        result.url = key
        seen[key] = result

    ordered = sorted(seen.values(), key=lambda r: r.score, reverse=True)

    kept: list[SearchResult] = []
    per_domain_count: dict[str, int] = {}
    overflow: list[SearchResult] = []
    for result in ordered:
        count = per_domain_count.get(result.domain, 0)
        if count >= per_domain:
            overflow.append(result)
            continue
        per_domain_count[result.domain] = count + 1
        kept.append(result)

    # A short budget may relax the cap, but never abandon it. Letting the whole
    # overflow back in is how one site supplied 14 of 20 candidates in a real
    # run: the search was thin, the cap gave up, and MDN filled the shortfall
    # with documentation nobody asked for. Doubling is enough to top up a thin
    # search; past that, fewer sources beats one source repeated.
    if len(kept) < limit and overflow:
        readmitted: list[SearchResult] = []
        needed = limit - len(kept)
        for result in overflow:
            if len(readmitted) >= needed:
                break
            if per_domain_count.get(result.domain, 0) >= per_domain * 2:
                continue
            per_domain_count[result.domain] = per_domain_count.get(result.domain, 0) + 1
            readmitted.append(result)
        kept.extend(readmitted)
        dropped["domain_cap"] = len(overflow) - len(readmitted)
    else:
        dropped["domain_cap"] = len(overflow)

    outcome.results = kept[:limit]
    outcome.dropped = {k: v for k, v in dropped.items() if v}
    return outcome
