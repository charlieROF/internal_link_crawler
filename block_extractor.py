from __future__ import annotations

"""Structured content-block extraction for content-inventory / rewrite work.

Unlike content_extractor (which flattens a page to one prose string for SEO
grounding), this walks the main content in document order and emits typed blocks
with their link targets intact, so a page can be reconstructed into content
frameworks (headline / intro / feature grid / social proof / CTA).

Deliberately structural, not semantic: it reports what the markup IS (heading,
paragraph, list, blockquote, cta), never what a block MEANS ("Feature Grid").
Semantic labelling is the consuming prompt's job — hardcoding it here would be
brittle across templates.
"""

from typing import Any, Optional

from bs4 import BeautifulSoup, NavigableString

from content_extractor import (
    REMOVAL_SELECTORS,
    _collapse_ws,
    _remove_non_content_elements,
)
from url_utils import normalize_url


# Same noise removal as the prose extractor, but KEEP <form>: form labels and
# button copy are real page content when the goal is rewriting the site.
BLOCK_REMOVAL_SELECTORS = [s for s in REMOVAL_SELECTORS if s != "form"]

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCKISH = HEADINGS | {"p", "ul", "ol", "blockquote", "table", "form"}
# class/id tokens that mark a link as a styled call-to-action rather than prose
CTA_HINTS = ("btn", "button", "cta")
MIN_TEXT = 1


def extract_blocks(rendered_html: str, url: str, base_url: Optional[str] = None) -> dict[str, Any]:
    soup = BeautifulSoup(rendered_html or "", "html.parser")
    _remove_non_content_elements(soup, BLOCK_REMOVAL_SELECTORS)

    content, quality = _select_block_root(soup)
    if content is None:
        return {"url": url, "extraction_quality": "uncertain", "blocks": []}

    blocks: list[dict[str, Any]] = []
    _walk(content, blocks, base_url or url)
    for index, block in enumerate(blocks, 1):
        block["order"] = index
    return {"url": url, "extraction_quality": quality, "blocks": blocks}


def _select_block_root(soup: BeautifulSoup):
    """Pick the root to walk for a content inventory.

    Prefer a real main/article landmark. Otherwise fall back to the whole <body>
    (chrome already stripped) — NOT the largest text block. A content inventory
    must capture every block on the page; "biggest container wins" is a prose
    heuristic that silently drops hero copy, testimonials, and CTAs.
    """
    main = soup.find("main")
    if main is not None:
        return main, "main"
    article = soup.find("article")
    if article is not None:
        return article, "article"
    role_main = soup.find(attrs={"role": lambda v: v and v.lower() == "main"})
    if role_main is not None:
        return role_main, "main"
    if soup.body is not None:
        return soup.body, "body"
    return (soup, "body") if soup else (None, "uncertain")


def _walk(element, blocks: list[dict[str, Any]], base_url: str) -> None:
    for child in getattr(element, "children", []):
        # Raw text sitting directly in a container (very common — many templates
        # never use <p>). Without this the prose is silently dropped.
        if isinstance(child, NavigableString):
            text = _collapse_ws(str(child))
            if text:
                _emit(blocks, {"type": "text", "text": text})
            continue
        name = getattr(child, "name", None)
        if name is None:
            continue
        if _is_text_leaf(child):
            _emit(blocks, {"type": "text", "text": _text(child)})
        elif name in HEADINGS:
            _emit(blocks, {"type": "heading", "level": int(name[1]), "text": _text(child)})
        elif name == "p":
            _emit(blocks, {"type": "paragraph", "text": _text(child), "links": _inline_links(child, base_url)})
        elif name in ("ul", "ol"):
            items = [_text(li) for li in child.find_all("li", recursive=False)]
            items = [item for item in items if item]
            if items:
                _emit(blocks, {"type": "list", "ordered": name == "ol", "items": items})
        elif name == "blockquote":
            _emit(blocks, {"type": "blockquote", "text": _text(child)})
        elif name == "table":
            _emit(blocks, {"type": "table", "text": _text(child)})
        elif name == "form":
            _walk_form(child, blocks, base_url)
        elif name in ("img", "figure"):
            image = child if name == "img" else child.find("img")
            if image is not None:
                _emit(blocks, {
                    "type": "image",
                    "alt": _collapse_ws(image.get("alt", "") or ""),
                    "href": _resolve(image.get("src", ""), base_url),
                    "text": _text(child.find("figcaption")) if name == "figure" else "",
                })
            else:
                _walk(child, blocks, base_url)
        elif name == "a":
            # A link wrapping real block structure (a card) is a container, not a CTA.
            if _has_block_children(child):
                _walk(child, blocks, base_url)
            else:
                _emit(blocks, {
                    "type": "cta" if _looks_like_cta(child) else "link",
                    "text": _text(child),
                    "href": _resolve(child.get("href", ""), base_url),
                })
        elif name == "button":
            _emit(blocks, {"type": "button", "text": _text(child)})
        else:
            _walk(child, blocks, base_url)


