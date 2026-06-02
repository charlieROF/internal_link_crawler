from __future__ import annotations

from dataclasses import dataclass
from posixpath import normpath
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


SKIPPED_SCHEMES = {"mailto", "tel", "javascript", "ftp", "file"}
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


def normalize_url(raw_url: str, base_url: Optional[str] = None) -> Optional[str]:
    raw_url = (raw_url or "").strip()
    if not raw_url or raw_url == "#":
        return None
    if raw_url.startswith("#"):
        return None

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
    return registered_domain(host) == policy.registered_domain


def canonicalize_start_url(start_url: str) -> str:
    normalized = normalize_url(start_url)
    if not normalized:
        raise ValueError(f"Invalid start URL: {start_url}")
    return normalized


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
