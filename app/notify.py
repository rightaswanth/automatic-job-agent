"""
Desktop notifications for newly found jobs.

Linux uses ``notify-send`` (libnotify, present on GNOME/KDE/XFCE...), macOS uses
``osascript``. Anywhere else the notification is only logged.
"""
from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess

from .models import Job

log = logging.getLogger(__name__)

APP_NAME = "Job Agent"
INDIVIDUAL_LIMIT = 3  # up to this many new jobs get their own popup; more are rolled into one summary
_warned_unsupported = False


def desktop_notify(title: str, body: str, urgency: str = "normal") -> bool:
    """Show a desktop popup. Returns False (after logging) when the platform has no notifier."""
    global _warned_unsupported
    system = platform.system()
    try:
        if system == "Linux":
            exe = shutil.which("notify-send")
            if not exe:
                return _unsupported("notify-send is not installed (apt install libnotify-bin)")
            cmd = [exe, f"--app-name={APP_NAME}", f"--urgency={urgency}", "--expire-time=15000", title, body]
        elif system == "Darwin":
            script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
            cmd = ["osascript", "-e", script]
        else:
            return _unsupported(f"no desktop notifier for {system}")
        subprocess.run(cmd, check=False, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Desktop notification failed: %s", exc)
        return False
    return True


def _unsupported(reason: str) -> bool:
    global _warned_unsupported
    if not _warned_unsupported:
        log.warning("Desktop notifications disabled: %s", reason)
        _warned_unsupported = True
    return False


def _job_line(job: Job) -> str:
    score = f"{job.match_score}% match" if job.match_score is not None else "unscored"
    applicants = job.applicants_text or (f"{job.applicants} applicants" if job.applicants is not None else None)
    bits = [job.company or "?", job.location or "?", score]
    if job.posted_text:
        bits.append(f"posted {job.posted_text}")
    if applicants:
        bits.append(applicants)
    if job.easy_apply:
        bits.append("Easy Apply")
    return " · ".join(bits)


def notify_new_jobs(jobs: list[Job]) -> int:
    """Pop a notification for each job in ``jobs`` (or one summary when there are many). Returns how many were announced."""
    if not jobs:
        return 0
    ranked = sorted(jobs, key=lambda j: (j.match_score if j.match_score is not None else -1), reverse=True)
    if len(ranked) <= INDIVIDUAL_LIMIT:
        for job in ranked:
            desktop_notify(f"New job: {job.title}", f"{_job_line(job)}\n{job.url}")
    else:
        lines = [f"• {job.title} — {job.company or '?'} ({job.match_score or 0}%)" for job in ranked[:5]]
        if len(ranked) > 5:
            lines.append(f"…and {len(ranked) - 5} more, see data/jobs/latest.json")
        desktop_notify(f"{len(ranked)} new matching jobs", "\n".join(lines))
    log.info("Notified about %d new job(s)", len(ranked))
    return len(ranked)
