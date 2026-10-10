"""
Score a job posting against the candidate's resume profile (``resume.json``).

The score is a plain, explainable 0-100 number built from four parts:

* title       (0-20)  the job title contains one of the profile's target titles
* core skills (0-45)  share of ``core_skills`` mentioned in the title + description
* secondary   (0-20)  ``secondary_skills`` mentioned, capped at SECONDARY_CAP of them
* experience  (0-15)  how the years the posting asks for compare to the candidate's

The years a posting asks for are read from phrases such as "3+ years of experience",
"Experience: 5-8 years" or "8 - 9 Years". The first phrase in the text decides, except
that an explicitly labelled one ("Experience:", "Minimum", "Total") beats an unlabelled
one, so "1-3 years of Mendix" cannot override "3-5 years of software engineering".
The deciding phrase is quoted in ``match_notes``. A posting asking for more years than
the candidate has (plus tolerance) does not fit; neither does a Senior/Lead title that
states no years at all.

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
MAX_YEARS = 20  # "founded 25 years ago" is not a requirement
LABEL_WINDOW = 30  # an explicit label ("Experience:", "Minimum") must end within this many chars before the number
PHRASE_MAX = 60  # how much of the deciding phrase match_notes quotes

# A title with one of these and no years stated is treated as asking for more experience than the candidate has.
# "sr" covers "Sr." and "Sr Data" through _mentions' word boundaries; "lead" does not hit "leadership".
SENIOR_TITLE_TERMS = ("senior", "sr", "lead", "principal", "staff", "architect", "head of", "manager", "director")

# Building blocks for the years patterns. group(1) is always the lower bound.
_NUM = r"(?<!\d)(?<!past )(?<!last )(?<!next )(\d{1,2})(?:\.\d)?(?!\d)"  # not inside "25000"; not "past 5 years"; "2.5" -> 2
_NUM2 = r"(?<!\d)\d{1,2}(?:\.\d)?(?!\d)"  # upper bound of a range, not captured
_SEP = r"\s*(?:[-–—−‑~]|to)\s*"  # "3-5", "3–5", "3—5", "3~5", "3 to 5"
_RANGE = _NUM + r"(?:\s*\+)?(?:" + _SEP + _NUM2 + r")?\s*\+?\s*"  # "3", "3+", "3-5", "3 - 5+"
_YEARS = r"(?:years?|yrs?\.?)(?![a-z])(?!\s*-?\s*(?:old|ago)\b)"  # the unit; rejects "10 year old", "5 years ago"
_YOE = r"(?:y\.?o\.?e\.?)(?![a-z])"  # "yoe" already means years of experience
_EXP = r"(?<![a-z])(?:experience|exp)(?![a-z])"  # letter boundaries, so "Experience8 - 9 Years" still matches
_QUALIFIER = r"(?<![a-z])(?:minimum|min\.?|at\s*least|atleast|over|more\s+than|above|upwards\s+of)\s+"
_NOT_OTHER_UNIT = r"(?!\s*(?:months?|mos?|weeks?|days?|hours?|hrs?|%|k(?![a-z])|lpa|lakhs?|lacs?))"

_YEARS_PATTERNS = (
    re.compile(_RANGE + _YEARS + r"[^.\n]{0,60}?" + _EXP, re.IGNORECASE),  # "3+ years of ... experience"
    re.compile(_EXP + r"[^.\n]{0,60}?" + _RANGE + _YEARS, re.IGNORECASE),  # "Experience: 7-8 Years", "experience of 3 years"
    re.compile(_NUM + r"\s*\+\s*" + _YEARS, re.IGNORECASE),  # a bare "7+ years" is nearly always a requirement
    re.compile(_RANGE + _YOE, re.IGNORECASE),  # "7+ yoe", "5-8 YOE"
    re.compile(_NUM + _SEP + _NUM2 + r"\s*\+?\s*" + _YEARS, re.IGNORECASE),  # bare range with unit: "8 - 9 Years"
    re.compile(r"between\s+" + _NUM + r"\s+(?:and|to)\s+" + _NUM2 + r"\s*" + _YEARS, re.IGNORECASE),
    re.compile(_QUALIFIER + _RANGE + _YEARS, re.IGNORECASE),  # "minimum 5 years", "at least 5 years in Django"
    re.compile(_NUM + r"\s*" + _YEARS + r"\s*(?:minimum|min\.?|or\s+more|and\s+above|\+)", re.IGNORECASE),  # "5 years minimum"
    re.compile(r"(?:years?\s+of\s+)?" + _EXP + r"\s*[:\-–—]\s*" + _RANGE + _NOT_OTHER_UNIT, re.IGNORECASE),  # "Experience: 3-5"
)
_FRESHER_RE = re.compile(r"(?<![a-z])freshers?(?![a-z])", re.IGNORECASE)  # -> 0 years, only when no number is stated
# Applied to the LABEL_WINDOW chars before a number; the $ anchor means the label sits right before it.
_LABEL_RE = re.compile(
    r"(?<![a-z])(?:(?:total|overall|minimum|min\.?|at\s*least|atleast|required|requirements?)\s*:?"
    r"|(?:years?\s+of\s+)?(?:experience|exp)\s*(?:level|required|range|needed)?\s*(?:[:\-–—]|of|is)?)\s*(?:of\s+)?$",
    re.IGNORECASE,
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


def senior_title_term(title: str) -> str | None:
    """The first SENIOR_TITLE_TERMS entry the (lowercased) title mentions, or None."""
    return next((term for term in SENIOR_TITLE_TERMS if _mentions(title, term)), None)


@dataclass(frozen=True)
class YearsMention:
    years: int  # lower bound the phrase states
    phrase: str  # the text that stated it, e.g. "Experience: 7+ Years"
    labeled: bool  # an explicit label ("Experience:", "Minimum", "Total") sits right before the number
    position: int  # offset of the number in the text, i.e. document order (the title comes first)


def years_mentions(text: str) -> list[YearsMention]:
    """Every years-of-experience phrase in ``text``, in document order, one per number."""
    by_position: dict[int, YearsMention] = {}
    for pattern in _YEARS_PATTERNS:
        for match in pattern.finditer(text):
            years = int(match.group(1))
            if years > MAX_YEARS:
                continue
            position = match.start(1)
            window_start = max(0, position - LABEL_WINDOW)
            label = _LABEL_RE.search(text[window_start:position])
            start = min(match.start(), window_start + label.start()) if label else match.start()
            mention = YearsMention(years, text[start:match.end()], label is not None, position)
            # Several patterns often hit the same number; keep the one that quotes the most context.
            if position not in by_position or len(mention.phrase) > len(by_position[position].phrase):
                by_position[position] = mention
    return sorted(by_position.values(), key=lambda m: m.position)


def required_years_mention(text: str) -> YearsMention | None:
    """
    The phrase that decides how many years the posting asks for, or None when it names none.

    The first labelled mention wins, else the first mention of any kind, else "fresher" counts as 0.
    """
    mentions = years_mentions(text)
    if mentions:
        return next((m for m in mentions if m.labeled), mentions[0])
    fresher = _FRESHER_RE.search(text)
    return YearsMention(0, fresher.group(0), False, fresher.start()) if fresher else None


def required_years(text: str) -> int | None:
    """Number of years of experience a posting asks for (lower bound), or None when it names none."""
    mention = required_years_mention(text)
    return mention.years if mention else None


def _quote(phrase: str) -> str:
    phrase = " ".join(phrase.split())
    return f"«{phrase}»" if len(phrase) <= PHRASE_MAX else f"«{phrase[:PHRASE_MAX - 1]}…»"


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
    """
    Fill ``job.match_*`` in place from its title and (when fetched) description.
    """
    raw_text = f"{job.title or ''} {job.description or ''}"  # original case, so the quoted phrase reads as posted
    title = (job.title or "").lower()
    description = (job.description or "").lower()
    text = raw_text.lower()
    notes: list[str] = []

    title_ok = any(_mentions(title, t) for t in profile.target_titles)
    title_points = TITLE_POINTS if title_ok else 0

    core_hits = _matched(text, profile.core_skills)
    core_points = round(CORE_POINTS * len(core_hits) / len(profile.core_skills))

    secondary_hits = _matched(text, profile.secondary_skills)
    secondary_points = round(SECONDARY_POINTS * min(len(secondary_hits), SECONDARY_CAP) / SECONDARY_CAP)

    mention = required_years_mention(raw_text)
    years = mention.years if mention else None
    fits, experience_points, experience_note = experience_fit(years, profile.years_experience, profile.years_tolerance)
    if years is None and (term := senior_title_term(title)):
        fits, experience_points, experience_note = False, 0, f"no years stated, title says {term!r}"
    if mention:
        experience_note += f" ({_quote(mention.phrase)})"
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
