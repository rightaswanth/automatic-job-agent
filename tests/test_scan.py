"""``announceable``: which scored jobs raise a notification."""
from __future__ import annotations

import unittest

from app.models import Job
from app.scan import announceable


def _job(**overrides) -> Job:
    fields = dict(
        job_id="1", title="Python Developer", url="https://example.test/1",
        is_new=True, experience_fit=True, match_score=60, required_years=2,
    )
    fields.update(overrides)
    return Job(**fields)


class AnnounceableTests(unittest.TestCase):
    def test_scored_job_uses_min_score(self) -> None:
        self.assertTrue(announceable(_job(match_score=50), 50))
        self.assertFalse(announceable(_job(match_score=49), 50))

    def test_unknown_years_need_the_higher_bar(self) -> None:
        self.assertTrue(announceable(_job(match_score=86, required_years=None), 50, 70))
        self.assertFalse(announceable(_job(match_score=63, required_years=None), 50, 70))
        self.assertTrue(announceable(_job(match_score=63, required_years=2), 50, 70))

    def test_not_new_or_not_fitting_is_never_announced(self) -> None:
        self.assertFalse(announceable(_job(is_new=False, match_score=99), 50))
        self.assertFalse(announceable(_job(experience_fit=False, match_score=99), 50))

    def test_unscored_job_means_no_resume_profile(self) -> None:
        self.assertTrue(announceable(_job(match_score=None, experience_fit=None, required_years=None), 50))


if __name__ == "__main__":
    unittest.main()
