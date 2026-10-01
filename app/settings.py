"""
Settings for the FastAPI service, read from ``JOB_AGENT_*`` environment
variables (the command line uses flags instead).

    JOB_AGENT_INTERVAL=300            seconds between automatic scans
    JOB_AGENT_AUTO_SCAN=1             0 = only scan when POST /api/scan is called
    JOB_AGENT_HEADLESS=1              0 = show the browser window
    JOB_AGENT_MIN_SCORE=50            notify for new jobs scoring at least this
    JOB_AGENT_KEYWORDS="Python Developer,FastAPI Developer"
    JOB_AGENT_MAX_PAGES=10
    JOB_AGENT_NOTIFY=1                0 = no desktop notifications
    JOB_AGENT_YEARS_TOLERANCE=1       overrides years_tolerance in resume.json
    JOB_AGENT_RESUME=/path/resume.json
    JOB_AGENT_BROWSER_DIR, JOB_AGENT_OUT_DIR, JOB_AGENT_VERBOSE=1
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .config import (
    BROWSER_DATA_DIR,
    DEFAULT_KEYWORDS,
    DEFAULT_MIN_SCORE,
    OUTPUT_DIR,
    RESUME_PATH,
    WATCH_INTERVAL_SECONDS,
    SearchCriteria,
)
from .scan import ScanOptions

_TRUE = {"1", "true", "yes", "on"}


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    return default if value is None or value.strip() == "" else value.strip().lower() in _TRUE


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    value = env.get(key)
    return default if value is None or value.strip() == "" else int(value)


@dataclass(frozen=True)
class Settings:
    interval: int = WATCH_INTERVAL_SECONDS
    auto_scan: bool = True
    headless: bool = True
    min_score: int = DEFAULT_MIN_SCORE
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    max_pages: int = 10
    notify: bool = True
    years_tolerance: float | None = None
    resume_path: Path = RESUME_PATH
    browser_dir: Path = BROWSER_DATA_DIR
    out_dir: Path = OUTPUT_DIR
    verbose: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        keywords = tuple(k.strip() for k in env.get("JOB_AGENT_KEYWORDS", "").split(",") if k.strip())
        tolerance = env.get("JOB_AGENT_YEARS_TOLERANCE", "").strip()
        return cls(
            interval=max(_int(env, "JOB_AGENT_INTERVAL", WATCH_INTERVAL_SECONDS), 30),
            auto_scan=_bool(env, "JOB_AGENT_AUTO_SCAN", True),
            headless=_bool(env, "JOB_AGENT_HEADLESS", True),
            min_score=_int(env, "JOB_AGENT_MIN_SCORE", DEFAULT_MIN_SCORE),
            keywords=keywords or DEFAULT_KEYWORDS,
            max_pages=_int(env, "JOB_AGENT_MAX_PAGES", SearchCriteria().max_pages),
            notify=_bool(env, "JOB_AGENT_NOTIFY", True),
            years_tolerance=float(tolerance) if tolerance else None,
            resume_path=Path(env.get("JOB_AGENT_RESUME") or RESUME_PATH),
            browser_dir=Path(env.get("JOB_AGENT_BROWSER_DIR") or BROWSER_DATA_DIR),
            out_dir=Path(env.get("JOB_AGENT_OUT_DIR") or OUTPUT_DIR),
            verbose=_bool(env, "JOB_AGENT_VERBOSE", False),
        )

    def criteria(self) -> list[SearchCriteria]:
        base = SearchCriteria(max_pages=self.max_pages)
        return [replace(base, keywords=kw) for kw in self.keywords]

    def scan_options(self) -> ScanOptions:
        return ScanOptions(
            profile_dir=self.browser_dir,
            out_dir=self.out_dir,
            headless=self.headless,
            stop_at_seen_page=True,  # the service polls, so older pages are already known
            min_score=self.min_score,
            notify=self.notify,
        )

    def describe(self) -> dict:
        return {
            "interval_seconds": self.interval,
            "auto_scan": self.auto_scan,
            "headless": self.headless,
            "min_score": self.min_score,
            "keywords": list(self.keywords),
            "max_pages": self.max_pages,
            "notify": self.notify,
            "years_tolerance": self.years_tolerance,
            "resume_path": str(self.resume_path),
            "out_dir": str(self.out_dir),
        }
