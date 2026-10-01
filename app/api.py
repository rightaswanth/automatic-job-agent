"""
FastAPI app: the JSON API and the dashboard at ``/``.

    uvicorn app.api:app --port 8000           # then open http://localhost:8000
    uvicorn app.api:app --port 8000 --reload  # while developing

Interactive API docs are at ``/docs``. Settings come from ``JOB_AGENT_*``
environment variables (see ``app/settings.py``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .config import LOGS_DIR
from .matcher import ResumeProfile, ResumeProfileError
from .report import configure_logging
from .service import JobService
from .settings import Settings
from .storage import load_jobs_db

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    configure_logging(LOGS_DIR, settings.verbose)
    service = JobService(settings)
    app.state.service = service
    await service.start()
    try:
        yield
    finally:
        await service.stop()


app = FastAPI(
    title="Job Agent",
    version="1.0.0",
    description="Watches LinkedIn for new jobs, scores them against your resume and notifies you.",
    lifespan=lifespan,
)


def _service(request: Request) -> JobService:
    return request.app.state.service


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/status")
def status(request: Request) -> dict:
    """Scanner state, last scan summary, next scheduled scan, profile and settings."""
    return _service(request).status()


@app.post("/api/scan", status_code=202)
async def start_scan(request: Request) -> dict:
    """Start a scan now (it runs in the background; poll /api/status)."""
    service = _service(request)
    if service.is_scanning:
        raise HTTPException(status_code=409, detail="A scan is already running")
    asyncio.create_task(service.scan(), name="job-agent-manual-scan")
    return {"started": True}


@app.get("/api/scans")
def scans(request: Request) -> list[dict]:
    """Summaries of the scans this process has run, newest first."""
    return list(_service(request).history)


@app.get("/api/jobs")
def jobs(
    request: Request,
    min_score: int = Query(0, ge=0, le=100, description="hide jobs scoring below this (unscored jobs are hidden when > 0)"),
    only_new: bool = Query(False, description="only jobs first seen in the latest scan"),
    include_skipped: bool = Query(True, description="include jobs that ask for more experience than you have"),
    q: str | None = Query(None, description="text in title, company or location"),
    limit: int = Query(300, ge=1, le=2000),
    full: bool = Query(False, description="include the job description"),
) -> dict:
    """Every job ever found, best score first."""
    db = load_jobs_db(_service(request).settings.out_dir)
    latest_scan = max((r.get("last_seen_scan") or "" for r in db.values()), default=None) or None
    needle = (q or "").strip().lower()
    rows = []
    for record in db.values():
        score = record.get("match_score")
        if min_score and (score is None or score < min_score):
            continue
        if only_new and record.get("first_seen_scan") != latest_scan:
            continue
        if not include_skipped and record.get("experience_fit") is False:
            continue
        if needle and needle not in f"{record.get('title', '')} {record.get('company', '')} {record.get('location', '')}".lower():
            continue
        row = record if full else {k: v for k, v in record.items() if k != "description"}
        rows.append({**row, "new_in_latest_scan": record.get("first_seen_scan") == latest_scan})
    rows.sort(
        key=lambda r: (r["match_score"] if r.get("match_score") is not None else -1, r.get("last_seen_scan") or ""),
        reverse=True,
    )
    return {"count": min(len(rows), limit), "total": len(db), "latest_scan": latest_scan, "jobs": rows[:limit]}


@app.get("/api/jobs/{job_id}")
def job(request: Request, job_id: str) -> dict:
    """One job with its full description."""
    record = load_jobs_db(_service(request).settings.out_dir).get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return record


@app.get("/api/profile")
def profile(request: Request) -> dict:
    """The resume profile (resume.json) and what was loaded from it."""
    service = _service(request)
    path = service.settings.resume_path
    raw: dict | None = None
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = None
    return {"summary": service.profile_summary(), "profile": raw}


@app.put("/api/profile")
def update_profile(request: Request, new_profile: dict[str, Any] = Body(...)) -> dict:
    """Replace resume.json after validating it; the next scan uses it."""
    service = _service(request)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(new_profile, tmp)
        tmp_path = Path(tmp.name)
    try:
        ResumeProfile.load(tmp_path)
    except ResumeProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    service.settings.resume_path.write_text(json.dumps(new_profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    service.reload_profile()
    log.info("Resume profile updated from the dashboard")
    return service.profile_summary()
