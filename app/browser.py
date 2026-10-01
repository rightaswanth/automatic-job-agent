"""Launch Chromium on the persistent (logged-in) LinkedIn profile."""
from __future__ import annotations

import logging
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

log = logging.getLogger(__name__)

# Full Chromium in headless mode announces itself as "HeadlessChrome", which
# LinkedIn treats differently. The version is filled in from the installed binary.
_HEADLESS_UA_TEMPLATE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/{major}.0.0.0 Safari/537.36"
)
_FALLBACK_CHROME_MAJOR = "153"


class BrowserLaunchError(RuntimeError):
    pass


def profile_in_use_by(profile_dir: Path) -> int | None:
    """Return the PID of a live Chromium holding this profile, or None."""
    lock = profile_dir / "SingletonLock"
    if not lock.is_symlink():
        return None
    try:
        target = os.readlink(lock)  # "<hostname>-<pid>"
        pid = int(target.rsplit("-", 1)[1])
        os.kill(pid, 0)
    except (OSError, ValueError, IndexError):
        return None
    return pid


def _chrome_major_version(executable: str | None) -> str:
    if not executable:
        return _FALLBACK_CHROME_MAJOR
    try:
        out = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10).stdout
        match = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
        return match.group(1) if match else _FALLBACK_CHROME_MAJOR
    except (OSError, subprocess.SubprocessError):
        return _FALLBACK_CHROME_MAJOR


@contextmanager
def open_browser(profile_dir: Path, headless: bool = False) -> Iterator[tuple[BrowserContext, Page]]:
    """Yield (context, page) on the persistent profile. Closes the browser on exit."""
    pid = profile_in_use_by(profile_dir)
    if pid:
        raise BrowserLaunchError(
            f"Browser profile {profile_dir} is already open in another Chromium process (pid {pid}). "
            "Close that window (or press Enter in the terminal that is still running) and retry."
        )

    with sync_playwright() as p:
        launch_kwargs: dict = {
            "user_data_dir": str(profile_dir),
            "headless": headless,
            "channel": "chromium",  # full Chromium rather than the headless shell
            "viewport": {"width": 1400, "height": 900},
            "locale": "en-US",
        }
        if headless:
            major = _chrome_major_version(p.chromium.executable_path)
            launch_kwargs["user_agent"] = _HEADLESS_UA_TEMPLATE.format(major=major)
        try:
            context = p.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:  # playwright raises several error types here
            raise BrowserLaunchError(f"Could not launch Chromium on profile {profile_dir}: {exc}") from exc

        log.info("Browser launched (headless=%s, profile=%s)", headless, profile_dir)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield context, page
        finally:
            context.close()
            log.info("Browser closed")
