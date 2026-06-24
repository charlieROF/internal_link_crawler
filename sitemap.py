from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from urllib import request
from urllib.parse import urlsplit, urlunsplit


# Backstops so a malformed or hostile sitemap can't run the crawler away.
MAX_SITEMAPS = 200
MAX_URLS = 200_000
FETCH_TIMEOUT = 30

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_SITEMAP_DIRECTIVE_RE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)\s*$")


@dataclass
class SitemapResult:
    urls: list[str] = field(default_factory=list)
    sitemaps_fetched: int = 0
    source: str = "none"  # "robots" | "fallback" | "none"
    errors: int = 0


def robots_url(start_url: str) -> str:
    parts = urlsplit(start_url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def fallback_sitemap_url(start_url: str) -> str:
    parts = urlsplit(start_url)
    return urlunsplit((parts.scheme, parts.netloc, "/sitemap.xml", "", ""))


def discover_sitemap_urls(start_url: str, user_agent: str, logger=None) -> SitemapResult:
    """Fetch robots.txt, find Sitemap: directives (falling back to /sitemap.xml),
    recursively expand sitemap index files, and return all listed URLs.

    Returns raw (un-normalized) URLs; the caller is responsible for normalizing,
    scope-filtering, and deduping against the frontier.
    """
    result = SitemapResult()

    roots = _sitemap_directives(start_url, user_agent, logger)
    if roots:
        result.source = "robots"
    else:
        roots = [fallback_sitemap_url(start_url)]
        result.source = "fallback"

    seen: set[str] = set()
    collected: list[str] = []
    queue = list(dict.fromkeys(roots))

    while queue:
        if result.sitemaps_fetched >= MAX_SITEMAPS or len(collected) >= MAX_URLS:
            if logger:
                logger.warning("Sitemap expansion hit a safety cap; stopping discovery.")
            break
        sitemap = queue.pop()
        if sitemap in seen:
            continue
        seen.add(sitemap)

        xml = _fetch(sitemap, user_agent, logger)
        if xml is None:
            result.errors += 1
            continue
        result.sitemaps_fetched += 1
        locs = _LOC_RE.findall(xml)

        if "<sitemapindex" in xml.lower():
            # Index file: its <loc> entries are child sitemaps to expand.
            for child in locs:
                if child not in seen:
                    queue.append(child)
        else:
            collected.extend(locs)

    # dedupe, preserve order
    result.urls = list(dict.fromkeys(collected))[:MAX_URLS]
    if logger:
        logger.info(
            f"Sitemap discovery via {result.source}: {result.sitemaps_fetched} sitemap(s), "
            f"{len(result.urls)} URLs, {result.errors} fetch error(s)."
        )
    return result


def _sitemap_directives(start_url: str, user_agent: str, logger=None) -> list[str]:
    robots = _fetch(robots_url(start_url), user_agent, logger)
    if not robots:
        return []
    return list(dict.fromkeys(_SITEMAP_DIRECTIVE_RE.findall(robots)))


def _fetch(url: str, user_agent: str, logger=None) -> str | None:
    try:
        req = request.Request(url, headers={"User-Agent": user_agent})
        with request.urlopen(req, timeout=FETCH_TIMEOUT) as response:
            data = response.read()
    except Exception as exc:
        if logger:
            logger.warning(f"Could not fetch sitemap resource {url}: {exc}")
        return None
    if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except Exception as exc:
            if logger:
                logger.warning(f"Could not gunzip sitemap {url}: {exc}")
            return None
    return data.decode("utf-8", "replace")
