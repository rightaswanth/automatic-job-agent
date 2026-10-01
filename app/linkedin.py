"""
Run a filtered LinkedIn job search in the logged-in browser and parse the results.
"""
from __future__ import annotations

import logging
import random
import re
from datetime import datetime

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout

from .config import PAGE_SIZE, SearchCriteria
from .models import (
    Job,
    clean_title,
    parse_applicants,
    parse_job_type,
    parse_posted,
    split_location,
)

log = logging.getLogger(__name__)


class NotLoggedInError(RuntimeError):
    pass


class SearchPageChangedError(RuntimeError):
    """
    LinkedIn served a page whose layout this parser does not recognise.
    """


CARD_SELECTOR = "li[data-occludable-job-id]"
DETAIL_TITLE_SELECTOR = ".job-details-jobs-unified-top-card__job-title"

# Runs in the browser on the list of <li> job cards. Cards that are scrolled
# out of view are "occluded" (empty), so the list is scrolled first.
CARDS_JS = """(items) => items.map((li) => {
  const q = (sel) => li.querySelector(sel);
  const txt = (el) => el ? el.innerText.replace(/\\s+/g, ' ').trim() : null;
  const link = q('a[href*="/jobs/view/"]');
  const container = q('[data-job-id]');
  const raw = li.innerText.replace(/\\s+/g, ' ').trim();
  return {
    id: li.getAttribute('data-occludable-job-id') || (container && container.getAttribute('data-job-id')),
    title: link ? (link.getAttribute('aria-label') || txt(q('strong')) || txt(link)) : null,
    company: txt(q('.artdeco-entity-lockup__subtitle')),
    location: txt(q('.artdeco-entity-lockup__caption')),
    metadata: Array.from(li.querySelectorAll('.artdeco-entity-lockup__metadata')).map(txt),
    footer: Array.from(li.querySelectorAll('.job-card-container__footer-item')).map(txt),
    posted: txt(q('time')),
    href: link ? link.href.split('?')[0] : null,
    easyApply: /\\bEasy Apply\\b/i.test(raw),
    promoted: /\\bPromoted\\b/.test(raw),
    viewed: /\\bViewed\\b/.test(raw),
    rendered: li.children.length > 0,
  };
})"""

# Runs in the browser after a card is clicked and the right-hand pane has loaded.
DETAIL_JS = """() => {
  const q = (sel) => document.querySelector(sel);
  const txt = (el) => el ? el.innerText.replace(/\\s+/g, ' ').trim() : null;
  const applyBtn = q('.jobs-apply-button');
  return {
    title: txt(q('.job-details-jobs-unified-top-card__job-title')),
    company: txt(q('.job-details-jobs-unified-top-card__company-name')),
    tertiary: txt(q('.job-details-jobs-unified-top-card__tertiary-description-container'))
           || txt(q('.job-details-jobs-unified-top-card__primary-description-container')),
    pills: Array.from(document.querySelectorAll('.job-details-fit-level-preferences button'))
             .map(txt).filter(Boolean).map(t => t.split(' Matches your job preferences')[0].trim()),
    applyLabel: applyBtn ? (applyBtn.getAttribute('aria-label') || txt(applyBtn)) : null,
    description: txt(q('#job-details')) || txt(q('.jobs-description__content')),
  };
}"""

# Filter chips above the results: toggles use aria-checked, dropdown chips a "--selected" class.
ACTIVE_FILTERS_JS = """() => Array.from(document.querySelectorAll(
    '.search-reusables__primary-filter button[aria-checked="true"], '
  + '.search-reusables__primary-filter button[aria-pressed="true"], '
  + '.search-reusables__primary-filter button[class*="--selected"]'))
  .map(b => (b.innerText.replace(/\\s+/g, ' ').trim() || (b.getAttribute('aria-label') || '').replace(/ filter\\.?$/, '')))
  .filter(Boolean)"""


