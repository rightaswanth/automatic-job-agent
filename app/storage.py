"""Persist search results (JSON + CSV) and remember which jobs were seen before."""
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


def _read_seen_store(store: Path) -> dict[str, str]:
    """job_id -> ISO time it was first seen."""
    if not store.exists():
        return {}
    try:
        return json.loads(store.read_text())
    except json.JSONDecodeError:
        log.warning("Seen-store %s is corrupt, starting fresh", store)
        return {}


def load_seen_ids(output_dir: Path) -> set[str]:
    return set(_read_seen_store(output_dir / SEEN_STORE_NAME))


def mark_seen(jobs: list[Job], output_dir: Path) -> int:
    """Set ``is_new`` on each job from the seen-store and record the new ones. Returns the count of new jobs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    store = output_dir / SEEN_STORE_NAME
    seen = _read_seen_store(store)
    now = datetime.now().isoformat(timespec="seconds")
    new_count = 0
    for job in jobs:
        job.is_new = job.job_id not in seen
        if job.is_new:
            seen[job.job_id] = now
            new_count += 1
    store.write_text(json.dumps(seen, indent=2))
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


JOBS_DB_NAME = "jobs_db.json"
_EMPTY = (None, "", [])


def load_jobs_db(output_dir: Path) -> dict[str, dict]:
    """Every job ever scanned: job_id -> ``Job.to_dict()`` plus first/last seen scan ids and ``times_seen``."""
    path = output_dir / JOBS_DB_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("Jobs database %s is corrupt, starting fresh", path)
        return {}


def upsert_jobs_db(jobs: list[Job], output_dir: Path, scan_id: str) -> dict[str, dict]:
    """Merge this scan's jobs into the database.

    Fields the fresh record lacks (the description and score of a job whose
    detail pane was not re-opened, say) keep their earlier value.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    db = load_jobs_db(output_dir)
    for job in jobs:
        fresh = job.to_dict()
        record = db.get(job.job_id)
        if record is None:
            record = {**fresh, "first_seen_scan": scan_id, "times_seen": 0}
            db[job.job_id] = record
        else:
            record.update({key: value for key, value in fresh.items() if value not in _EMPTY})
        record["last_seen_scan"] = scan_id
        record["times_seen"] += 1
    (output_dir / JOBS_DB_NAME).write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")
    return db
