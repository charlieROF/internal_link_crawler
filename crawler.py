from __future__ import annotations

import asyncio
import statistics
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib import robotparser
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from tqdm import tqdm

from boilerplate import apply_boilerplate_detection
from content_extractor import extract as extract_content
from link_extractor import ExtractedLink, extract_links, extract_page_metadata
from output import (
    CrawlLogger,
    delete_checkpoint,
    load_checkpoint,
    write_checkpoint,
    write_links_csv,
    write_page_content_csv,
    write_pages_csv,
    write_summary,
)
from url_utils import (
    DomainPolicy,
    canonicalize_start_url,
    is_internal_url,
    is_non_html_asset_url,
    normalize_url,
    registered_domain,
)


CHECKPOINT_INTERVAL = 50
DEFAULT_CONTACT_EMAIL = "operator@example.com"
PAGE_TIMEOUT_MS = 30_000


@dataclass
class CrawlConfig:
    start_url: str
    output_dir: Path
    max_pages: Optional[int] = 2000
    rate_limit: float = 1.0
    wait_buffer_seconds: float = 2.0
    boilerplate_threshold: float = 0.80
    include_subdomains: bool = False
    ignore_robots: bool = False
    contact_email: Optional[str] = None
    body_text_enabled: bool = True


@dataclass
class EnqueueStats:
    queued: int = 0
    skipped_assets: int = 0


