from __future__ import annotations

import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup


# Safety ceiling only. Set well above the largest real page so normal
# product/policy pages are never cut; truncation should effectively never fire.
MAX_BODY_CHARS = 50_000
MIN_MEANINGFUL_CHARS = 20
CONTENT_SELECTORS = [
    "#content",
    "#main",
    "#main-content",
    "#primary",
    ".content",
    ".main-content",
    ".entry-content",
    ".post-content",
    ".page-content",
    ".article-content",
]
REMOVAL_SELECTORS = [
    "script",
    "style",
    "noscript",
    "template",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    '[id*="cookie" i]',
    '[class*="cookie" i]',
    '[id*="consent" i]',
    '[class*="consent" i]',
    '[id*="gdpr" i]',
    '[class*="gdpr" i]',
    '[id*="chat" i][role="dialog"]',
    '[class*="intercom"]',
    '[class*="drift-frame"]',
    '[id*="hubspot-messages"]',
    "[hidden]",
    '[aria-hidden="true"]',
]


def extract(rendered_html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(rendered_html or "", "html.parser")
    _remove_non_content_elements(soup)

    content_element, quality = _select_content_element(soup)
    if content_element is None:
        heading = _first_h1_text(soup)
        return _result("", heading, "uncertain", False, 0)

    heading = _first_h1_text(content_element) or _first_h1_text(soup)
    body_text = _extract_text(content_element)
    body_text = _strip_heading_prefix(body_text, heading, content_element)
    body_text = _strip_end_boilerplate(body_text)

    if len(body_text) < MIN_MEANINGFUL_CHARS:
        quality = "uncertain"
        body_text = ""

    original_length = len(body_text)
    truncated_text, truncated = _truncate_at_word_boundary(body_text, MAX_BODY_CHARS)
    return _result(truncated_text, heading, quality, truncated, original_length)


def _remove_non_content_elements(soup: BeautifulSoup, selectors: list[str] | None = None) -> None:
    elements_to_remove = []
    for selector in (REMOVAL_SELECTORS if selectors is None else selectors):
        elements_to_remove.extend(_safe_select(soup, selector))

    for element in soup.find_all(style=True):
        if element is None:
            continue
        style = _collapse_ws(_safe_get(element, "style", "")).replace(" ", "").lower()
        if "display:none" in style:
            elements_to_remove.append(element)

    seen = set()
    for element in elements_to_remove:
        if element is None:
            continue
        element_id = id(element)
        if element_id in seen:
            continue
        seen.add(element_id)
        if getattr(element, "parent", None) is not None:
            element.decompose()


def _select_content_element(soup: BeautifulSoup):
    main = soup.find("main")
    if main:
        return main, "main"

    article = soup.find("article")
    if article:
        return article, "article"

    role_main = soup.find(attrs={"role": lambda value: value and value.lower() == "main"})
    if role_main:
        return role_main, "main"

    for selector in CONTENT_SELECTORS:
        element = _safe_select_one(soup, selector)
        if element:
            return element, "selector"

    fallback = _largest_text_block(soup)
    if fallback:
        return fallback, "fallback"
    return None, "uncertain"


def _largest_text_block(soup: BeautifulSoup):
    largest = None
    largest_length = 0
    for element in soup.find_all(["div", "section"]):
        if element is None:
            continue
        text_length = len(_extract_text(element))
        if text_length > largest_length:
            largest = element
            largest_length = text_length
    return largest if largest_length >= MIN_MEANINGFUL_CHARS else None


def _extract_text(element) -> str:
    if element is None:
        return ""
    return _collapse_ws(unescape(element.get_text(separator=" ", strip=True)))


def _first_h1_text(element) -> str:
    h1 = element.find("h1") if element else None
    return _collapse_ws(unescape(h1.get_text(separator=" ", strip=True))) if h1 else ""


def _strip_heading_prefix(body_text: str, heading: str, content_element) -> str:
    if not body_text or not heading:
        return body_text
    if not body_text.startswith(heading):
        return body_text
    if not _first_text_element_is_h1(content_element):
        return body_text
    remainder = body_text[len(heading) :].lstrip(" :-|")
    return remainder if remainder else body_text


def _first_text_element_is_h1(content_element) -> bool:
    if content_element is None:
        return False
    first_text = content_element.find(string=lambda value: bool(_collapse_ws(str(value))))
    return bool(first_text and first_text.find_parent("h1"))


def _strip_end_boilerplate(text: str) -> str:
    text = text.strip()
    if not text:
        return ""

    tail_200_start = max(0, len(text) - 200)
    tail_200 = text[tail_200_start:]
    copyright_match = re.search(r"©\s*\d{4}.*$", tail_200)
    if copyright_match:
        text = text[: tail_200_start + copyright_match.start()].strip()

    tail_100_start = max(0, len(text) - 100)
    tail_100 = text[tail_100_start:]
    contact_match = re.search(
        r"(?i)(contact\s+us|call\s+us|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}).*$",
        tail_100,
    )
    if contact_match:
        text = text[: tail_100_start + contact_match.start()].strip()
    return text


def _truncate_at_word_boundary(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    candidate = text[:max_chars].rstrip()
    boundary = candidate.rfind(" ")
    if boundary > 0:
        candidate = candidate[:boundary].rstrip()
    return candidate, True


def _result(body_text: str, heading: str, quality: str, truncated: bool, original_length: int) -> dict[str, Any]:
    return {
        "body_text": body_text,
        "main_heading_text": heading,
        "extraction_quality": quality,
        "truncated": truncated,
        "original_length": original_length,
        "char_count": len(body_text),
        "word_count": len(body_text.split()),
    }


def _collapse_ws(value: str) -> str:
    return " ".join((value or "").split())


def _safe_select(soup: BeautifulSoup, selector: str) -> list[Any]:
    try:
        return [element for element in soup.select(selector) if element is not None]
    except Exception:
        return []


def _safe_select_one(soup: BeautifulSoup, selector: str):
    try:
        return soup.select_one(selector)
    except Exception:
        return None


def _safe_get(element, key: str, default: str = "") -> str:
    if element is None:
        return default
    getter = getattr(element, "get", None)
    if getter is None:
        return default
    value = getter(key, default)
    return default if value is None else value
