"""
Find LinkedIn jobs that match your criteria and your resume, and alert you on the desktop.

Default searches: "Python Developer" and "FastAPI Developer", posted in the past
24 hours, Entry level and Senior (Mid-Senior) level, under 10 applicants, title
mentioning python/fastapi/django. Every job is scored 0-100 against resume.json
(from its title alone when the detail pane did not load); jobs not seen in earlier
runs that fit your experience and score at least --min-score raise a desktop
notification (--unknown-years-min-score when the posting states no years).
Results are printed and saved under data/jobs/ as JSON and CSV.

Examples:
    python -m app.main --keep-open           # first time: sign in to LinkedIn in the window, then rerun
    python -m app.main                       # one run, headed browser
    python -m app.main --watch --headless    # keep checking every 5 minutes, no browser window
    python -m app.main --watch --interval 600 --min-score 60
    python -m app.main --keywords "Django Developer" --title-keyword django
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from .browser import BrowserLaunchError
from .config import (
    BROWSER_DATA_DIR,
    DEFAULT_KEYWORDS,
    DEFAULT_MIN_SCORE,
    LOGS_DIR,
    OUTPUT_DIR,
    RESUME_PATH,
    WATCH_INTERVAL_SECONDS,
    ExperienceLevel,
    SearchCriteria,
)
from .linkedin import NotLoggedInError, SearchPageChangedError
from .matcher import ResumeProfile, ResumeProfileError
from .notify import desktop_notify
from .report import configure_logging, print_next_check, print_report, print_scan_start
from .scan import DEFAULT_UNKNOWN_YEARS_MIN_SCORE, ScanOptions, ScanResult, scan

log = logging.getLogger("job_agent")

MAX_CONSECUTIVE_FAILURES = 5  # --watch gives up (and notifies) after this many failed checks in a row
MIN_SLEEP_SECONDS = 10


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    defaults = SearchCriteria()
    parser = argparse.ArgumentParser(description="Find LinkedIn jobs matching the configured filters and your resume.")
    search = parser.add_argument_group("search")
    search.add_argument(
        "--keywords", action="append", metavar="TEXT",
        help=f"search keywords; repeatable, one LinkedIn search each (default: {', '.join(DEFAULT_KEYWORDS)})",
    )
    search.add_argument(
        "--title-keyword", action="append", dest="title_keywords", metavar="WORD",
        help=f"keep jobs whose title contains any of these; repeatable (default: {', '.join(defaults.title_any_of)})",
    )
    search.add_argument("--any-title", action="store_true", help="keep every result, do not filter on title")
    search.add_argument("--location", default=None, help="location text, e.g. 'India' or 'Bengaluru'")
    search.add_argument(
        "--experience-level", action="append", dest="experience_levels", metavar="LEVEL",
        choices=[level.name.lower() for level in ExperienceLevel],
        help=f"LinkedIn experience-level filter; repeatable, one of {', '.join(l.name.lower() for l in ExperienceLevel)} "
             f"(default: {', '.join(l.name.lower() for l in defaults.experience_levels)})",
    )
    search.add_argument("--max-pages", type=int, default=defaults.max_pages, help="results pages to scan per search (default: %(default)s)")
    search.add_argument("--no-details", action="store_true", help="do not open each job's detail pane (no applicant counts, no resume scoring)")
    search.add_argument("--refresh-seen", action="store_true", help="open the detail pane for already-seen jobs too (re-scores them)")

    matching = parser.add_argument_group("resume matching and alerts")
    matching.add_argument("--resume", type=Path, default=RESUME_PATH, help="resume profile JSON (default: %(default)s)")
    matching.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE, help="notify only for new jobs scoring at least this, 0-100 (default: %(default)s)")
    matching.add_argument(
        "--years-tolerance", type=float, default=None, metavar="YEARS",
        help="skip jobs asking for more than your experience plus this many years (default: years_tolerance in resume.json, 1)",
    )
    matching.add_argument(
        "--unknown-years-min-score", type=int, default=DEFAULT_UNKNOWN_YEARS_MIN_SCORE, metavar="SCORE",
        help="a job that states no years of experience must score at least this to be notified (default: %(default)s)",
    )
    matching.add_argument("--no-notify", action="store_true", help="never show desktop notifications")

    running = parser.add_argument_group("running")
    running.add_argument("--watch", action="store_true", help="keep running, checking LinkedIn every --interval seconds")
    running.add_argument("--interval", type=int, default=WATCH_INTERVAL_SECONDS, help="seconds between checks with --watch (default: %(default)s)")
    running.add_argument("--headless", action="store_true", help="run without a visible browser window")
    running.add_argument("--keep-open", action="store_true", help="wait for Enter before closing the browser (sign-in runs)")
    running.add_argument("--profile", type=Path, default=BROWSER_DATA_DIR, help="Chromium user-data dir (default: %(default)s)")
    running.add_argument("--out-dir", type=Path, default=OUTPUT_DIR, help="where to write results (default: %(default)s)")
    running.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def build_criteria(args: argparse.Namespace) -> list[SearchCriteria]:
    """
    One SearchCriteria per keyword phrase; everything else is shared.
    """
    if args.any_title:
        title_terms: tuple[str, ...] = ()
    elif args.title_keywords:
        title_terms = tuple(args.title_keywords)
    else:
        title_terms = SearchCriteria().title_any_of
    if args.experience_levels:
        levels = tuple(ExperienceLevel[name.upper()] for name in args.experience_levels)
    else:
        levels = SearchCriteria().experience_levels
    base = SearchCriteria(location=args.location, title_any_of=title_terms, max_pages=args.max_pages, experience_levels=levels)
    return [replace(base, keywords=kw) for kw in (args.keywords or DEFAULT_KEYWORDS)]


def load_profile(path: Path, years_tolerance: float | None) -> ResumeProfile | None:
    """None (with a warning) when there is no resume file: the run then works without scoring."""
    if not path.exists():
        log.warning("No resume profile at %s: jobs will not be scored and every new job is announced", path)
        return None
    profile = ResumeProfile.load(path)
    if years_tolerance is not None:
        profile = replace(profile, years_tolerance=years_tolerance)
    log.info(
        "Resume profile: %s, %.1f yrs experience (jobs may ask up to %g more), %d core + %d secondary skills",
        profile.name, profile.years_experience, profile.years_tolerance, len(profile.core_skills), len(profile.secondary_skills),
    )
    return profile


def run_once(
    args: argparse.Namespace, criteria_list: list[SearchCriteria], profile: ResumeProfile | None, watching: bool, cycle: int = 1,
) -> ScanResult:
    """One full check (search, score, persist, notify), then the printed report."""
    print_scan_start(cycle, datetime.now())
    options = ScanOptions(
        profile_dir=args.profile,
        out_dir=args.out_dir,
        headless=args.headless,
        fetch_details=not args.no_details,
        refresh_seen=args.refresh_seen,
        stop_at_seen_page=watching,  # polling: older pages are already known
        min_score=args.min_score,
        unknown_years_min_score=args.unknown_years_min_score,
        notify=not args.no_notify,
        keep_open=args.keep_open and not watching,
    )
    result = scan(options, criteria_list, profile)
    print_report(
        result.jobs,
        new_count=result.new_count,
        announced_ids=result.announced_ids,
        min_score=args.min_score,
        only_new=watching,
        finished_at=result.finished_at,
        json_path=result.json_path,
        csv_path=result.csv_path,
    )
    return result


def watch(args: argparse.Namespace, criteria_list: list[SearchCriteria], profile: ResumeProfile | None) -> int:
    log.info("Watching LinkedIn every %d s (Ctrl+C to stop)", args.interval)
    failures = 0
    cycle = 0
    while True:
        cycle += 1
        started = time.monotonic()
        try:
            run_once(args, criteria_list, profile, watching=True, cycle=cycle)
            failures = 0
        except NotLoggedInError as exc:
            log.error("%s", exc)
            desktop_notify("Job Agent stopped", "LinkedIn wants you to sign in again. Run: python -m app.main --keep-open", urgency="critical")
            return 3
        except (BrowserLaunchError, SearchPageChangedError) as exc:
            failures += 1
            log.error("Check failed (%d/%d): %s", failures, MAX_CONSECUTIVE_FAILURES, exc)
        except Exception:  # noqa: BLE001 - a flaky page must not kill a background watcher
            failures += 1
            log.exception("Check failed (%d/%d)", failures, MAX_CONSECUTIVE_FAILURES)
        if failures >= MAX_CONSECUTIVE_FAILURES:
            desktop_notify("Job Agent stopped", f"{failures} checks failed in a row, see logs/job_agent.log", urgency="critical")
            return 6

        sleep_for = max(args.interval - (time.monotonic() - started), MIN_SLEEP_SECONDS)
        next_at = datetime.now() + timedelta(seconds=sleep_for)
        log.debug("Next check at %s (in %d s)", f"{next_at:%H:%M:%S}", sleep_for)
        print_next_check(next_at, sleep_for)
        time.sleep(sleep_for)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(LOGS_DIR, args.verbose)
    criteria_list = build_criteria(args)
    try:
        profile = load_profile(args.resume, args.years_tolerance)
    except ResumeProfileError as exc:
        log.error("%s", exc)
        return 5

    try:
        if args.watch:
            return watch(args, criteria_list, profile)
        run_once(args, criteria_list, profile, watching=False)
    except KeyboardInterrupt:
        log.info("Stopped by user")
        return 0
    except BrowserLaunchError as exc:
        log.error("%s", exc)
        return 2
    except NotLoggedInError as exc:
        log.error("%s", exc)
        return 3
    except SearchPageChangedError as exc:
        log.error("%s", exc)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
