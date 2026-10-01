"""
Search criteria, LinkedIn URL building and project paths.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlencode

BASE_DIR = Path(__file__).resolve().parent.parent
BROWSER_DATA_DIR = Path(os.environ.get("JOB_AGENT_BROWSER_DIR", BASE_DIR / "data" / "browser"))
OUTPUT_DIR = BASE_DIR / "data" / "jobs"
LOGS_DIR = BASE_DIR / "logs"
RESUME_PATH = BASE_DIR / "resume.json"  # candidate profile used to score jobs (see app/matcher.py)

LINKEDIN_JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"
PAGE_SIZE = 25  # LinkedIn shows 25 job cards per results page

# One LinkedIn search is run per entry; results are merged by job id.
DEFAULT_KEYWORDS: tuple[str, ...] = ("Python Developer", "FastAPI Developer")
# A job is kept when its title contains at least one of these (case-insensitive).
DEFAULT_TITLE_TERMS: tuple[str, ...] = ("python", "fastapi", "fast api", "django")

WATCH_INTERVAL_SECONDS = 300  # --watch polls LinkedIn this often
DEFAULT_MIN_SCORE = 50  # notify only for jobs scoring at least this (0-100) against the resume


class ExperienceLevel(str, Enum):
    """Values of LinkedIn's ``f_E`` filter (the "Experience level" dropdown)."""

    INTERNSHIP = "1"
    ENTRY_LEVEL = "2"
    ASSOCIATE = "3"
    MID_SENIOR_LEVEL = "4"  # LinkedIn's label for "Senior level"
    DIRECTOR = "5"
    EXECUTIVE = "6"


class PostedWithin(str, Enum):
    """Values of LinkedIn's ``f_TPR`` filter (the "Date posted" dropdown)."""

    ANY_TIME = ""
    PAST_24_HOURS = "r86400"
    PAST_WEEK = "r604800"
    PAST_MONTH = "r2592000"


class SortBy(str, Enum):
    MOST_RECENT = "DD"
    MOST_RELEVANT = "R"


@dataclass(frozen=True)
class SearchCriteria:
    """What to search for. Defaults match the current target:

    Python Developer, posted in the past 24 hours, Entry level + Senior level,
    under 10 applicants.
    """

    keywords: str = DEFAULT_KEYWORDS[0]
    posted_within: PostedWithin = PostedWithin.PAST_24_HOURS
    experience_levels: tuple[ExperienceLevel, ...] = (
        ExperienceLevel.ENTRY_LEVEL,
        ExperienceLevel.MID_SENIOR_LEVEL,
    )
    under_10_applicants: bool = True
    easy_apply_only: bool = False
    location: str | None = None  # None = LinkedIn's default (your profile location)
    sort_by: SortBy = SortBy.MOST_RECENT
    # LinkedIn's keyword search is fuzzy ("Python Developer" also returns
    # "Lead Infrastructure Engineer"), so results are post-filtered on title.
    # At least one term in ``title_any_of`` has to appear (case-insensitive);
    # an empty tuple keeps every result.
    title_any_of: tuple[str, ...] = DEFAULT_TITLE_TERMS
    title_must_not_contain: tuple[str, ...] = ()
    max_pages: int = 10

    def search_url(self, start: int = 0) -> str:
        params: dict[str, str] = {"keywords": self.keywords}
        if self.posted_within.value:
            params["f_TPR"] = self.posted_within.value
        if self.experience_levels:
            params["f_E"] = ",".join(level.value for level in self.experience_levels)
        if self.under_10_applicants:
            params["f_EA"] = "true"
        if self.easy_apply_only:
            params["f_AL"] = "true"
        if self.location:
            params["location"] = self.location
        params["sortBy"] = self.sort_by.value
        if start:
            params["start"] = str(start)
        return f"{LINKEDIN_JOBS_SEARCH_URL}?{urlencode(params)}"

    def title_matches(self, title: str | None) -> bool:
        if not title:
            return False
        lowered = title.lower()
        if self.title_any_of and not any(term.lower() in lowered for term in self.title_any_of):
            return False
        return not any(term.lower() in lowered for term in self.title_must_not_contain)

    def describe(self) -> str:
        levels = ", ".join(level.name.replace("_", " ").title() for level in self.experience_levels) or "any"
        parts = [
            f"keywords='{self.keywords}'",
            f"posted={self.posted_within.name.replace('_', ' ').lower()}",
            f"experience={levels}",
            f"under_10_applicants={self.under_10_applicants}",
        ]
        if self.easy_apply_only:
            parts.append("easy_apply_only=True")
        if self.location:
            parts.append(f"location='{self.location}'")
        if self.title_any_of:
            parts.append(f"title_any_of={list(self.title_any_of)}")
        return ", ".join(parts)
