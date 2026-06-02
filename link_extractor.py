from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

from url_utils import DomainPolicy, is_internal_url, normalize_url


@dataclass
class PageMetadata:
    title: str
    h1: str
    meta_description: str


@dataclass
class ExtractedLink:
    source_url: str
    target_url: str
    anchor_text: str
    link_context: str
    is_internal: bool
    is_boilerplate: Optional[bool]
    rel_attribute: str


def extract_page_metadata(html: str) -> PageMetadata:
    soup = BeautifulSoup(html, "html.parser")
    title = _collapse_ws(soup.title.get_text(" ")) if soup.title else ""
    h1_tag = soup.find("h1")
    h1 = _collapse_ws(h1_tag.get_text(" ")) if h1_tag else ""
    meta = soup.find("meta", attrs={"name": lambda value: value and value.lower() == "description"})
    meta_description = _collapse_ws(meta.get("content", "")) if meta else ""
    return PageMetadata(title=title, h1=h1, meta_description=meta_description)


def extract_links(html: str, source_url: str, policy: DomainPolicy, logger=None) -> list[ExtractedLink]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[ExtractedLink] = []

    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        target_url = normalize_url(href, source_url)
        if not target_url:
            continue

        try:
            is_internal = is_internal_url(target_url, policy)
        except ValueError as exc:
            if logger:
                logger.warning(f"Invalid URL skipped on {source_url}: {href!r} ({exc})")
            continue

        rel = anchor.get("rel")
        rel_attribute = " ".join(rel) if isinstance(rel, list) else (rel or "")
        links.append(
            ExtractedLink(
                source_url=source_url,
                target_url=target_url,
                anchor_text=_anchor_text(anchor),
                link_context=_link_context(anchor),
                is_internal=is_internal,
                is_boilerplate=None,
                rel_attribute=_collapse_ws(rel_attribute),
            )
        )
    return links


def _anchor_text(anchor) -> str:
    text = _collapse_ws(anchor.get_text(" "))
    if text:
        return text
    image = anchor.find("img")
    if image:
        alt = _collapse_ws(image.get("alt", ""))
        return f"[image: {alt}]" if alt else "[image]"
    return ""


def _link_context(anchor) -> str:
    parent = anchor.parent
    if not parent:
        return ""

    before_parts: list[str] = []
    after_parts: list[str] = []
    seen_anchor = False

    for child in parent.children:
        if child is anchor:
            seen_anchor = True
            continue
        if getattr(child, "name", None) in {"script", "style", "noscript"}:
            continue
        text = _collapse_ws(child.get_text(" ") if hasattr(child, "get_text") else str(child))
        if not text:
            continue
        if seen_anchor:
            after_parts.append(text)
        else:
            before_parts.append(text)

    before = _collapse_ws(" ".join(before_parts))[-100:]
    after = _collapse_ws(" ".join(after_parts))[:100]
    context = _collapse_ws(f"{before} {after}")
    if context:
        return context
    return parent.name or ""


def _collapse_ws(value: str) -> str:
    return " ".join((value or "").split())
