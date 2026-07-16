from __future__ import annotations

import asyncio
import re
import statistics
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib import robotparser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from tqdm import tqdm

from block_extractor import extract_blocks
from boilerplate import apply_boilerplate_detection
from content_extractor import extract as extract_content
from link_extractor import ExtractedLink, extract_links, extract_page_metadata
from output import (
    CrawlLogger,
    delete_checkpoint,
    load_checkpoint,
    read_url_list_csv,
    write_checkpoint,
    write_links_csv,
    write_page_blocks_csv,
    write_page_blocks_json,
    write_page_content_csv,
    write_pages_csv,
    write_reconciliation_csv,
    write_summary,
)
from sitemap import discover_sitemap_urls
from url_utils import (
    DomainPolicy,
    canonicalize_start_url,
    hostname,
    is_internal_url,
    is_non_html_asset_url,
    normalize_url,
    reconcile_key,
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
    networkidle_timeout_seconds: float = 0.0
    boilerplate_threshold: float = 0.80
    include_subdomains: bool = False
    ignore_robots: bool = False
    contact_email: Optional[str] = None
    body_text_enabled: bool = True
    content_blocks_enabled: bool = False
    ignored_crawl_query_params: tuple[str, ...] = ("_pos", "_sid", "_ss", "_fid", "bvroute", "bvstate")
    canonicalize_shopify_product_urls: bool = True
    sitemap_enabled: bool = True
    semrush_csv: Optional[str] = None
    gsc_csv: Optional[str] = None
    seed_urls_files: tuple[str, ...] = ()


@dataclass
class EnqueueStats:
    queued: int = 0
    skipped_assets: int = 0
    skipped_query_duplicates: int = 0


class InternalLinkCrawler:
    def __init__(self, config: CrawlConfig):
        self.config = config
        self.start_url = canonicalize_start_url(config.start_url)
        start_host = hostname(self.start_url)
        if start_host.startswith("www."):
            start_host = start_host[4:]
        self.policy = DomainPolicy(
            registered_domain=registered_domain(self.start_url),
            include_subdomains=config.include_subdomains,
            start_host=start_host,
        )
        self.logger = CrawlLogger(config.output_dir)
        self.user_agent = self._user_agent()
        self.robot_parser: Optional[robotparser.RobotFileParser] = None
        self.queue: deque[tuple[str, int]] = deque([(self.start_url, 0)])
        self.queued: set[str] = {self.start_url}
        self.queued_crawl_keys: set[str] = {self._crawl_key(self.start_url)}
        self.visited: set[str] = set()
        self.visited_crawl_keys: set[str] = set()
        self.pages: list[dict[str, Any]] = []
        self.links: list[ExtractedLink] = []
        self.page_content_records: list[dict[str, Any]] = []
        self.page_block_records: list[dict[str, Any]] = []
        self.pages_skipped_robots = 0
        self.crawl_started = datetime.now(timezone.utc)
        self.max_pages_reached = False
        self._last_request_at: dict[str, float] = {}
        self.sitemap_urls: set[str] = set()  # reconcile keys of all sitemap URLs
        self.sitemap_repr: dict[str, str] = {}  # reconcile key -> representative raw URL
        self.sitemap_url_count = 0
        self.sitemap_seeded_count = 0
        self.sitemap_source = "disabled"
        self.seeded_extra_count = 0

    async def run(self, resume_state: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if resume_state:
            self._load_state(resume_state)

        await self._load_robots()
        await self._seed_from_sitemap()
        self._seed_extra_urls()
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
                        url = self._canonical_crawl_url(url)
                        crawl_key = self._crawl_key(url)
                        if crawl_key in self.visited_crawl_keys:
                            continue
                        if not self._robots_allowed(url):
                            self.pages_skipped_robots += 1
                            self.visited.add(url)
                            self.visited_crawl_keys.add(crawl_key)
                            self.logger.info(f"Skipped by robots.txt: {url}")
                            tqdm.write(f"Skipped by robots.txt depth={depth}: {url}")
                            progress.update(1)
                            progress.set_postfix(queue=len(self.queue), pages=len(self.pages))
                            continue

                        tqdm.write(f"Fetching depth={depth} queue={len(self.queue)}: {url}")
                        await self._respect_rate_limit(url)
                        page_result, page_links = await self._crawl_one(context, url, depth)
                        self.visited.add(url)
                        self.visited_crawl_keys.add(crawl_key)
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
                "page_block_records": self.page_block_records if self.config.content_blocks_enabled else [],
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
                redirect_count, redirect_chain = await self._redirect_chain(response)
                self.logger.info(f"Fetched {url} status={status} duration_ms={duration_ms}")

                if status >= 400:
                    return self._page_row(
                        url, final_url, status, depth, duration_ms, timestamp, error="",
                        redirect_count=redirect_count, redirect_chain=redirect_chain,
                    ), []

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
                        redirect_count=redirect_count,
                        redirect_chain=redirect_chain,
                    ), []

                html = await page.content()
                metadata = extract_page_metadata(html)
                page_links = extract_links(html, final_url, self.policy, self.logger)
                word_count = 0
                if self.config.body_text_enabled:
                    content_record = self._extract_body_content(html, url)
                    word_count = int(content_record.get("word_count", 0))
                if self.config.content_blocks_enabled:
                    self._extract_content_blocks(html, url, final_url)

                x_robots_tag = ""
                if response:
                    x_robots_tag = (await response.header_value("x-robots-tag")) or ""
                canonical_url = ""
                if metadata.canonical_url:
                    canonical_url = normalize_url(metadata.canonical_url, final_url) or metadata.canonical_url
                indexable = _is_indexable(metadata.meta_robots, x_robots_tag)

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
                    canonical_url=canonical_url,
                    meta_robots=metadata.meta_robots,
                    x_robots_tag=_collapse_inline_ws(x_robots_tag),
                    indexable=indexable,
                    word_count=word_count,
                    redirect_count=redirect_count,
                    redirect_chain=redirect_chain,
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

    async def _redirect_chain(self, response) -> tuple[int, str]:
        """Walk the navigation's redirect history. Returns (hop_count, chain), where
        chain is "<url> (<status>) > ... > <final_url> (<status>)". hop_count is the
        number of redirects before the final response (0 when there were none)."""
        if response is None:
            return 0, ""
        requests = []
        request = response.request
        while request is not None:
            requests.append(request)
            request = request.redirected_from
        requests.reverse()  # earliest hop first

        hops: list[str] = []
        for req in requests:
            try:
                resp = await req.response()
                status = resp.status if resp else 0
            except Exception:
                status = 0
            hops.append(f"{req.url} ({status})")
        redirect_count = max(0, len(hops) - 1)
        if redirect_count == 0:
            return 0, ""
        return redirect_count, " > ".join(hops)

    async def _wait_for_render(self, page, url: str, started: float) -> None:
        # Primary gate is domcontentloaded (already awaited by page.goto) plus the
        # fixed buffer below. networkidle is NOT a reliable gate on modern stacks:
        # Shopify holds analytics/chat/pixel connections open indefinitely, so it
        # never fires and every page burns the full page timeout. It is therefore
        # opt-in (networkidle_timeout_seconds > 0) and never the primary gate.
        if self.config.networkidle_timeout_seconds > 0:
            remaining_ms = max(0, PAGE_TIMEOUT_MS - int((time.monotonic() - started) * 1000))
            networkidle_timeout_ms = int(self.config.networkidle_timeout_seconds * 1000)
            networkidle_wait_ms = min(networkidle_timeout_ms, remaining_ms)
            if networkidle_wait_ms > 0:
                try:
                    await page.wait_for_load_state("networkidle", timeout=networkidle_wait_ms)
                except PlaywrightTimeoutError:
                    self.logger.warning(
                        f"Network idle timeout after {self.config.networkidle_timeout_seconds}s on "
                        f"{url}; using currently rendered HTML."
                    )

        remaining_ms = max(0, PAGE_TIMEOUT_MS - int((time.monotonic() - started) * 1000))
        requested_buffer_ms = int(self.config.wait_buffer_seconds * 1000)
        buffer_ms = min(requested_buffer_ms, remaining_ms)
        if requested_buffer_ms and buffer_ms < requested_buffer_ms:
            self.logger.warning(f"Wait buffer timeout on {url}; capped by 30 second page limit.")
        if buffer_ms > 0:
            await page.wait_for_timeout(buffer_ms)

    def _finalize(self, started_monotonic: float) -> dict[str, Any]:
        # Canonicalize the link graph with the same function used for the crawl
        # frontier so source_url/target_url reconcile against pages.csv. Without
        # this, collection-scoped product aliases (/collections/x/products/y)
        # appear as phantom "never crawled" internal targets even though their
        # canonical /products/y was crawled.
        for link in self.links:
            link.source_url = self._canonical_crawl_url(link.source_url)
            if link.is_internal:
                link.target_url = self._canonical_crawl_url(link.target_url)

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
        if self.config.content_blocks_enabled and self.page_block_records:
            write_page_blocks_csv(self.config.output_dir, self.page_block_records)
            write_page_blocks_json(self.config.output_dir, self.page_block_records)

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
                "networkidle_timeout_seconds": self.config.networkidle_timeout_seconds,
                "ignored_crawl_query_params": list(self.config.ignored_crawl_query_params),
                "canonicalize_shopify_product_urls": self.config.canonicalize_shopify_product_urls,
                "include_subdomains": self.config.include_subdomains,
                "ignore_robots": self.config.ignore_robots,
                "boilerplate_threshold": self.config.boilerplate_threshold,
                "sitemap_enabled": self.config.sitemap_enabled,
            },
            "body_extraction": self._body_extraction_summary(),
            "coverage_reconciliation": self._build_reconciliation(),
        }
        write_summary(self.config.output_dir, summary)
        self._print_summary(summary)
        return summary

    def _build_reconciliation(self) -> dict[str, Any]:
        """Reconcile the crawled URL set against the sitemap and any provided
        SEMRush / GSC URL lists. Writes coverage_reconciliation.csv and returns a
        top-line summary block for crawl_summary.json. All sources are normalized
        with the shared reconcile_key so diffs aren't full of false mismatches."""
        pre = self._canonical_crawl_url
        crawl_repr: dict[str, str] = {}
        for page in self.pages:
            key = reconcile_key(page["url"], pre)
            if not key:
                continue
            if key not in crawl_repr or page.get("status_code") == 200:
                crawl_repr[key] = page["url"]
        crawled = set(crawl_repr)

        sources: dict[str, set[str]] = {}
        repr_maps: dict[str, dict[str, str]] = {"crawl": crawl_repr}
        if self.config.sitemap_enabled:
            sources["sitemap"] = set(self.sitemap_urls)
            repr_maps["sitemap"] = self.sitemap_repr
        if self.config.semrush_csv:
            sources["semrush"], repr_maps["semrush"] = self._load_external_keys(self.config.semrush_csv, "semrush")
        if self.config.gsc_csv:
            sources["gsc"], repr_maps["gsc"] = self._load_external_keys(self.config.gsc_csv, "gsc")

        # write the per-URL artifact (union of every source)
        all_keys = set(crawled)
        for keys in sources.values():
            all_keys |= keys

        def representative(key: str) -> str:
            for name in ("crawl", "sitemap", "semrush", "gsc"):
                if key in repr_maps.get(name, {}):
                    return repr_maps[name][key]
            return f"https://{key}"

        rows = []
        for key in sorted(all_keys):
            in_crawl = key in crawled
            row = {
                "url": representative(key),
                "in_crawl": in_crawl,
                "in_sitemap": key in sources.get("sitemap", set()),
                "in_semrush": key in sources.get("semrush", set()),
                "in_gsc": key in sources.get("gsc", set()),
                "classification": "crawled" if in_crawl else "discovery_gap",
            }
            rows.append(row)
        write_reconciliation_csv(self.config.output_dir, rows)

        summary: dict[str, Any] = {"crawled_urls": len(crawled)}
        for name, keys in sources.items():
            covered = len(keys & crawled)
            block = {
                "total": len(keys),
                "covered_by_crawl": covered,
                "not_crawled": len(keys - crawled),
                "coverage_pct": round(100 * covered / len(keys), 2) if keys else 0.0,
            }
            if name == "sitemap":
                block["source"] = self.sitemap_source
                block["crawled_not_in_sitemap"] = len(crawled - keys)
            summary[name] = block
        return summary

    def _load_external_keys(self, path: str, label: str) -> tuple[set[str], dict[str, str]]:
        keys: set[str] = set()
        repr_map: dict[str, str] = {}
        try:
            raw_urls = read_url_list_csv(path)
        except Exception as exc:
            self.logger.warning(f"Could not read {label} CSV {path}: {exc}")
            return keys, repr_map
        for raw in raw_urls:
            key = reconcile_key(raw, self._canonical_crawl_url)
            if key:
                keys.add(key)
                repr_map.setdefault(key, raw)
        self.logger.info(f"Loaded {len(keys)} {label} URLs for reconciliation from {path}.")
        return keys, repr_map

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

    async def _seed_from_sitemap(self) -> None:
        if not self.config.sitemap_enabled:
            self.logger.info("Sitemap seeding disabled (--no-sitemap).")
            return

        result = await asyncio.to_thread(
            discover_sitemap_urls, self.start_url, self.user_agent, self.logger
        )
        self.sitemap_source = result.source
        seeded = 0
        out_of_scope = 0
        for raw in result.urls:
            key = reconcile_key(raw, self._canonical_crawl_url)
            if key:
                self.sitemap_urls.add(key)
                self.sitemap_repr.setdefault(key, raw)
            normalized = normalize_url(raw)
            if not normalized:
                continue
            crawl_url = self._canonical_crawl_url(normalized)
            if not is_internal_url(crawl_url, self.policy):
                out_of_scope += 1
                continue
            if is_non_html_asset_url(crawl_url):
                continue
            crawl_key = self._crawl_key(crawl_url)
            if crawl_key in self.visited_crawl_keys or crawl_key in self.queued_crawl_keys:
                continue
            self.queue.append((crawl_url, 0))
            self.queued.add(crawl_url)
            self.queued_crawl_keys.add(crawl_key)
            seeded += 1

        self.sitemap_url_count = len(self.sitemap_urls)
        self.sitemap_seeded_count = seeded
        self.logger.info(
            f"Sitemap seeded {seeded} new URLs into the frontier "
            f"({self.sitemap_url_count} sitemap URLs total, {out_of_scope} out-of-scope skipped)."
        )
        tqdm.write(
            f"Sitemap ({self.sitemap_source}): {self.sitemap_url_count} URLs, "
            f"{seeded} added to crawl frontier."
        )

    def _seed_extra_urls(self) -> None:
        """Seed additional URLs from user-supplied files into the frontier. Use to
        force-fetch coverage-gap URLs (e.g. SEMRush/GSC pages) that are neither
        internally linked nor in the sitemap."""
        if not self.config.seed_urls_files:
            return
        total = 0
        for path in self.config.seed_urls_files:
            try:
                raw_urls = read_url_list_csv(path)
            except Exception as exc:
                self.logger.warning(f"Could not read seed URL file {path}: {exc}")
                continue
            added = 0
            for raw in raw_urls:
                normalized = normalize_url(raw)
                if not normalized:
                    continue
                crawl_url = self._canonical_crawl_url(normalized)
                if not is_internal_url(crawl_url, self.policy):
                    continue
                if is_non_html_asset_url(crawl_url):
                    continue
                crawl_key = self._crawl_key(crawl_url)
                if crawl_key in self.visited_crawl_keys or crawl_key in self.queued_crawl_keys:
                    continue
                self.queue.append((crawl_url, 0))
                self.queued.add(crawl_url)
                self.queued_crawl_keys.add(crawl_key)
                added += 1
                total += 1
            self.logger.info(f"Seeded {added} new URLs from {path}.")
        self.seeded_extra_count = total
        if total:
            tqdm.write(f"Seed files: {total} extra URLs added to crawl frontier.")

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
            crawl_url = self._canonical_crawl_url(link.target_url)
            crawl_key = self._crawl_key(crawl_url)
            if crawl_key in self.visited_crawl_keys or crawl_key in self.queued_crawl_keys:
                if crawl_url != link.target_url:
                    stats.skipped_query_duplicates += 1
                continue
            if not is_internal_url(crawl_url, self.policy):
                continue
            if is_non_html_asset_url(crawl_url):
                stats.skipped_assets += 1
                continue
            self.queue.append((crawl_url, depth + 1))
            self.queued.add(crawl_url)
            self.queued_crawl_keys.add(crawl_key)
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
            f"queued=+{enqueue_stats.queued} assets_skipped={enqueue_stats.skipped_assets} "
            f"query_dupes_skipped={enqueue_stats.skipped_query_duplicates}: "
            f"{page_result['final_url']}"
        )
        if page_result.get("error"):
            message = f"{message} | error={page_result['error']}"
        tqdm.write(message)

    def _extract_body_content(self, html: str, url: str) -> dict[str, Any]:
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
                "word_count": result["word_count"],
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
            return record
        except Exception as exc:
            self.logger.error(f"Body extraction failed for {url}: {exc}")
            record = {
                "url": url,
                "main_heading_text": "",
                "body_text": "",
                "extraction_quality": "error",
                "truncated": False,
                "original_length": 0,
                "char_count": 0,
                "word_count": 0,
                "extracted_at": extracted_at,
            }
            self.page_content_records.append(record)
            return record

    def _extract_content_blocks(self, html: str, url: str, final_url: str) -> None:
        try:
            record = extract_blocks(html, url, final_url)
            self.page_block_records.append(record)
            self.logger.info(
                f"Blocks extracted from {url}: {len(record['blocks'])} blocks, "
                f"quality={record['extraction_quality']}"
            )
        except Exception as exc:
            self.logger.error(f"Block extraction failed for {url}: {exc}")
            self.page_block_records.append(
                {"url": url, "extraction_quality": "error", "blocks": []}
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
        self.visited = set(state.get("visited", []))
        self.pages = list(state.get("pages", []))
        self.visited_crawl_keys = {
            self._crawl_key(url)
            for url in list(self.visited) + [page.get("url", "") for page in self.pages]
            if url
        }
        self.queue = deque()
        self.queued = set()
        self.queued_crawl_keys = set()
        removed_duplicate_queue_urls = 0
        for item in state.get("queue", []):
            url, depth = item[0], int(item[1])
            crawl_url = self._canonical_crawl_url(url)
            crawl_key = self._crawl_key(crawl_url)
            if crawl_key in self.visited_crawl_keys or crawl_key in self.queued_crawl_keys:
                removed_duplicate_queue_urls += 1
                continue
            self.queue.append((crawl_url, depth))
            self.queued.add(crawl_url)
            self.queued_crawl_keys.add(crawl_key)
        if removed_duplicate_queue_urls:
            self.logger.info(
                f"Compacted resume queue by removing {removed_duplicate_queue_urls} query-duplicate URLs."
            )
        self.links = [ExtractedLink(**item) for item in state.get("links", [])]
        self.page_content_records = list(state.get("page_content_records", []))
        self.page_block_records = list(state.get("page_block_records", []))
        self.pages_skipped_robots = int(state.get("pages_skipped_robots", 0))
        if state.get("crawl_started"):
            self.crawl_started = datetime.fromisoformat(state["crawl_started"])
        self.max_pages_reached = bool(state.get("max_pages_reached", False))

    def _canonical_crawl_url(self, url: str) -> str:
        path = urlsplit(url).path
        if self.config.canonicalize_shopify_product_urls:
            path = re.sub(r"^/collections/[^/]+/products/([^/]+)/?$", r"/products/\1/", path)
        if not self.config.ignored_crawl_query_params:
            parts = urlsplit(url)
            return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
        ignored = set(self.config.ignored_crawl_query_params)
        parts = urlsplit(url)
        params = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in ignored
        ]
        query = urlencode(params, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))

    def _crawl_key(self, url: str) -> str:
        return self._canonical_crawl_url(url)

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
        canonical_url: str = "",
        meta_robots: str = "",
        x_robots_tag: str = "",
        indexable: Any = "",
        word_count: int = 0,
        redirect_count: int = 0,
        redirect_chain: str = "",
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
            "canonical_url": canonical_url,
            "meta_robots": meta_robots,
            "x_robots_tag": x_robots_tag,
            "indexable": indexable,
            "word_count": word_count,
            "redirect_count": redirect_count,
            "redirect_chain": redirect_chain,
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
        recon = summary.get("coverage_reconciliation", {})
        sm = recon.get("sitemap")
        if sm:
            print(
                f"Sitemap coverage: {sm['covered_by_crawl']}/{sm['total']} "
                f"({sm['coverage_pct']}%); {sm['not_crawled']} in sitemap not crawled"
            )
        for name in ("semrush", "gsc"):
            block = recon.get(name)
            if block:
                print(
                    f"{name.upper()} coverage: {block['covered_by_crawl']}/{block['total']} "
                    f"({block['coverage_pct']}%); {block['not_crawled']} not crawled"
                )


def load_existing_checkpoint(output_dir: Path) -> dict[str, Any]:
    return load_checkpoint(output_dir)


def _robots_url(start_url: str) -> str:
    parts = urlsplit(start_url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def _is_indexable(meta_robots: str, x_robots_tag: str) -> bool:
    """A page is non-indexable if ``noindex`` appears, or the standalone ``none``
    directive is present, in either the robots meta tag or the X-Robots-Tag header.

    ``none`` is only matched as a bare directive so valued directives such as
    ``max-image-preview:none`` are not mistaken for it.
    """
    combined = f"{meta_robots},{x_robots_tag}".lower()
    if re.search(r"\bnoindex\b", combined):
        return False
    for part in re.split(r"[,;]", combined):
        if ":" not in part and part.strip() == "none":
            return False
    return True


def _collapse_inline_ws(value: str) -> str:
    return " ".join((value or "").split())
