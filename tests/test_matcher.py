"""
Regression suite for the years-of-experience parser and the scoring rules around it.

Every phrase below was copied from a real LinkedIn posting the scanner stored (or is a
common variant of one), with the number of years the posting actually asks for.
"""
from __future__ import annotations

import unittest

from app.matcher import (
    ResumeProfile,
    required_years,
    required_years_mention,
    score_job,
    senior_title_term,
)
from app.models import Job

# Built directly so the test does not depend on resume.json or on today's date.
PROFILE = ResumeProfile(
    name="test",
    years_experience=2.4,
    target_titles=("python developer",),
    core_skills={"Python": ("python",), "FastAPI": ("fastapi",)},
    secondary_skills={},
    years_tolerance=0,
)

MENDIX = (
    "Required qualifications include 3–5 years of software engineering or automation experience, "
    "with 1–3 years of hands-on Mendix experience."
)
ARTECH = (
    "Python Developer(7+ Years & Immediate) About the job Experience: 7+ Years Location: Bangalore. "
    "Required Skills and Experience 7+ years software development or data engineering experience. "
    "Preferred Skills and Experience 5+ years with Python web frameworks"
)

# (text, years the posting asks for; None = it states none)
YEARS_CASES: list[tuple[str, int | None]] = [
    # A: a skill-specific sub-requirement must not override the general one
    (MENDIX, 3),
    (ARTECH, 7),
    (
        "Posted 721 days ago Experience Minimum 4 Year Work Level Senior Level Job Type Full Time. Graduate "
        "with at least 6+ years of minimum relevant experience in the technology stack. Over the past 5 years we grew.",
        4,
    ),
    # B: bare range with a unit and no "experience" word nearby (job-board header lines)
    ("Python developer Zensar Technologies Development FULLTIME 25000 - 30000 8 - 9 Years Ghaziabad Job Description", 8),
    ("Python Developer Qloron Pvt Ltd Telecaller FULLTIME 20000 - 25000 1 - 2 Years Ghaziabad Job Description", 1),
    ("Experience8 - 9 Years DegreeMaster", 8),
    # D: phrases that already parsed correctly and must keep doing so
    ("Minimum 6+ years of backend (Python) or full-stack software engineering experience", 6),
    ("At least 6+ years of experience as a Python Developer or similar role", 6),
    ("4–6 years of strong Python development experience", 4),
    ("Job Description Experience: 7-8 Years Location: Noida, Gurugram, Pune", 7),
    ("Professional Skills 7–10 years of experience in software/full-stack development", 7),
    ("8+ years of experience in business systems engineering, software engineering", 8),
    ("7+ yoe python backend developer with agentic ai | any ust location | immediate joiner", 7),
    ("Must-Have Skills 7+ years of experience in Python Backend Development", 7),
    ("Required Skills 5-8 years of hands-on Python development experience", 5),
    ("Must-Have Skills Strong experience in Core Python development (6–10 years) Strong SQL skills", 6),
    (
        "EXPERIENCE, LOCATION, MODE OF WORK Experience: 5-8 years Location: Mumbai Mode of Work: Hybrid "
        "Mandatory Skills SQL and Database Design (5+ years in enterprise data platforms)",
        5,
    ),
    ("Employment Type : Full-time Experience : 6+ years Notice Period : Immediate to 30 days", 6),
    ("About the job Experience: 5-10 Years Location: Bangalore Work Mode: Hybrid", 5),
    (
        "Contract Type: Freelance / Retainer (2 – 3 Hours / Day) Start Date: Immediate (Within 24–48 hours) "
        "Must-Haves: 8+ years of total software development experience as a senior engineer. "
        "Commitment: Dedicated 2 to 3 hours daily (Monday to Friday). Preferred daily 2–3 hour work slot",
        8,
    ),
    # E: numbers that are not experience requirements
    ("posted 721 days ago", None),
    ("About the job July 26, 2023 algorithus6723 Job Description", None),
    ("Founded in 2015, our core philosophy is built around a strong product engineering mindset", None),
    ("Senior Technologist - AI/GenAI/Python-Q3 FY 27", None),
    ("Senior Technologist Python Engineer-Q3 FY 26", None),
    ("Development FULLTIME 25000 - 30000 Ghaziabad", None),
    ("Contract Type: Freelance / Retainer (2 – 3 Hours / Day)", None),
    ("Start Date: Immediate (Within 24–48 hours)", None),
    ("Commitment: Dedicated 2 to 3 hours daily", None),
    ("25 minutes ago Within the past 24 hours", None),
    ("a 10 year old company with deep experience in fintech", None),
    ("Proficiency with Python (3.6+) including environment management", None),
    ("Notice Period : Immediate to 30 days", None),
    ("founded 5 years ago with experience across domains", None),
    ("Experience: 6 months", None),
    ("Salary 3-5 LPA based on experience", None),
    ("We are seeking a highly skilled and experienced Python Developer with a proven track record", None),
    # F: common forms not seen in the corpus yet
    ("minimum 5 years", 5),
    ("at least 5 years in Django", 5),
    ("over 5 years", 5),
    ("more than 5 years", 5),
    ("5 years minimum", 5),
    ("5+ yrs exp", 5),
    ("Experience: 3-5", 3),
    ("Exp: 3+", 3),
    ("Years of experience: 3-5", 3),
    ("3 to 5 years", 3),
    ("3—5 years", 3),
    ("3~5 years", 3),
    ("between 3 and 5 years", 3),
    ("fresher", 0),
    ("Freshers welcome", 0),
    ("0-1 years", 0),
    ("Freshers please do not apply. 5+ years required", 5),
    ("2.5+ years of experience", 2),
    ("Must have 2+ years with FastAPI; overall experience: 5+ years", 5),
]


