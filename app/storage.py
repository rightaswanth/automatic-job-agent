"""
Persist search results (JSON + CSV) and remember which jobs were seen before.

Two small JSON stores live next to the results: ``seen_jobs.json`` (job id -> when it
was first seen) and ``pending_details.json`` (job id -> how many scans its detail pane
failed to load). A job whose pane did not load is not remembered yet, so the next scan
opens it again; after MAX_DETAIL_ATTEMPTS such scans it is remembered anyway.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

from .models import Job

log = logging.getLogger(__name__)

CSV_COLUMNS = [
    "job_id", "title", "company", "location", "workplace_type", "posted_text", "posted_at",
    "applicants", "applicants_text", "easy_apply", "salary", "job_type", "is_new",
    "match_score", "match_verdict", "matched_skills", "missing_skills", "required_years", "experience_fit", "match_notes", "url",
]
SEEN_STORE_NAME = "seen_jobs.json"
PENDING_DETAILS_NAME = "pending_details.json"
MAX_DETAIL_ATTEMPTS = 3  # scans without a detail pane before the job is remembered with its title-only score


def _read_store(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("Store %s is corrupt, starting fresh", path)
        return {}


def load_seen_ids(output_dir: Path) -> set[str]:
    return set(_read_store(output_dir / SEEN_STORE_NAME))


def load_pending_details(output_dir: Path) -> dict[str, int]:
    """job_id -> number of scans whose detail pane failed to load, for jobs not remembered yet."""
    return _read_store(output_dir / PENDING_DETAILS_NAME)


def mark_seen(jobs: list[Job], output_dir: Path, details_missing: set[str] = frozenset()) -> int:
    """
    Set ``is_new`` on each job from the seen-store and record the new ones. Returns the count of new jobs.

    A new job in ``details_missing`` (its detail pane did not load) is counted as new but not recorded,
    so the next scan opens its pane again; after MAX_DETAIL_ATTEMPTS such scans it is recorded anyway.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    seen_store, pending_store = output_dir / SEEN_STORE_NAME, output_dir / PENDING_DETAILS_NAME
    seen = _read_store(seen_store)
    pending: dict[str, int] = _read_store(pending_store)
    now = datetime.now().isoformat(timespec="seconds")
    new_count = 0
    for job in jobs:
        job.is_new = job.job_id not in seen
        if not job.is_new:
            continue
        new_count += 1
        if job.job_id in details_missing:
            attempts = pending.get(job.job_id, 0) + 1
            if attempts < MAX_DETAIL_ATTEMPTS:
                pending[job.job_id] = attempts
                log.info("Detail pane missing for %s (%s), attempt %d/%d; retrying next scan", job.job_id, job.title, attempts, MAX_DETAIL_ATTEMPTS)
                continue
            log.warning("Giving up on details for %s (%s) after %d scans; scored from the title only", job.job_id, job.title, attempts)
        seen[job.job_id] = now
        pending.pop(job.job_id, None)
    seen_store.write_text(json.dumps(seen, indent=2))
    pending_store.write_text(json.dumps(pending, indent=2))
    return new_count


def save_results(jobs: list[Job], output_dir: Path, criteria_description: str) -> tuple[Path, Path]:
    """Write timestamped JSON and CSV files, plus ``latest.json``. Returns (json_path, csv_path)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "searched_at": datetime.now().isoformat(timespec="seconds"),
        "criteria": criteria_description,
        "count": len(jobs),
        "jobs": [job.to_dict() for job in jobs],
    }
    json_path = output_dir / f"jobs_{stamp}.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    (output_dir / "latest.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    csv_path = output_dir / f"jobs_{stamp}.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for job in jobs:
            row = job.to_dict()
            for key in ("matched_skills", "missing_skills"):  # lists read badly in a spreadsheet cell
                row[key] = "; ".join(row[key])
            writer.writerow(row)
    log.info("Saved %d jobs to %s and %s", len(jobs), json_path, csv_path)
    return json_path, csv_path
