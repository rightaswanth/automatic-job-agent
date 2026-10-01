"""
One scan: search LinkedIn, score against the resume, persist, notify.

Shared by the command line (``app.main``) and the FastAPI service
(``app.service``). It prints nothing; the callers decide how to show the result.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .browser import open_browser
from .config import DEFAULT_MIN_SCORE, SearchCriteria
from .linkedin import LinkedInJobSearch
from .matcher import ResumeProfile, score_jobs
from .models import Job
from .notify import notify_new_jobs
from .storage import load_seen_ids, mark_seen, save_results, upsert_jobs_db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanOptions:
    profile_dir: Path  # Chromium user-data dir holding the LinkedIn session
    out_dir: Path
    headless: bool = True
    fetch_details: bool = True
    refresh_seen: bool = False  # open the detail pane for already-seen jobs too
    stop_at_seen_page: bool = False  # stop paging once a page holds only seen jobs (polling mode)
    min_score: int = DEFAULT_MIN_SCORE
    notify: bool = True
    keep_open: bool = False  # CLI only: wait for Enter before closing a visible browser


@dataclass
class ScanResult:
    started_at: datetime
    finished_at: datetime
    criteria: str
    jobs: list[Job]
    new_count: int
    announced_ids: set[str]
    json_path: Path
    csv_path: Path

    @property
    def scan_id(self) -> str:
        return self.started_at.isoformat(timespec="seconds")

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def skipped_count(self) -> int:
        return sum(1 for j in self.jobs if j.experience_fit is False)

    def summary(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "started_at": self.scan_id,
            "finished_at": self.finished_at.isoformat(timespec="seconds"),
            "duration_seconds": round(self.duration_seconds, 1),
            "matching": len(self.jobs),
            "new": self.new_count,
            "notified": len(self.announced_ids),
            "skipped": self.skipped_count,
            "criteria": self.criteria,
            "json_path": str(self.json_path),
            "csv_path": str(self.csv_path),
            "error": None,
        }


def announceable(job: Job, min_score: int) -> bool:
    """New, fits the candidate's experience, and scores enough (unscored = no resume / no details)."""
    return (
        job.is_new
        and job.experience_fit is not False
        and (job.match_score is None or job.match_score >= min_score)
    )


def scan(options: ScanOptions, criteria_list: list[SearchCriteria], profile: ResumeProfile | None) -> ScanResult:
    """Search every criteria, score, persist, notify.

    Raises the browser/LinkedIn errors of ``app.browser`` and ``app.linkedin``.
    """
    started = datetime.now()
    seen_ids = set() if options.refresh_seen else load_seen_ids(options.out_dir)
    jobs_by_id: dict[str, Job] = {}
    with open_browser(options.profile_dir, headless=options.headless) as (_context, page):
        for criteria in criteria_list:
            searcher = LinkedInJobSearch(
                page, criteria,
                fetch_details=options.fetch_details,
                skip_details_for=seen_ids,
                stop_at_seen_page=options.stop_at_seen_page,
            )
            for job in searcher.run():
                jobs_by_id.setdefault(job.job_id, job)  # the same posting often matches several searches
            seen_ids = seen_ids | set(jobs_by_id)  # do not re-open panes the previous search already read
        if options.keep_open and not options.headless:
            input("Press Enter to close the browser...")

    jobs = list(jobs_by_id.values())
    new_count = mark_seen(jobs, options.out_dir)
    if profile:
        score_jobs([j for j in jobs if j.description], profile)  # only fetched descriptions score meaningfully

    criteria_text = "; ".join(c.describe() for c in criteria_list)
    json_path, csv_path = save_results(jobs, options.out_dir, criteria_text)
    upsert_jobs_db(jobs, options.out_dir, scan_id=started.isoformat(timespec="seconds"))

    to_announce = [j for j in jobs if announceable(j, options.min_score)]
    if to_announce and options.notify:
        notify_new_jobs(to_announce)
    result = ScanResult(
        started_at=started, finished_at=datetime.now(), criteria=criteria_text, jobs=jobs, new_count=new_count,
        announced_ids={j.job_id for j in to_announce}, json_path=json_path, csv_path=csv_path,
    )
    log.info(
        "Scan done in %.0f s: %d matching, %d new, %d announced, %d skipped",
        result.duration_seconds, len(jobs), new_count, len(to_announce), result.skipped_count,
    )
    return result