class InternalLinkCrawler:
    def __init__(self, config: CrawlConfig):
        self.config = config
        self.start_url = canonicalize_start_url(config.start_url)
        self.policy = DomainPolicy(
            registered_domain=registered_domain(self.start_url),
            include_subdomains=config.include_subdomains,
        )
        self.logger = CrawlLogger(config.output_dir)
        self.user_agent = self._user_agent()
        self.robot_parser: Optional[robotparser.RobotFileParser] = None
        self.queue: deque[tuple[str, int]] = deque([(self.start_url, 0)])
        self.queued: set[str] = {self.start_url}
        self.visited: set[str] = set()
        self.pages: list[dict[str, Any]] = []
        self.links: list[ExtractedLink] = []
        self.page_content_records: list[dict[str, Any]] = []
        self.pages_skipped_robots = 0
        self.crawl_started = datetime.now(timezone.utc)
        self.max_pages_reached = False
        self._last_request_at: dict[str, float] = {}

    async def run(self, resume_state: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if resume_state:
            self._load_state(resume_state)

        await self._load_robots()
        started_monotonic = time.monotonic()

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context(user_agent=self.user_agent)
                progress = tqdm(
                    total=self._progress_total(),
                    initial=self._progress_initial(),
                    desc="URLs processed",
                    unit="url",
                    dynamic_ncols=True,
                )
                try:
                    while self.queue and self._can_crawl_more():
                        url, depth = self.queue.popleft()
                        if url in self.visited:
                            continue
                        if not self._robots_allowed(url):
                            self.pages_skipped_robots += 1
                            self.visited.add(url)
                            self.logger.info(f"Skipped by robots.txt: {url}")
                            tqdm.write(f"Skipped by robots.txt depth={depth}: {url}")
                            progress.update(1)
                            progress.set_postfix(queue=len(self.queue), pages=len(self.pages))
                            continue

                        tqdm.write(f"Fetching depth={depth} queue={len(self.queue)}: {url}")
                        await self._respect_rate_limit(url)
                        page_result, page_links = await self._crawl_one(context, url, depth)
                        self.visited.add(url)
                        self.pages.append(page_result)
                        self.links.extend(page_links)
                        enqueue_stats = self._enqueue_internal_links(page_links, depth)

                        if self.config.max_pages is None and enqueue_stats.queued:
                            progress.total = (progress.total or 0) + enqueue_stats.queued
                            progress.refresh()

                        progress.update(1)
                        progress.set_postfix(queue=len(self.queue), pages=len(self.pages))
                        self._write_live_fetch_result(page_result, page_links, enqueue_stats, depth)

                        if len(self.pages) % CHECKPOINT_INTERVAL == 0:
                            self.save_checkpoint()
                            tqdm.write(f"Checkpoint saved after {len(self.pages)} pages.")

                    self.max_pages_reached = (
                        self.config.max_pages is not None
                        and len(self.pages) >= self.config.max_pages
                        and bool(self.queue)
                    )
                finally:
                    progress.close()
                    await context.close()
                    await browser.close()
        except KeyboardInterrupt:
            self.save_checkpoint()
            self.logger.warning("Keyboard interrupt received; checkpoint saved.")
            raise

        summary = self._finalize(started_monotonic)
        delete_checkpoint(self.config.output_dir)
        return summary

    def save_checkpoint(self) -> None:
        write_checkpoint(
            self.config.output_dir,
            {
                "queue": list(self.queue),
                "queued": sorted(self.queued),
                "visited": sorted(self.visited),
                "pages": self.pages,
                "links": self.links,
                "page_content_records": self.page_content_records if self.config.body_text_enabled else [],
                "pages_skipped_robots": self.pages_skipped_robots,
                "crawl_started": self.crawl_started.isoformat(),
                "max_pages_reached": self.max_pages_reached,
            },
        )

    async def _crawl_one(self, context, url: str, depth: int) -> tuple[dict[str, Any], list[ExtractedLink]]:
        for attempt in (1, 2):
            page = await context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            started = time.monotonic()
            timestamp = datetime.now(timezone.utc).isoformat()
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                await self._wait_for_render(page, url, started)
                final_url = normalize_url(page.url) or page.url
                status = response.status if response else 0
                duration_ms = int((time.monotonic() - started) * 1000)
                self.logger.info(f"Fetched {url} status={status} duration_ms={duration_ms}")

                if status >= 400:
                    return self._page_row(url, final_url, status, depth, duration_ms, timestamp, error=""), []

                content_type = ""
                if response:
                    content_type = (await response.header_value("content-type")) or ""
                if content_type and "html" not in content_type.lower():
                    return self._page_row(
                        url,
                        final_url,
                        status,
                        depth,
                        duration_ms,
                        timestamp,
                        error=f"Non-HTML response: {content_type}",
                    ), []

                html = await page.content()
                metadata = extract_page_metadata(html)
                page_links = extract_links(html, final_url, self.policy, self.logger)
                if self.config.body_text_enabled:
                    self._extract_body_content(html, url)
                internal_count = sum(1 for link in page_links if link.is_internal)
                external_count = len(page_links) - internal_count
                row = self._page_row(
                    url=url,
                    final_url=final_url,
                    status_code=status,
                    depth=depth,
                    duration_ms=duration_ms,
                    timestamp=timestamp,
                    title=metadata.title,
                    h1=metadata.h1,
                    meta_description=metadata.meta_description,
                    internal_outlinks_count=internal_count,
                    external_outlinks_count=external_count,
                )
                return row, page_links
            except PlaywrightTimeoutError as exc:
                duration_ms = int((time.monotonic() - started) * 1000)
                self.logger.warning(f"Navigation timeout on {url}; attempt {attempt}: {exc}")
                if attempt == 2:
                    return self._page_row(url, url, 0, depth, duration_ms, timestamp, error=str(exc)), []
            except PlaywrightError as exc:
                duration_ms = int((time.monotonic() - started) * 1000)
                self.logger.error(f"Playwright error on {url} attempt {attempt}: {exc}")
                if attempt == 2:
                    return self._page_row(url, url, 0, depth, duration_ms, timestamp, error=str(exc)), []
            except Exception as exc:
                duration_ms = int((time.monotonic() - started) * 1000)
                self.logger.error(f"Failed {url}: {exc}")
                return self._page_row(url, url, 0, depth, duration_ms, timestamp, error=str(exc)), []
            finally:
                await page.close()

        return self._page_row(url, url, 0, depth, 0, datetime.now(timezone.utc).isoformat(), error="Unknown error"), []

    async def _wait_for_render(self, page, url: str, started: float) -> None:
        remaining_ms = max(0, PAGE_TIMEOUT_MS - int((time.monotonic() - started) * 1000))
        if remaining_ms > 0:
            try:
                await page.wait_for_load_state("networkidle", timeout=remaining_ms)
            except PlaywrightTimeoutError:
                self.logger.warning(f"Network idle timeout on {url}; using currently rendered HTML.")

        remaining_ms = max(0, PAGE_TIMEOUT_MS - int((time.monotonic() - started) * 1000))
        requested_buffer_ms = int(self.config.wait_buffer_seconds * 1000)
        buffer_ms = min(requested_buffer_ms, remaining_ms)
        if requested_buffer_ms and buffer_ms < requested_buffer_ms:
            self.logger.warning(f"Wait buffer timeout on {url}; capped by 30 second page limit.")
        if buffer_ms > 0:
            await page.wait_for_timeout(buffer_ms)

    def _finalize(self, started_monotonic: float) -> dict[str, Any]:
        inlink_counts: dict[str, int] = {}
        for link in self.links:
            if link.is_internal:
                inlink_counts[link.target_url] = inlink_counts.get(link.target_url, 0) + 1

        for page in self.pages:
            page["internal_inlinks_count"] = inlink_counts.get(page["url"], 0)

        boilerplate_count = apply_boilerplate_detection(
            self.links,
            len(self.pages),
            self.config.boilerplate_threshold,
        )

        write_pages_csv(self.config.output_dir, self.pages)
        write_links_csv(self.config.output_dir, self.links)
        if self.config.body_text_enabled and self.page_content_records:
            write_page_content_csv(self.page_content_records, self.config.output_dir)

        completed = datetime.now(timezone.utc)
        summary = {
            "start_url": self.start_url,
            "crawl_started": self.crawl_started.isoformat(),
            "crawl_completed": completed.isoformat(),
            "total_duration_seconds": round(time.monotonic() - started_monotonic, 3),
            "pages_crawled": len(self.pages),
            "pages_failed": sum(1 for page in self.pages if page.get("error")),
            "pages_skipped_robots": self.pages_skipped_robots,
            "total_links_found": len(self.links),
            "internal_links_found": sum(1 for link in self.links if link.is_internal),
            "external_links_found": sum(1 for link in self.links if not link.is_internal),
            "boilerplate_links_flagged": boilerplate_count,
            "max_pages_reached": self.max_pages_reached,
            "configuration": {
                "max_pages": self.config.max_pages,
                "rate_limit": self.config.rate_limit,
                "wait_buffer_seconds": self.config.wait_buffer_seconds,
                "include_subdomains": self.config.include_subdomains,
                "ignore_robots": self.config.ignore_robots,
                "boilerplate_threshold": self.config.boilerplate_threshold,
            },
            "body_extraction": self._body_extraction_summary(),
        }
        write_summary(self.config.output_dir, summary)
        self._print_summary(summary)
        return summary

    async def _load_robots(self) -> None:
        if self.config.ignore_robots:
            self.logger.warning("robots.txt ignored because --ignore-robots was set.")
            return

        robots_url = _robots_url(self.start_url)
        parser = robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            await asyncio.to_thread(parser.read)
            self.robot_parser = parser
            self.logger.info(f"Loaded robots.txt from {robots_url}")
        except Exception as exc:
            self.logger.warning(f"Could not fetch robots.txt from {robots_url}: {exc}")
            self.robot_parser = None

    def _robots_allowed(self, url: str) -> bool:
        if self.config.ignore_robots or not self.robot_parser:
            return True
        return self.robot_parser.can_fetch(self.user_agent, url)

    async def _respect_rate_limit(self, url: str) -> None:
        if self.config.rate_limit <= 0:
            return
        host = urlsplit(url).netloc
        delay = 1.0 / self.config.rate_limit
        elapsed = time.monotonic() - self._last_request_at.get(host, 0)
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_request_at[host] = time.monotonic()

    def _can_crawl_more(self) -> bool:
        return self.config.max_pages is None or len(self.pages) < self.config.max_pages

    def _progress_initial(self) -> int:
        return len(self.pages) + self.pages_skipped_robots

    def _progress_total(self) -> int:
        if self.config.max_pages is not None:
            return self.config.max_pages
        return max(len(self.queued), self._progress_initial() + len(self.queue), 1)

    def _enqueue_internal_links(self, links: list[ExtractedLink], depth: int) -> EnqueueStats:
        stats = EnqueueStats()
        for link in links:
            if not link.is_internal:
                continue
            if link.target_url in self.visited or link.target_url in self.queued:
                continue
            if not is_internal_url(link.target_url, self.policy):
                continue
            if is_non_html_asset_url(link.target_url):
                stats.skipped_assets += 1
                continue
            self.queue.append((link.target_url, depth + 1))
            self.queued.add(link.target_url)
            stats.queued += 1
        return stats

    def _write_live_fetch_result(
        self,
        page_result: dict[str, Any],
        page_links: list[ExtractedLink],
        enqueue_stats: EnqueueStats,
        depth: int,
    ) -> None:
        internal_links = sum(1 for link in page_links if link.is_internal)
        external_links = len(page_links) - internal_links
        message = (
            f"Fetched status={page_result['status_code']} depth={depth} "
            f"duration={page_result['crawl_duration_ms']}ms "
            f"links={len(page_links)} internal={internal_links} external={external_links} "
            f"queued=+{enqueue_stats.queued} assets_skipped={enqueue_stats.skipped_assets}: "
            f"{page_result['final_url']}"
        )
        if page_result.get("error"):
            message = f"{message} | error={page_result['error']}"
        tqdm.write(message)

    def _extract_body_content(self, html: str, url: str) -> None:
        extracted_at = datetime.now(timezone.utc).isoformat()
        try:
            result = extract_content(html, url)
            record = {
                "url": url,
                "main_heading_text": result["main_heading_text"],
                "body_text": result["body_text"],
                "extraction_quality": result["extraction_quality"],
                "truncated": result["truncated"],
                "original_length": result["original_length"],
                "char_count": result["char_count"],
                "extracted_at": extracted_at,
            }
            self.page_content_records.append(record)
            self.logger.info(
                "Body extracted from "
                f"{url}: quality={record['extraction_quality']}, "
                f"chars={record['char_count']}, truncated={record['truncated']}"
            )
            if record["extraction_quality"] == "fallback":
                self.logger.warning(
                    f"Body extraction fallback for {url}: no main/article/selector match, used largest text block"
                )
        except Exception as exc:
            self.logger.error(f"Body extraction failed for {url}: {exc}")
            self.page_content_records.append(
                {
                    "url": url,
                    "main_heading_text": "",
                    "body_text": "",
                    "extraction_quality": "error",
                    "truncated": False,
                    "original_length": 0,
                    "char_count": 0,
                    "extracted_at": extracted_at,
                }
            )

    def _body_extraction_summary(self) -> dict[str, Any]:
        if not self.config.body_text_enabled:
            return {
                "enabled": False,
                "pages_with_extraction": 0,
                "extraction_quality_breakdown": {
                    "main": 0,
                    "article": 0,
                    "selector": 0,
                    "fallback": 0,
                    "uncertain": 0,
                    "error": 0,
                },
                "truncated_count": 0,
                "average_char_count": 0,
                "median_char_count": 0,
            }

        qualities = ["main", "article", "selector", "fallback", "uncertain", "error"]
        char_counts = [int(record.get("char_count", 0)) for record in self.page_content_records]
        return {
            "enabled": True,
            "pages_with_extraction": len(self.page_content_records),
            "extraction_quality_breakdown": {
                quality: sum(1 for record in self.page_content_records if record.get("extraction_quality") == quality)
                for quality in qualities
            },
            "truncated_count": sum(1 for record in self.page_content_records if record.get("truncated")),
            "average_char_count": round(sum(char_counts) / len(char_counts)) if char_counts else 0,
            "median_char_count": round(statistics.median(char_counts)) if char_counts else 0,
        }

    def _load_state(self, state: dict[str, Any]) -> None:
        self.queue = deque((item[0], int(item[1])) for item in state.get("queue", []))
        self.queued = set(state.get("queued", [])) or {url for url, _depth in self.queue}
        self.visited = set(state.get("visited", []))
        self.pages = list(state.get("pages", []))
        self.links = [ExtractedLink(**item) for item in state.get("links", [])]
        self.page_content_records = list(state.get("page_content_records", []))
        self.pages_skipped_robots = int(state.get("pages_skipped_robots", 0))
        if state.get("crawl_started"):
            self.crawl_started = datetime.fromisoformat(state["crawl_started"])
        self.max_pages_reached = bool(state.get("max_pages_reached", False))

    def _user_agent(self) -> str:
        contact = self.config.contact_email or DEFAULT_CONTACT_EMAIL
        if not self.config.contact_email:
            CrawlLogger(self.config.output_dir).warning(
                f"No --contact-email provided; using default contact email {DEFAULT_CONTACT_EMAIL}."
            )
        return f"InternalLinkCrawler/1.0 (+contact: {contact})"

    def _page_row(
        self,
        url: str,
        final_url: str,
        status_code: int,
        depth: int,
        duration_ms: int,
        timestamp: str,
        title: str = "",
        h1: str = "",
        meta_description: str = "",
        internal_outlinks_count: int = 0,
        external_outlinks_count: int = 0,
        error: str = "",
    ) -> dict[str, Any]:
        return {
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "crawl_depth": depth,
            "title": title,
            "h1": h1,
            "meta_description": meta_description,
            "internal_outlinks_count": internal_outlinks_count,
            "external_outlinks_count": external_outlinks_count,
            "internal_inlinks_count": 0,
            "crawl_duration_ms": duration_ms,
            "crawl_timestamp": timestamp,
            "error": error,
        }

    def _print_summary(self, summary: dict[str, Any]) -> None:
        print("\nCrawl complete")
        print(f"Pages crawled: {summary['pages_crawled']}")
        print(f"Pages failed: {summary['pages_failed']}")
        print(f"Robots skips: {summary['pages_skipped_robots']}")
        print(f"Links found: {summary['total_links_found']}")
        print(f"Internal links: {summary['internal_links_found']}")
        print(f"External links: {summary['external_links_found']}")
        print(f"Max pages reached: {summary['max_pages_reached']}")


def load_existing_checkpoint(output_dir: Path) -> dict[str, Any]:
    return load_checkpoint(output_dir)


def _robots_url(start_url: str) -> str:
    parts = urlsplit(start_url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
