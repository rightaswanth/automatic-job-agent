"""
One scan: search LinkedIn, score against the resume, persist, notify.

Called by the command line (``app.main``). It prints nothing; the caller decides
how to show the result.
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
from .storage import load_seen_ids, mark_seen, save_results

log = logging.getLogger(__name__)

DEFAULT_UNKNOWN_YEARS_MIN_SCORE = 70  # a posting that states no years must score at least this to be announced


@dataclass(frozen=True)
class ScanOptions:
    profile_dir: Path  # Chromium user-data dir holding the LinkedIn session
    out_dir: Path
    headless: bool = True
    fetch_details: bool = True
    refresh_seen: bool = False  # open the detail pane for already-seen jobs too
    stop_at_seen_page: bool = False  # stop paging once a page holds only seen jobs (polling mode)
    min_score: int = DEFAULT_MIN_SCORE
    unknown_years_min_score: int = DEFAULT_UNKNOWN_YEARS_MIN_SCORE  # bar for postings that state no years
    notify: bool = True
    keep_open: bool = False  # wait for Enter before closing a visible browser


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


def announceable(job: Job, min_score: int, unknown_years_min_score: int = DEFAULT_UNKNOWN_YEARS_MIN_SCORE) -> bool:
    """
    New, fits the candidate's experience, and scores enough. An unscored job (no resume profile) is
    announced; a posting that states no years must clear ``unknown_years_min_score`` as well.
    """
    if not job.is_new or job.experience_fit is False:
        return False
    if job.match_score is None:
        return True
    bar = max(min_score, unknown_years_min_score) if job.required_years is None else min_score
    return job.match_score >= bar


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
    details_missing = {j.job_id for j in jobs if options.fetch_details and not j.description}  # pane failed: retried next scan
    new_count = mark_seen(jobs, options.out_dir, details_missing)
    if profile:
        score_jobs(jobs, profile)  # a job without a description is scored from its title alone; the notes say so

    criteria_text = "; ".join(c.describe() for c in criteria_list)
    json_path, csv_path = save_results(jobs, options.out_dir, criteria_text)

    to_announce = [  # a job whose pane failed is announced once its description arrives, not now
        j for j in jobs
        if j.job_id not in details_missing and announceable(j, options.min_score, options.unknown_years_min_score)
    ]
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
