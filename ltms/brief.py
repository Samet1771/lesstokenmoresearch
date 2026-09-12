"""The research brief.

The calling agent already knows why it wants something researched and which
wording will find it. Asking a small local model to guess those queries was the
weakest stage in the pipeline, so the agent writes them down instead:

    # postgres logical replication lag

    ## queries
    - postgres logical replication lag causes
    - postgres wal sender bottleneck high write volume

    ## questions
    - What makes lag grow under heavy writes?

    ## notes
    Prefer official docs and mailing list threads over blog posts.

Only the queries are required, and the parser is deliberately forgiving: a file
that is nothing but a list of lines still works.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
FENCE = re.compile(r"^\s*(```|~~~)")

QUERY_SECTIONS = {"queries", "query", "searches", "search", "aramalar", "sorgular"}
QUESTION_SECTIONS = {"questions", "question", "sorular", "answer", "answers"}
NOTE_SECTIONS = {"notes", "note", "instructions", "guidance", "notlar", "talimatlar"}

TEMPLATE = """# <topic in a few words>

## queries
- <a search someone would actually type>
- <another angle on the same subject>
- <the official documentation angle>
- <the failure mode / criticism angle>

## questions
- <what you actually need answered>

## notes
<optional: which kinds of sources to prefer or avoid>
"""


class BriefError(ValueError):
    pass


@dataclass
class Brief:
    topic: str
    queries: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    notes: str = ""
    path: Path | None = None

    @property
    def instructions(self) -> str:
        """What the extractor and editor agents are steered by."""
        parts: list[str] = []
        if self.questions:
            parts.append("Questions to answer:\n" + "\n".join(f"- {q}" for q in self.questions))
        if self.notes:
            parts.append(self.notes)
        return "\n\n".join(parts)


def _section_key(title: str) -> str:
    return re.sub(r"[^a-z]+", "", title.lower())


def _clean(line: str) -> str:
    return BULLET.sub("", line).strip().strip("`").strip()


def parse(text: str, fallback_topic: str = "") -> Brief:
    topic = ""
    section = ""
    buckets: dict[str, list[str]] = {"queries": [], "questions": [], "notes": []}
    loose: list[str] = []
    in_fence = False

    for raw in text.splitlines():
        if FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        line = raw.rstrip()
        if not line.strip():
            continue

        heading = HEADING.match(line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            key = _section_key(title)
            if key in QUERY_SECTIONS:
                section = "queries"
            elif key in QUESTION_SECTIONS:
                section = "questions"
            elif key in NOTE_SECTIONS:
                section = "notes"
            else:
                # An unrecognised heading -- the first one names the topic.
                section = ""
                if not topic and level <= 2:
                    topic = title
            continue

        if line.strip().startswith(("#", "//")) and not heading:
            continue  # a comment, not a heading

        value = _clean(line)
        if not value:
            continue
        if section == "notes":
            buckets["notes"].append(value)
        elif section:
            buckets[section].append(value)
        else:
            loose.append(value)

    queries = buckets["queries"]
    if not queries:
        # No `## queries` section: treat every loose line as a query. This is
        # what a hand-written one-line-per-search file looks like.
        queries = loose

    # Preserve order, drop repeats and anything too short to be a real search.
    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        folded = query.lower()
        if folded in seen or len(query) < 3:
            continue
        seen.add(folded)
        unique.append(query)

    if not topic:
        topic = fallback_topic or (unique[0] if unique else "")

    if not unique:
        raise BriefError(
            "the brief has no queries.\n"
            "Write one search per line, or put them under a '## queries' heading.\n"
            "Run `ltms template` for a starting point."
        )

    return Brief(
        topic=topic,
        queries=unique,
        questions=buckets["questions"],
        notes="\n".join(buckets["notes"]).strip(),
    )


def load(path: Path) -> Brief:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BriefError(f"cannot read {path}: {error}") from error
    brief = parse(text, fallback_topic=path.stem.replace("-", " ").replace("_", " "))
    brief.path = path
    return brief


def from_topic(topic: str, count: int) -> Brief:
    """The quick inline form: one subject, expanded along a few plain angles.

    No model involved. These are worse than queries a caller writes by hand,
    which is exactly why the brief file is the recommended path.
    """
    angles = [
        "",
        "documentation",
        "benchmark data",
        "problems limitations",
        "comparison alternatives",
        "best practices",
        "case study",
        "how it works internally",
        "official specification",
        "known issues",
        "performance tuning",
        "migration experience",
    ]
    queries: list[str] = []
    for angle in angles:
        query = f"{topic} {angle}".strip()
        if query not in queries:
            queries.append(query)
        if len(queries) >= count:
            break
    return Brief(topic=topic, queries=queries)


BRIEF_SUFFIXES = {".md", ".markdown", ".txt"}


def looks_like_brief(argument: str) -> bool:
    """Is this argument a path to a brief rather than a topic to search?"""
    if not argument or "\n" in argument:
        return False
    candidate = Path(argument)
    return candidate.suffix.lower() in BRIEF_SUFFIXES and candidate.is_file()


def missing_brief(argument: str) -> bool:
    """Was a brief clearly meant, but the file is not there?

    Without this a mistyped path is silently researched as though it were the
    topic -- the caller gets a plausible-looking report about nothing, which is
    worse than an error.
    """
    if not argument or "\n" in argument:
        return False
    candidate = Path(argument)
    if candidate.is_file():
        return False
    looks_like_path = "/" in argument or "\\" in argument
    return candidate.suffix.lower() in BRIEF_SUFFIXES or looks_like_path