class RequiredYearsTests(unittest.TestCase):
    def test_required_years(self) -> None:
        for text, want in YEARS_CASES:
            with self.subTest(text=text):
                self.assertEqual(required_years(text), want)

    def test_first_mention_wins_over_a_later_sub_requirement(self) -> None:
        mention = required_years_mention(MENDIX)
        self.assertEqual(mention.years, 3)
        self.assertFalse(mention.labeled)
        self.assertTrue(mention.phrase.startswith("3–5 years of software engineering"))

    def test_labeled_mention_beats_an_earlier_unlabeled_one(self) -> None:
        mention = required_years_mention("Must have 2+ years with FastAPI; overall experience: 5+ years")
        self.assertEqual(mention.years, 5)
        self.assertTrue(mention.labeled)

    def test_phrase_keeps_the_postings_wording(self) -> None:
        mention = required_years_mention(ARTECH)
        self.assertEqual(mention.years, 7)
        self.assertTrue(mention.phrase.startswith("Experience: 7+ Years"))

    def test_fresher_is_only_a_fallback(self) -> None:
        self.assertEqual(required_years_mention("Freshers welcome").years, 0)
        self.assertIsNone(required_years_mention("no requirement mentioned"))


class SeniorTitleTests(unittest.TestCase):
    def test_senior_terms(self) -> None:
        for title, want in [
            ("senior python developer", "senior"),
            ("sr. python backend developer", "sr"),
            ("sr data scientist ii", "sr"),
            ("tech lead - python developer", "lead"),
            ("principal engineer", "principal"),
            ("solutions architect", "architect"),
            ("python developer", None),
            ("python developer - leadership team", None),
            ("python staffing coordinator", None),
            ("software architecture python", None),
        ]:
            with self.subTest(title=title):
                self.assertEqual(senior_title_term(title), want)


class ScoreJobTests(unittest.TestCase):
    def _job(self, title: str, description: str | None) -> Job:
        job = Job(job_id="1", title=title, url="https://example.test/1", description=description)
        score_job(job, PROFILE)
        return job

    def test_title_only_job_is_scored(self) -> None:
        job = self._job("Python Developer (7+ Years)", None)
        self.assertIsInstance(job.match_score, int)
        self.assertEqual(job.required_years, 7)
        self.assertIs(job.experience_fit, False)
        self.assertIn("title only, no description fetched", job.match_notes)

    def test_deciding_phrase_is_quoted_in_the_notes(self) -> None:
        job = self._job("Python Developer", ARTECH + " python fastapi")
        self.assertIs(job.experience_fit, False)
        self.assertIn("«Experience: 7+ Years", job.match_notes)

    def test_sub_requirement_no_longer_lets_a_senior_posting_through(self) -> None:
        job = self._job("Python Developer", MENDIX + " python")
        self.assertEqual(job.required_years, 3)
        self.assertIs(job.experience_fit, False)

    def test_senior_title_without_years_does_not_fit(self) -> None:
        job = self._job("Senior Python Developer", "We want a python expert with fastapi and strong design skills.")
        self.assertIsNone(job.required_years)
        self.assertIs(job.experience_fit, False)
        self.assertIn("title says 'senior'", job.match_notes)

    def test_senior_title_trusts_stated_years(self) -> None:
        job = self._job("Senior Python Engineer", "DPDP Consultants FULLTIME 15000 - 20000 1 - 2 Years Ghaziabad python")
        self.assertEqual(job.required_years, 1)
        self.assertIs(job.experience_fit, True)

    def test_no_years_and_plain_title_still_fits(self) -> None:
        job = self._job("Python Developer", "highly skilled and experienced python developer, fastapi")
        self.assertIsNone(job.required_years)
        self.assertIs(job.experience_fit, True)
        self.assertIn("no years stated", job.match_notes)


if __name__ == "__main__":
    unittest.main()