class LinkedInJobSearch:
    def __init__(
        self,
        page: Page,
        criteria: SearchCriteria,
        fetch_details: bool = True,
        description_chars: int = 8000,
        skip_details_for: set[str] | None = None,
        stop_at_seen_page: bool = False,
    ) -> None:
        """``skip_details_for`` holds job ids seen in earlier runs: their detail pane
        is not opened again (fewer clicks on LinkedIn). With ``stop_at_seen_page``
        the walk stops at the first page whose title matches were all seen before,
        which is safe when results are sorted most-recent-first and polled often."""
        self.page = page
        self.criteria = criteria
        self.fetch_details = fetch_details
        self.description_chars = description_chars
        self.skip_details_for = skip_details_for or set()
        self.stop_at_seen_page = stop_at_seen_page
        self.total_results: int | None = None

    # ------------------------------------------------------------------ public
    def run(self) -> list[Job]:
        """Walk every results page, keep title matches, optionally enrich from the detail pane."""
        log.info("Search criteria: %s", self.criteria.describe())
        jobs: list[Job] = []
        seen_ids: set[str] = set()

        for page_no in range(1, self.criteria.max_pages + 1):
            url = self.criteria.search_url(start=(page_no - 1) * PAGE_SIZE)
            log.info("Page %d: %s", page_no, url)
            if not self._goto_results(url):
                log.info("Page %d has no job cards, stopping", page_no)
                break
            if page_no == 1:
                self._check_active_filters()
                self.total_results = self._read_result_count()
                if self.total_results is not None:
                    log.info("LinkedIn reports %d results", self.total_results)

            cards = self._collect_cards()
            new_cards = [c for c in cards if c["id"] and c["id"] not in seen_ids]
            seen_ids.update(c["id"] for c in new_cards)
            if not new_cards:
                log.info("Page %d repeated earlier results, stopping", page_no)
                break

            matched = unseen = 0
            for rank, card in enumerate(new_cards, start=1):
                job = self._card_to_job(card, page_no, rank)
                if not self.criteria.title_matches(job.title):
                    log.debug("Skip (title): %s | %s", job.title, job.company)
                    continue
                matched += 1
                seen_before = job.job_id in self.skip_details_for
                if seen_before:
                    log.debug("Seen before, details skipped: %s | %s", job.title, job.company)
                else:
                    unseen += 1
                    if self.fetch_details:
                        self._enrich_from_detail_pane(job)
                    log.info(
                        "Match: %s | %s | %s | %s | applicants=%s",
                        job.title, job.company, job.location, job.posted_text,
                        job.applicants_text or job.applicants,
                    )
                jobs.append(job)
            log.info("Page %d: %d cards, %d title matches, %d not seen before", page_no, len(new_cards), matched, unseen)

            if self.stop_at_seen_page and matched and not unseen:
                log.info("Page %d: every title match was seen in an earlier run, stopping", page_no)
                break
            if self._is_last_page():
                break
            self._pause(1.5, 3.0)

        log.info("Done: %d matching jobs out of %d cards scanned", len(jobs), len(seen_ids))
        return jobs

    # ---------------------------------------------------------------- navigation
    def _goto_results(self, url: str) -> bool:
        self.page.goto(url, wait_until="domcontentloaded")
        self._ensure_logged_in()
        try:
            self.page.wait_for_selector(CARD_SELECTOR, timeout=20_000)
        except PlaywrightTimeout:
            if self._no_results_shown():
                return False
            body = self.page.locator("body").inner_text(timeout=5_000)[:300].replace("\n", " ")
            raise SearchPageChangedError(
                f"No job cards found at {self.page.url}. LinkedIn may have changed the results "
                f"layout (it is retiring 'classic' job search). Page starts with: {body!r}"
            )
        self._pause(1.0, 2.0)
        return True

    def _ensure_logged_in(self) -> None:
        url = self.page.url
        if any(marker in url for marker in ("/login", "/authwall", "/checkpoint", "/uas/")):
            raise NotLoggedInError(
                f"LinkedIn redirected to {url}. Log in once in this browser profile "
                "(run with --keep-open, sign in, then rerun)."
            )

    def _no_results_shown(self) -> bool:
        try:
            text = self.page.locator("main").first.inner_text(timeout=3_000)
        except PlaywrightTimeout:
            return False
        return bool(re.search(r"No matching jobs|Jobs you may be interested in", text))

    def _is_last_page(self) -> bool:
        state = self.page.locator(".jobs-search-pagination__page-state")
        try:
            text = state.first.inner_text(timeout=2_000)
        except PlaywrightTimeout:
            return False  # no pagination widget: rely on the "no new cards" stop
        match = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", text)
        return bool(match) and int(match.group(1)) >= int(match.group(2))

    def _read_result_count(self) -> int | None:
        try:
            text = self.page.locator(".jobs-search-results-list__subtitle").first.inner_text(timeout=3_000)
        except PlaywrightTimeout:
            return None
        match = re.search(r"([\d,]+)\s+results?", text)
        return int(match.group(1).replace(",", "")) if match else None

    def _check_active_filters(self) -> None:
        """Log the filter chips LinkedIn shows as active and warn if one we asked for is missing."""
        active = self.page.evaluate(ACTIVE_FILTERS_JS)
        log.info("Active filters on page: %s", active)
        expected: list[str] = []
        if self.criteria.under_10_applicants:
            expected.append("Under 10 applicants")
        if self.criteria.posted_within.name == "PAST_24_HOURS":
            expected.append("Past 24 hours")
        if self.criteria.experience_levels:
            expected.append(f"Experience level {len(self.criteria.experience_levels)}")
        joined = " | ".join(active)
        for chip in expected:
            if chip not in joined:
                log.warning("Filter %r does not appear active on the page", chip)

    # ------------------------------------------------------------------ parsing
    def _collect_cards(self) -> list[dict]:
        """Scroll the virtualized results list, reading each card as it renders.

        LinkedIn fills in only the cards near the viewport and empties
        ("occludes") the rest, recycling the <li> elements as the list scrolls.
        A handle to a single card can therefore detach mid-scroll (Playwright
        then raises "Element is not attached to the DOM"), so we tolerate that,
        and we harvest the rendered cards on every step -- keeping the first good
        copy of each job id -- rather than reading them all once at the end, by
        when the early cards have been occluded again.
        """
        cards = self.page.locator(CARD_SELECTOR)
        by_id: dict[str, dict] = {}

        def harvest() -> None:
            for card in cards.evaluate_all(CARDS_JS):
                cid = card.get("id")
                if cid and card.get("rendered") and cid not in by_id:
                    by_id[cid] = card

        count = cards.count()
        for i in range(count):  # scrolling renders the occluded cards
            try:
                cards.nth(i).scroll_into_view_if_needed(timeout=3_000)
            except PlaywrightError as exc:  # timeout, or the card was recycled and detached
                log.debug("Could not scroll card %d into view: %s", i, exc)
            self.page.wait_for_timeout(random.randint(120, 250))
            harvest()

        harvest()
        unrendered = [c["id"] for c in cards.evaluate_all(CARDS_JS) if c["id"] and c["id"] not in by_id]
        if unrendered:
            log.warning("%d cards did not render (ids %s)", len(unrendered), unrendered[:5])
        return list(by_id.values())

    def _card_to_job(self, card: dict, page_no: int, rank: int) -> Job:
        location, workplace = split_location(card.get("location"))
        posted_text, posted_at = parse_posted(card.get("posted") or " ".join(card.get("footer") or []))
        job_id = card["id"]
        url = card.get("href") or f"https://www.linkedin.com/jobs/view/{job_id}/"
        return Job(
            job_id=job_id,
            title=clean_title(card.get("title")),
            url=url,
            company=card.get("company"),
            location=location,
            workplace_type=workplace,
            posted_text=posted_text,
            posted_at=posted_at,
            salary=next((m for m in card.get("metadata") or [] if m), None),
            easy_apply=bool(card.get("easyApply")),
            promoted=bool(card.get("promoted")),
            viewed=bool(card.get("viewed")),
            page=page_no,
            rank=rank,
        )

    def _enrich_from_detail_pane(self, job: Job) -> None:
        """Click the card so the right-hand pane shows this job, then read applicant count etc."""
        card = self.page.locator(f'[data-job-id="{job.job_id}"]').first
        try:
            card.scroll_into_view_if_needed(timeout=5_000)
            card.click(timeout=5_000)
            self.page.wait_for_url(lambda u: f"currentJobId={job.job_id}" in u, timeout=10_000)
            self.page.wait_for_selector(DETAIL_TITLE_SELECTOR, timeout=10_000)
            self._pause(0.6, 1.2)
            detail = self.page.evaluate(DETAIL_JS)
        except PlaywrightTimeout as exc:
            log.warning("Detail pane did not load for %s (%s): %s", job.job_id, job.title, exc)
            return

        detail_title = clean_title(detail.get("title"))
        if detail_title and detail_title.lower() != job.title.lower():
            log.warning("Detail pane title %r differs from card title %r", detail_title, job.title)

        tertiary = detail.get("tertiary") or ""
        job.applicants, job.applicants_text = parse_applicants(tertiary)
        if not job.posted_text:
            job.posted_text, job.posted_at = parse_posted(tertiary)
        if not job.company and detail.get("company"):
            job.company = detail["company"]
        if "Promoted" in tertiary:
            job.promoted = True

        pills = detail.get("pills") or []
        job.job_type = parse_job_type(pills)
        if not job.salary:
            job.salary = next((p for p in pills if re.search(r"/yr|/hr|/mo|\bINR\b|\$|€|£", p)), None)
        if not job.workplace_type:
            job.workplace_type = next((p for p in pills if p in ("On-site", "Hybrid", "Remote")), None)

        job.apply_label = detail.get("applyLabel")
        if job.apply_label and "easy apply" in job.apply_label.lower():
            job.easy_apply = True
        description = detail.get("description")
        if description:
            job.description = description[: self.description_chars]

    # ------------------------------------------------------------------ helpers
    def _pause(self, low: float, high: float) -> None:
        self.page.wait_for_timeout(int(random.uniform(low, high) * 1000))
