"""Job record plus small parsers for the text LinkedIn shows on cards and detail panes."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

_VERIFICATION_SUFFIX = re.compile(r"\s+with verification\s*$", re.IGNORECASE)
_POSTED_RE = re.compile(r"\b(\d+)\s+(second|minute|hour|day|week|month)s?\s+ago\b", re.IGNORECASE)
_WORKPLACE_RE = re.compile(r"\s*\((On-site|Hybrid|Remote)\)\s*$", re.IGNORECASE)
_APPLICANTS_PATTERNS = (
    re.compile(r"(\d+)\s+(?:people|person)\s+clicked\s+apply", re.IGNORECASE),
    re.compile(r"(\d+)\s+applicants?\b", re.IGNORECASE),
)
_APPLICANTS_TEXT_RE = re.compile(
    r"(Be among the first \d+ applicants|Over \d+ applicants|\d+ applicants?|\d+ (?:people|person) clicked apply)",
    re.IGNORECASE,
)
_JOB_TYPES = ("Full-time", "Part-time", "Contract", "Temporary", "Internship", "Volunteer")


@dataclass
class Job:
    job_id: str
    title: str
    url: str
    company: str | None = None
    location: str | None = None
    workplace_type: str | None = None  # On-site / Hybrid / Remote
    posted_text: str | None = None  # e.g. "25 minutes ago"
    posted_at: str | None = None  # ISO timestamp estimated from posted_text
    applicants: int | None = None  # exact count when LinkedIn shows one
    applicants_text: str | None = None  # e.g. "7 people clicked apply"
    salary: str | None = None
    job_type: str | None = None  # Full-time, Contract, ...
    easy_apply: bool = False
    apply_label: str | None = None
    promoted: bool = False
    viewed: bool = False
    description: str | None = None
    page: int = 0
    rank: int = 0
    is_new: bool = True
    # Resume match (filled by app.matcher when a resume profile is configured).
    match_score: int | None = None  # 0-100
    match_verdict: str | None = None  # strong / good / weak / poor
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)  # core skills the posting never mentions
    required_years: int | None = None  # lower bound of experience the posting asks for
    experience_fit: bool | None = None  # False = asks for more years than the candidate has (+tolerance)
    match_notes: str | None = None
    scraped_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)


def clean_title(title: str | None) -> str:
    """
    LinkedIn appends ' with verification' to the accessible title of verified jobs.
    """
    if not title:
        return ""
    return _VERIFICATION_SUFFIX.sub("", title).strip()


def parse_posted(text: str | None, now: datetime | None = None) -> tuple[str | None, str | None]:
    """
    Return (posted_text, posted_at_iso) from text such as '25 minutes ago Within the past 24 hours'.

    Returns (None, None) when the text holds no relative time (e.g. a card footer
    that only says 'Viewed'), so the caller can fill it from the detail pane.
    """
    if not text:
        return None, None
    match = _POSTED_RE.search(text)
    if not match:
        return None, None
    amount, unit = int(match.group(1)), match.group(2).lower()
    delta = {
        "second": timedelta(seconds=amount),
        "minute": timedelta(minutes=amount),
        "hour": timedelta(hours=amount),
        "day": timedelta(days=amount),
        "week": timedelta(weeks=amount),
        "month": timedelta(days=30 * amount),
    }[unit]
    now = now or datetime.now()
    return match.group(0), (now - delta).isoformat(timespec="minutes")


def split_location(text: str | None) -> tuple[str | None, str | None]:
    """'Bengaluru, Karnataka, India (Hybrid)' -> ('Bengaluru, Karnataka, India', 'Hybrid')."""
    if not text:
        return None, None
    match = _WORKPLACE_RE.search(text)
    if not match:
        return text.strip(), None
    return text[: match.start()].strip(), match.group(1)


def parse_applicants(text: str | None) -> tuple[int | None, str | None]:
    """Return (exact_count_or_None, matched_phrase) from detail-pane text.

    LinkedIn phrases: '7 people clicked apply', '1 person clicked apply',
    '12 applicants', 'Be among the first 25 applicants', 'Over 100 applicants'.
    """
    if not text:
        return None, None
    phrase_match = _APPLICANTS_TEXT_RE.search(text)
    phrase = phrase_match.group(1) if phrase_match else None
    if phrase and re.match(r"(Be among the first|Over)\b", phrase, re.IGNORECASE):
        return None, phrase  # a bound, not an exact count
    for pattern in _APPLICANTS_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1)), phrase
    return None, phrase


def parse_job_type(pills: list[str]) -> str | None:
    for pill in pills:
        for job_type in _JOB_TYPES:
            if pill.lower().startswith(job_type.lower()):
                return job_type
    return None
