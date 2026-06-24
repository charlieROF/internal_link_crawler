from __future__ import annotations

import re
from dataclasses import dataclass
from posixpath import normpath
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


SKIPPED_SCHEMES = {"mailto", "tel", "javascript", "ftp", "file"}
# Recognizable TLDs used to detect scheme-less bare-domain hrefs like
# "healthline.com/path" that would otherwise be mis-joined as relative paths.
_COMMON_TLDS = {
    "com", "net", "org", "io", "co", "gov", "edu", "info", "biz", "ai", "app",
    "dev", "store", "shop", "me", "tv", "us", "uk", "ca", "au", "de", "fr",
    "es", "it", "nl", "eu", "in", "jp", "cn", "br", "mx", "ru", "se", "no",
}
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
# Repairs hand-authored single-slash scheme typos (https:/host -> https://host),
# which browsers tolerate; without this they resolve to junk like
# biobidet.com/www.healthline.com/... and 404.
_MALFORMED_SCHEME_RE = re.compile(r"^(https?):/(?![/])", re.IGNORECASE)
_PREFIXED_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "ftp:", "file:")
_HOST_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.")
DEFAULT_PORTS = {"http": 80, "https": 443}
NON_HTML_ASSET_EXTENSIONS = {
    ".7z",
    ".avi",
    ".avif",
    ".bmp",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".eot",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".otf",
    ".pdf",
    ".png",
    ".rar",
    ".rss",
    ".svg",
    ".tar",
    ".ttf",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xml",
    ".zip",
}


@dataclass(frozen=True)
class DomainPolicy:
    registered_domain: str
    include_subdomains: bool = False
    # Canonical start host (www-stripped). Used to enforce exact-host scope
    # when include_subdomains is False. Falls back to registered_domain if unset.
    start_host: str = ""


def normalize_url(raw_url: str, base_url: Optional[str] = None) -> Optional[str]:
    raw_url = (raw_url or "").strip()
    if not raw_url or raw_url == "#":
        return None
    if raw_url.startswith("#"):
        return None

    raw_url = _prenormalize_href(raw_url)
    resolved = urljoin(base_url, raw_url) if base_url else raw_url
    parts = urlsplit(resolved)
    scheme = parts.scheme.lower()
    if scheme in SKIPPED_SCHEMES or scheme not in {"http", "https"}:
        return None

    host = (parts.hostname or "").lower()
    if not host:
        return None

    port = parts.port
    netloc = host
    if port and DEFAULT_PORTS.get(scheme) != port:
        netloc = f"{host}:{port}"

    path = _normalize_path(parts.path)
    query = _normalize_query(parts.query)
    return urlunsplit((scheme, netloc, path, query, ""))


def _prenormalize_href(raw_url: str) -> str:
    """Rewrite scheme-less bare-domain hrefs to absolute URLs before urljoin.

    Without this, an href like ``www.healthline.com/x`` or ``healthline.com/x``
    is treated as a relative path and joined onto the page base, producing junk
    like ``https://biobidet.com/www.healthline.com/x``. Protocol-relative hrefs
    (``//host/path``) and true relative paths (``/path``, ``path``) are left for
    urljoin to resolve against the base.
    """
    raw_url = _MALFORMED_SCHEME_RE.sub(lambda m: f"{m.group(1)}://", raw_url)
    if _SCHEME_RE.match(raw_url) or raw_url.lower().startswith(_PREFIXED_SCHEMES):
        return raw_url  # already absolute or a non-http scheme
    if raw_url.startswith(("/", "#", "?")):
        return raw_url  # site-relative or fragment/query-only
    if _looks_like_bare_host(raw_url):
        return f"https://{raw_url}"
    return raw_url


def _looks_like_bare_host(href: str) -> bool:
    candidate = href.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "." not in candidate or candidate.startswith(".") or candidate.endswith("."):
        return False
    if any(char not in _HOST_CHARS for char in candidate):
        return False
    labels = candidate.split(".")
    if len(labels) < 2 or not all(labels):
        return False
    if candidate.lower().startswith("www."):
        return True
    return labels[-1].lower() in _COMMON_TLDS


def registered_domain(url_or_host: str) -> str:
    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_internal_url(url: str, policy: DomainPolicy) -> bool:
    host = hostname(url)
    if host.startswith("www."):
        host = host[4:]
    if policy.include_subdomains:
        return host == policy.registered_domain or host.endswith(f".{policy.registered_domain}")
    # Subdomains excluded: require an exact match against the start host so
    # sibling subdomains (e.g. answers.example.com) are treated as external.
    target = policy.start_host or policy.registered_domain
    return host == target


def canonicalize_start_url(start_url: str) -> str:
    normalized = normalize_url(start_url)
    if not normalized:
        raise ValueError(f"Invalid start URL: {start_url}")
    return normalized


def reconcile_key(raw_url: str, pre=None) -> Optional[str]:
    """Aggressive cross-source key for set-diffing the crawl against external
    sources (sitemap, SEMRush, GSC). Drops scheme/query/fragment, lowercases the
    host (minus www) and path, and strips the trailing slash so trivially
    different spellings of the same page collapse to one key.

    ``pre`` is the crawl's frontier canonicalizer (item 3); applying it here keeps
    the reconciliation consistent with how the crawl deduped URLs (e.g. Shopify
    collection-scoped product aliases), avoiding false mismatches.
    """
    normalized = normalize_url(raw_url)
    if not normalized:
        return None
    if pre is not None:
        normalized = pre(normalized)
        if not normalized:
            return None
    parts = urlsplit(normalized)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    path = parts.path.lower()
    if len(path) > 1:
        path = path.rstrip("/")
    return f"{host}{path}"


def is_non_html_asset_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    last_segment = path.rsplit("/", 1)[-1]
    if "." not in last_segment:
        return False
    extension = f".{last_segment.rsplit('.', 1)[-1]}"
    return extension in NON_HTML_ASSET_EXTENSIONS


def _normalize_path(path: str) -> str:
    if not path:
        return "/"

    had_trailing_slash = path.endswith("/")
    normalized = normpath(path)
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if normalized == "/.":
        normalized = "/"

    last_segment = normalized.rsplit("/", 1)[-1]
    has_file_extension = "." in last_segment
    if normalized != "/" and (had_trailing_slash or not has_file_extension):
        normalized = f"{normalized.rstrip('/')}/"
    return normalized


def _normalize_query(query: str) -> str:
    if not query:
        return ""
    params = parse_qsl(query, keep_blank_values=True)
    params.sort(key=lambda item: (item[0], item[1]))
    return urlencode(params, doseq=True)
