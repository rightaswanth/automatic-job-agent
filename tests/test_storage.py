"""``mark_seen``: new-job detection and the bounded retry of jobs whose detail pane did not load."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.models import Job
from app.storage import MAX_DETAIL_ATTEMPTS, load_pending_details, load_seen_ids, mark_seen


def _job(description: str | None = "some description") -> Job:
    return Job(job_id="42", title="Python Developer", url="https://example.test/42", description=description)


class MarkSeenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_new_job_is_recorded_and_then_known(self) -> None:
        first = _job()
        self.assertEqual(mark_seen([first], self.out), 1)
        self.assertTrue(first.is_new)
        self.assertEqual(load_seen_ids(self.out), {"42"})
        again = _job()
        self.assertEqual(mark_seen([again], self.out), 0)
        self.assertFalse(again.is_new)

    def test_missing_details_are_not_recorded_yet(self) -> None:
        job = _job(description=None)
        self.assertEqual(mark_seen([job], self.out, details_missing={"42"}), 1)
        self.assertTrue(job.is_new)
        self.assertEqual(load_seen_ids(self.out), set())
        self.assertEqual(load_pending_details(self.out), {"42": 1})

    def test_gives_up_after_max_attempts(self) -> None:
        for _ in range(MAX_DETAIL_ATTEMPTS - 1):
            mark_seen([_job(description=None)], self.out, details_missing={"42"})
        self.assertEqual(load_pending_details(self.out), {"42": MAX_DETAIL_ATTEMPTS - 1})
        with self.assertLogs("app.storage", level="WARNING"):
            last = _job(description=None)
            mark_seen([last], self.out, details_missing={"42"})
        self.assertTrue(last.is_new)
        self.assertEqual(load_seen_ids(self.out), {"42"})
        self.assertEqual(load_pending_details(self.out), {})
        self.assertFalse(mark_seen([_job(description=None)], self.out, details_missing={"42"}))

    def test_description_arriving_clears_pending(self) -> None:
        mark_seen([_job(description=None)], self.out, details_missing={"42"})
        job = _job()
        mark_seen([job], self.out)
        self.assertTrue(job.is_new)
        self.assertEqual(load_seen_ids(self.out), {"42"})
        self.assertEqual(load_pending_details(self.out), {})


if __name__ == "__main__":
    unittest.main()
