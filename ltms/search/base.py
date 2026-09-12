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
    dropped = {"duplicate": 0, "noise": 0, "domain_cap": 0, "unusable": 0}

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

    # Only enforce the domain cap while we have alternatives; if the cap would
    # leave us short of the budget, let the overflow back in.
    if len(kept) < limit and overflow:
        needed = limit - len(kept)
        kept.extend(overflow[:needed])
        dropped["domain_cap"] = len(overflow) - needed if len(overflow) > needed else 0
    else:
        dropped["domain_cap"] = len(overflow)

    outcome.results = kept[:limit]
    outcome.dropped = {k: v for k, v in dropped.items() if v}
    return outcome
