"""Score a job posting against the candidate's resume profile (``resume.json``).

The score is a plain, explainable 0-100 number built from four parts:

* title       (0-20)  the job title contains one of the profile's target titles
* core skills (0-45)  share of ``core_skills`` mentioned in the title + description
* secondary   (0-20)  ``secondary_skills`` mentioned, capped at SECONDARY_CAP of them
* experience  (-15..15) how the years the posting asks for compare to the candidate's

No network or AI service is involved, so scoring is free, offline and repeatable.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import Job

log = logging.getLogger(__name__)

TITLE_POINTS = 20
CORE_POINTS = 45
SECONDARY_POINTS = 20
SECONDARY_CAP = 6  # this many secondary hits earns the full SECONDARY_POINTS
EXPERIENCE_UNKNOWN_POINTS = 8  # neutral credit when the posting names no years
TOO_SENIOR_CAP = 25  # a posting asking for more experience than the candidate has can score at most this
DEFAULT_YEARS_TOLERANCE = 1.0  # "3+ years" is still fine for someone with 2.4

# "3+ years", "2-4 yrs", "3 to 5 years", "7+ yoe" -> group(1) is the lower bound.
_NUM_RANGE = r"(\d{1,2})(?:\s*\+)?(?:\s*(?:-|–|to)\s*(?:\d{1,2}))?\s*\+?\s*"
_YEARS = r"(?:years?|yrs?)"
_YOE = r"(?:y\.?o\.?e\.?)(?![a-z])"  # "yoe" already means years of experience
_YEARS_PATTERNS = (
    re.compile(_NUM_RANGE + _YEARS + r"[^.\n]{0,60}?\b(?:experience|exp)\b", re.IGNORECASE),  # "3+ years of experience"
    re.compile(r"\b(?:experience|exp)\b[^.\n]{0,60}?" + _NUM_RANGE + _YEARS, re.IGNORECASE),  # "experience of 3 years"
    re.compile(r"(\d{1,2})\s*\+\s*" + _YEARS, re.IGNORECASE),  # a bare "3+ years" is nearly always a requirement
    re.compile(_NUM_RANGE + _YOE, re.IGNORECASE),  # "7+ yoe", "5-8 YOE"
)


class ResumeProfileError(RuntimeError):
    pass


def years_since(value: str, today: datetime | None = None) -> float:
    """'2024-05' or '2024-05-15' -> years elapsed, one decimal (2.4 for May 2024 in Sep 2026)."""
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            start = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return round(((today or datetime.now()) - start).days / 365.25, 1)
    raise ResumeProfileError(f"experience_since must be YYYY-MM or YYYY-MM-DD, got {value!r}")


@dataclass(frozen=True)
class ResumeProfile:
    name: str
    years_experience: float
    target_titles: tuple[str, ...]
    core_skills: dict[str, tuple[str, ...]]  # canonical name -> aliases a posting might use
    secondary_skills: dict[str, tuple[str, ...]]
    years_tolerance: float = DEFAULT_YEARS_TOLERANCE  # postings may ask up to this many years more

    @classmethod
    def load(cls, path: Path) -> "ResumeProfile":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ResumeProfileError(f"Resume profile not found: {path}") from None
        except json.JSONDecodeError as exc:
            raise ResumeProfileError(f"Resume profile {path} is not valid JSON: {exc}") from exc

        # "experience_since" keeps the number growing as time passes; "years_experience" is a fixed fallback.
        since = raw.get("experience_since")
        years = years_since(str(since)) if since else float(raw.get("years_experience") or 0)

        def skills(key: str) -> dict[str, tuple[str, ...]]:
            block = raw.get(key) or {}
            if not isinstance(block, dict):
                raise ResumeProfileError(f"{path}: '{key}' must be an object of {{skill: [aliases]}}")
            return {
                name: tuple(a.lower() for a in ([aliases] if isinstance(aliases, str) else aliases) if a)
                for name, aliases in block.items()
            }

        profile = cls(
            name=str(raw.get("name") or "candidate"),
            years_experience=years,
            target_titles=tuple(t.lower() for t in raw.get("target_titles") or []),
            core_skills=skills("core_skills"),
            secondary_skills=skills("secondary_skills"),
            years_tolerance=float(raw.get("years_tolerance", DEFAULT_YEARS_TOLERANCE)),
        )
        if not profile.core_skills:
            raise ResumeProfileError(f"{path}: 'core_skills' is empty, nothing to match against")
        return profile


def _mentions(text: str, alias: str) -> bool:
    """True when ``alias`` appears in ``text`` as a whole word/phrase ("sql" must not hit "postgresql")."""
    return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) is not None


def _matched(text: str, skills: dict[str, tuple[str, ...]]) -> list[str]:
    return [name for name, aliases in skills.items() if any(_mentions(text, a) for a in aliases)]


def required_years(text: str) -> int | None:
    """Lowest number of years of experience a posting asks for, or None when it names none."""
    lows = [
        int(m.group(1))
        for pattern in _YEARS_PATTERNS
        for m in pattern.finditer(text)
        if int(m.group(1)) <= 20  # "founded 25 years ago" is not a requirement
    ]
    return min(lows) if lows else None


def experience_fit(required: int | None, have: float, tolerance: float) -> tuple[bool, int, str]:
    """(fits, points, note). A posting fits when it asks for at most ``have + tolerance`` years."""
    if required is None:
        return True, EXPERIENCE_UNKNOWN_POINTS, "no years stated"
    if required <= have:
        return True, 15, f"asks {required}+ yrs"
    if required <= have + tolerance:
        return True, 10, f"asks {required}+ yrs (close to your {have:g})"
    return False, 0, f"asks {required}+ yrs, above your {have:g} (+{tolerance:g} allowed)"


def verdict_for(score: int) -> str:
    if score >= 70:
        return "strong"
    if score >= 50:
        return "good"
    if score >= 30:
        return "weak"
    return "poor"


def score_job(job: Job, profile: ResumeProfile) -> None:
    """Fill ``job.match_*`` in place from its title and (when fetched) description."""
    title = (job.title or "").lower()
    description = (job.description or "").lower()
    text = f"{title} {description}"
    notes: list[str] = []

    title_ok = any(_mentions(title, t) for t in profile.target_titles)
    title_points = TITLE_POINTS if title_ok else 0

    core_hits = _matched(text, profile.core_skills)
    core_points = round(CORE_POINTS * len(core_hits) / len(profile.core_skills))

    secondary_hits = _matched(text, profile.secondary_skills)
    secondary_points = round(SECONDARY_POINTS * min(len(secondary_hits), SECONDARY_CAP) / SECONDARY_CAP)

    years = required_years(text)
    fits, experience_points, experience_note = experience_fit(years, profile.years_experience, profile.years_tolerance)
    notes.append(experience_note)

    score = title_points + core_points + secondary_points + experience_points
    if not fits:
        score = min(score, TOO_SENIOR_CAP)  # skills cannot outweigh a hard experience requirement
    if not description:
        notes.append("title only, no description fetched")
    elif not any(_mentions(description, a) for a in ("python", "fastapi", "fast api", "django")):
        # Checked on the description alone: the title's own "Python" must not vouch for a Java ad.
        score = min(score, 30)
        notes.append("description never mentions Python")

    job.match_score = max(0, min(100, score))
    job.match_verdict = verdict_for(job.match_score)
    job.matched_skills = core_hits + secondary_hits
    job.missing_skills = [name for name in profile.core_skills if name not in core_hits]
    job.required_years = years
    job.experience_fit = fits
    job.match_notes = "; ".join(notes)


def score_jobs(jobs: list[Job], profile: ResumeProfile) -> None:
    for job in jobs:
        score_job(job, profile)
        log.debug("Score %s (%s): %s | %s", job.match_score, job.match_verdict, job.title, job.match_notes)
