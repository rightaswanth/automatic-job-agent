"""
Runs scans for the FastAPI app: one at a time, in a worker thread, on a timer
and on demand.

Playwright's sync API refuses to run on a thread that has a running asyncio
loop, so every scan goes through ``asyncio.to_thread``. A lock makes a second
request while a scan is running return immediately instead of queueing.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta

from .matcher import ResumeProfile, ResumeProfileError
from .scan import ScanResult, scan
from .settings import Settings

log = logging.getLogger(__name__)

HISTORY_SIZE = 50


class JobService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at = datetime.now()
        self.profile: ResumeProfile | None = None
        self.profile_error: str | None = None
        self.reload_profile()

        self._lock = threading.Lock()
        self._scheduler: asyncio.Task | None = None
        self.scanning_since: datetime | None = None
        self.next_scan_at: datetime | None = None
        self.last_result: ScanResult | None = None
        self.last_error: str | None = None
        self.history: deque[dict] = deque(maxlen=HISTORY_SIZE)  # newest first

    # ------------------------------------------------------------ profile
    def reload_profile(self) -> None:
        path = self.settings.resume_path
        try:
            if not path.exists():
                self.profile, self.profile_error = None, f"No resume profile at {path}: jobs are not scored"
                log.warning("%s", self.profile_error)
                return
            profile = ResumeProfile.load(path)
            if self.settings.years_tolerance is not None:
                profile = replace(profile, years_tolerance=self.settings.years_tolerance)
            self.profile, self.profile_error = profile, None
            log.info(
                "Resume profile: %s, %.1f yrs experience (+%g), %d core + %d secondary skills",
                profile.name, profile.years_experience, profile.years_tolerance,
                len(profile.core_skills), len(profile.secondary_skills),
            )
        except ResumeProfileError as exc:
            self.profile, self.profile_error = None, str(exc)
            log.error("%s", exc)

    def profile_summary(self) -> dict:
        summary: dict = {"path": str(self.settings.resume_path), "loaded": self.profile is not None, "error": self.profile_error}
        if self.profile:
            summary.update(
                name=self.profile.name,
                years_experience=self.profile.years_experience,
                years_tolerance=self.profile.years_tolerance,
                core_skills=len(self.profile.core_skills),
                secondary_skills=len(self.profile.secondary_skills),
            )
        return summary

    # ---------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self.settings.auto_scan:
            self._scheduler = asyncio.create_task(self._run_scheduler(), name="job-agent-scheduler")
            log.info("Scheduler started: a scan every %d s", self.settings.interval)
        else:
            log.info("Auto-scan is off (JOB_AGENT_AUTO_SCAN=0): scans run only on POST /api/scan")

    async def stop(self) -> None:
        if self._scheduler:
            self._scheduler.cancel()
            try:
                await self._scheduler
            except asyncio.CancelledError:
                pass
        if self.is_scanning:
            log.info("Waiting for the running scan to finish before exit")

    async def _run_scheduler(self) -> None:
        while True:
            await self.scan()
            self.next_scan_at = datetime.now() + timedelta(seconds=self.settings.interval)
            await asyncio.sleep(self.settings.interval)

    # ------------------------------------------------------------- scans
    @property
    def is_scanning(self) -> bool:
        return self._lock.locked()

    async def scan(self) -> ScanResult | None:
        """Run one scan in a worker thread. None when a scan is already running or it failed."""
        return await asyncio.to_thread(self._scan_blocking)

    def _scan_blocking(self) -> ScanResult | None:
        if not self._lock.acquire(blocking=False):
            log.info("Scan requested while one is running, ignored")
            return None
        started = datetime.now()
        self.scanning_since, self.last_error = started, None
        try:
            result = scan(self.settings.scan_options(), self.settings.criteria(), self.profile)
            self.last_result = result
            self.history.appendleft(result.summary())
            return result
        except Exception as exc:  # noqa: BLE001 - keep the service alive; the error is shown in /api/status
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("Scan failed")
            self.history.appendleft({
                "scan_id": started.isoformat(timespec="seconds"),
                "started_at": started.isoformat(timespec="seconds"),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "error": self.last_error,
            })
            return None
        finally:
            self.scanning_since = None
            self._lock.release()

    def status(self) -> dict:
        iso = lambda dt: dt.isoformat(timespec="seconds") if dt else None  # noqa: E731
        return {
            "state": "scanning" if self.is_scanning else "idle",
            "scanning_since": iso(self.scanning_since),
            "service_started_at": iso(self.started_at),
            "interval_seconds": self.settings.interval,
            "auto_scan": self.settings.auto_scan,
            "next_scan_at": iso(self.next_scan_at) if not self.is_scanning else None,
            "last_scan": self.last_result.summary() if self.last_result else None,
            "last_error": self.last_error,
            "profile": self.profile_summary(),
            "settings": self.settings.describe(),
        }
