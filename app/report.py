"""
Colourful terminal output via ``rich``: the console log handler used while
scanning, and the report printed after each scan.

Plain text is produced automatically when stdout is not a terminal (piping to
a file, ``nohup``), and ``NO_COLOR=1`` in the environment turns colour off.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from .models import Job

console = Console()

VERDICT_STYLE = {"strong": "bold green", "good": "green", "weak": "yellow", "poor": "red"}
LINK_STYLE = "bold underline bright_blue"
TITLE_MAX = 70  # skipped jobs are shown one per line, so long titles are cut


def log_handler(verbose: bool) -> logging.Handler:
    """Console handler with coloured level badges and highlighted numbers/URLs in every scan line."""
    handler = RichHandler(console=console, show_path=False, rich_tracebacks=True, log_time_format="%H:%M:%S")
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))  # rich prints time and level itself
    return handler


def _sort_key(job: Job) -> int:
    return job.match_score if job.match_score is not None else -1


def _link(url: str) -> Text:
    return Text(url, style=f"{LINK_STYLE} link {url}")  # clickable in terminals that support hyperlinks


def _score(job: Job) -> Text:
    if job.match_score is None:
        return Text(" --  unscored", style="dim")
    return Text(f"{job.match_score:3d}% {job.match_verdict:<6}", style=VERDICT_STYLE.get(job.match_verdict or "", ""))


def _job_block(job: Job, announced: bool) -> Text:
    applicants = job.applicants_text or (f"{job.applicants} applicants" if job.applicants is not None else None)
    meta = [
        job.company or "?", job.location or "?", job.workplace_type,
        f"posted {job.posted_text}" if job.posted_text else None, applicants,
    ]
    block = Text()
    block.append("NEW " if job.is_new else "    ", style="bold magenta")
    block.append_text(_score(job))
    block.append("  ")
    block.append(job.title, style="bold")
    if job.easy_apply:
        block.append("  Easy Apply", style="bold cyan")
    if announced:
        block.append("  notified", style="magenta")
    block.append("\n      ")
    block.append(" · ".join(m for m in meta if m), style="cyan")
    if job.match_score is not None:
        block.append("\n      ")
        block.append(job.match_notes or "", style="yellow" if job.experience_fit is False else "green")
        block.append("  ·  skills: ", style="dim")
        block.append(", ".join(job.matched_skills) or "none found", style="dim")
    block.append("\n      ")
    block.append_text(_link(job.url))
    return block


def _skipped_line(job: Job) -> Text:
    title = job.title if len(job.title) <= TITLE_MAX else job.title[: TITLE_MAX - 1] + "…"
    line = Text("  ✖ ", style="red")
    line.append(f"asks {job.required_years}+ yrs".ljust(14), style="red")
    line.append(title, style="dim")
    line.append(f"  |  {job.company or '?'}  |  ", style="dim")
    line.append_text(_link(job.url))
    return line


def _heading(symbol: str, title: str, detail: str, count: int, style: str) -> None:
    console.print()
    console.print(Text(f"{symbol} {title} ", style=f"bold {style}") + Text(f"— {count} · {detail}", style="dim"))


def print_scan_start(cycle: int, started_at: datetime) -> None:
    console.print()
    console.rule(Text(f"Scan #{cycle}  ·  started {started_at:%d %b %H:%M:%S}", style="bold"), style="bright_blue")


def print_report(
    jobs: list[Job],
    *,
    new_count: int,
    announced_ids: set[str],
    min_score: int,
    only_new: bool,
    finished_at: datetime,
    json_path: Path,
    csv_path: Path,
) -> None:
    """Three groups: notified, other matches, and skipped for asking too much experience."""
    shown = [j for j in jobs if j.is_new] if only_new else list(jobs)
    notified = sorted((j for j in shown if j.job_id in announced_ids), key=_sort_key, reverse=True)
    skipped = sorted((j for j in shown if j.experience_fit is False), key=_sort_key, reverse=True)
    others = sorted(
        (j for j in shown if j.job_id not in announced_ids and j.experience_fit is not False),
        key=_sort_key, reverse=True,
    )

    console.print()
    console.rule(
        Text(f"Scan finished {finished_at:%d %b %H:%M:%S}  ·  {len(jobs)} matching  ·  {new_count} new  ·  {len(notified)} notified", style="bold"),
        style="bright_blue",
    )

    _heading("🔔", "Notified", f"new, fit your experience, score ≥ {min_score}", len(notified), "green")
    if notified:
        for job in notified:
            console.print(_job_block(job, announced=True))
    else:
        console.print(Text("      nothing new worth announcing this time", style="dim"))

    if others:
        detail = f"new but below {min_score}" if only_new else f"below {min_score}, unscored, or seen in an earlier run"
        _heading("·", "Other matches", detail, len(others), "yellow")
        for job in others:
            console.print(_job_block(job, announced=False))

    if skipped:
        _heading("✖", "Skipped", "ask for more experience than you have", len(skipped), "red")
        for job in skipped:
            console.print(_skipped_line(job))

    console.print()
    console.print(Text(f"Saved  {json_path}\n       {csv_path}", style="dim"))


def print_next_check(at: datetime, seconds: float) -> None:
    console.print(Text(f"⏱  Next check at {at:%H:%M:%S} (in {seconds:.0f} s) — Ctrl+C to stop", style="dim"))


def configure_logging(logs_dir: Path, verbose: bool) -> None:
    """Debug lines to logs/job_agent.log (rotated at midnight), coloured INFO (or DEBUG) lines to the console."""
    from logging.handlers import TimedRotatingFileHandler

    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(logs_dir / "job_agent.log", when="midnight", backupCount=14, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # the file always keeps debug lines (e.g. skipped titles)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, log_handler(verbose)], force=True)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