def _walk_form(form, blocks: list[dict[str, Any]], base_url: str) -> None:
    for label in form.find_all("label"):
        text = _text(label)
        if text:
            _emit(blocks, {"type": "form_label", "text": text})
    for field in form.find_all(["input", "textarea", "select"]):
        placeholder = _collapse_ws(field.get("placeholder", "") or "")
        if placeholder:
            _emit(blocks, {"type": "form_placeholder", "text": placeholder})
    for button in form.find_all(["button"]):
        text = _text(button)
        if text:
            _emit(blocks, {"type": "form_button", "text": text})
    for submit in form.find_all("input", attrs={"type": lambda v: v and v.lower() in ("submit", "button")}):
        value = _collapse_ws(submit.get("value", "") or "")
        if value:
            _emit(blocks, {"type": "form_button", "text": value})


def _emit(blocks: list[dict[str, Any]], block: dict[str, Any]) -> None:
    has_text = len(block.get("text", "") or "") >= MIN_TEXT
    if block["type"] == "list" and block.get("items"):
        blocks.append(block)
        return
    if block["type"] == "image" and (block.get("alt") or block.get("href")):
        blocks.append(block)
        return
    if has_text:
        blocks.append(block)


def _has_block_children(element) -> bool:
    return element.find(list(BLOCKISH)) is not None


def _is_text_leaf(element) -> bool:
    """A container (div/span/etc.) whose whole job is holding a run of prose.

    Many templates put body copy in bare <div>s. Treating those as one text block
    keeps the copy intact instead of fragmenting or losing it. Containers holding
    structure or interactive children are walked instead, so their blocks/links
    are emitted properly.
    """
    if element.name in HEADINGS or element.name in BLOCKISH:
        return False
    if element.name in ("img", "figure", "a", "button", "br"):
        return False
    if _has_block_children(element):
        return False
    if element.find(["a", "button", "img"]) is not None:
        return False
    return bool(_text(element))


def _looks_like_cta(anchor) -> bool:
    """Only markup-declared calls to action. Previously any standalone link
    counted, which mislabelled gallery/carousel controls ('10' -> '#') as CTAs.
    Un-marked standalone links are still captured, just typed as 'link'."""
    if (anchor.get("role") or "").lower() == "button":
        return True
    tokens = " ".join((anchor.get("class") or []) + [anchor.get("id") or ""]).lower()
    return any(hint in tokens for hint in CTA_HINTS)


def _inline_links(element, base_url: str) -> list[dict[str, str]]:
    links = []
    for anchor in element.find_all("a"):
        text = _text(anchor)
        href = _resolve(anchor.get("href", ""), base_url)
        if text and href:
            links.append({"text": text, "href": href})
    return links


def _resolve(href: str, base_url: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    return normalize_url(href, base_url) or href


def _text(element) -> str:
    if element is None:
        return ""
    return _collapse_ws(element.get_text(" ", strip=True))
