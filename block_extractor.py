from __future__ import annotations

"""Structured content-block extraction for content-inventory / rewrite work.

Runs IN the rendered page rather than over the HTML string. That matters:

* Hidden slider/carousel clones are skipped, so the inventory never contains
  copy that isn't on the live page.
* Computed font-size/weight give the real visual hierarchy. Markup lies — on
  armclark.com the subheadline renders 62px/900 while its H1 is 30px/700, and
  H5 is used for body copy. Anything trusting tag names gets it backwards.
* Geometry gives true reading order and reveals grid rows (siblings sharing a y).

Deliberately structural, not semantic: it reports what a block IS and how it
renders, never what it MEANS ("Feature Grid"). Semantic mapping is the consuming
prompt's job — hardcoding it would be brittle across templates.
"""

from typing import Any, Optional

from url_utils import normalize_url


ROW_TOLERANCE_PX = 24  # blocks whose tops sit within this share a visual row

# Walks the live DOM and returns raw blocks + render signals. Kept in one
# evaluate() call (~100ms/page, no extra page loads).
_EXTRACT_JS = r"""
() => {
  const CTA_HINTS = ['btn', 'button', 'cta'];
  const HEADINGS = new Set(['H1','H2','H3','H4','H5','H6']);
  const BLOCKISH = ['H1','H2','H3','H4','H5','H6','P','UL','OL','BLOCKQUOTE','TABLE','FORM'];
  const BLOCKISH_SET = new Set(BLOCKISH);
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','NAV','HEADER','FOOTER',
                        'ASIDE','SVG','IFRAME','CANVAS','SELECT','OPTION']);
  const NON_TEXT_LEAF = new Set(['IMG','FIGURE','A','BUTTON','BR','INPUT','TEXTAREA','LABEL','VIDEO']);

  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const hidden = el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return true;
    if (parseFloat(cs.opacity) < 0.05) return true;
    return false;
  };
  const rendered = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };

  const root = document.querySelector('main')
            || document.querySelector('article')
            || document.querySelector('[role=main]')
            || document.body;
  const quality = document.querySelector('main') ? 'main'
                : document.querySelector('article') ? 'article'
                : document.querySelector('[role=main]') ? 'main' : 'body';
  if (!root) return { quality: 'uncertain', blocks: [] };

  // Section = nearest <section>/section-ish ancestor, else the top-level child
  // of the root that contains the block. Stable and template-truthful.
  const sectionIds = new Map();
  const sectionOf = el => {
    let found = null;
    try { found = el.closest('section,[class*="section" i],[class*="hero" i]'); } catch (e) { found = null; }
    if (!found || !root.contains(found) || found === root) {
      let cur = el;
      while (cur && cur.parentElement && cur.parentElement !== root) cur = cur.parentElement;
      found = cur;
    }
    if (!found) return 0;
    if (!sectionIds.has(found)) sectionIds.set(found, sectionIds.size + 1);
    return sectionIds.get(found);
  };

  const out = [];
  const push = (o, el) => {
    if (el) {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      o.x = Math.round(r.left + window.scrollX);
      o.y = Math.round(r.top + window.scrollY);
      o.w = Math.round(r.width);
      o.h = Math.round(r.height);
      o.font_size = Math.round(parseFloat(cs.fontSize) || 0);
      o.font_weight = cs.fontWeight;
      o.section = sectionOf(el);
    }
    out.push(o);
  };

  const isCta = a => {
    if ((a.getAttribute('role') || '').toLowerCase() === 'button') return true;
    const tok = ((a.className || '') + ' ' + (a.id || '')).toString().toLowerCase();
    return CTA_HINTS.some(h => tok.includes(h));
  };
  const hasBlockChild = el => !!el.querySelector(BLOCKISH.join(','));
  const isTextLeaf = el => {
    if (BLOCKISH_SET.has(el.tagName) || NON_TEXT_LEAF.has(el.tagName)) return false;
    if (hasBlockChild(el)) return false;
    if (el.querySelector('a,button,img')) return false;
    return !!clean(el.innerText);
  };
  const linksIn = el => [...el.querySelectorAll('a')]
    .map(a => ({ text: clean(a.innerText), href: a.getAttribute('href') || '' }))
    .filter(l => l.text && l.href);

  const walk = el => {
    for (const node of el.childNodes) {
      if (node.nodeType === 3) {                       // raw text in a container
        const t = clean(node.textContent);
        if (t) push({ type: 'text', text: t }, el);
        continue;
      }
      if (node.nodeType !== 1) continue;
      const tag = node.tagName;
      if (SKIP.has(tag)) continue;
      if (hidden(node)) continue;                      // skips hidden subtrees (slider clones)

      if (HEADINGS.has(tag)) {
        const t = clean(node.innerText);
        if (t && rendered(node)) push({ type: 'heading', level: +tag[1], text: t }, node);
      } else if (tag === 'P') {
        const t = clean(node.innerText);
        if (t && rendered(node)) push({ type: 'paragraph', text: t, links: linksIn(node) }, node);
      } else if (tag === 'UL' || tag === 'OL') {
        const items = [...node.children]
          .filter(li => li.tagName === 'LI' && !hidden(li))
          .map(li => clean(li.innerText)).filter(Boolean);
        if (items.length) push({ type: 'list', ordered: tag === 'OL', items: items }, node);
      } else if (tag === 'BLOCKQUOTE') {
        const t = clean(node.innerText); if (t) push({ type: 'blockquote', text: t }, node);
      } else if (tag === 'TABLE') {
        const t = clean(node.innerText); if (t) push({ type: 'table', text: t }, node);
      } else if (tag === 'LABEL') {
        const t = clean(node.innerText); if (t && rendered(node)) push({ type: 'form_label', text: t }, node);
      } else if (tag === 'INPUT' || tag === 'TEXTAREA') {
        const ty = (node.getAttribute('type') || '').toLowerCase();
        if (ty === 'submit' || ty === 'button') {
          const v = clean(node.value); if (v) push({ type: 'form_button', text: v }, node);
        } else {
          const ph = clean(node.getAttribute('placeholder'));
          if (ph) push({ type: 'form_placeholder', text: ph }, node);
        }
      } else if (tag === 'IMG') {
        if (rendered(node)) push({ type: 'image', alt: clean(node.getAttribute('alt')),
                                  href: node.getAttribute('src') || '', text: '' }, node);
      } else if (tag === 'FIGURE') {
        const img = node.querySelector('img');
        const cap = node.querySelector('figcaption');
        if (img) push({ type: 'image', alt: clean(img.getAttribute('alt')),
                        href: img.getAttribute('src') || '', text: cap ? clean(cap.innerText) : '' }, node);
        else walk(node);
      } else if (tag === 'A') {
        if (hasBlockChild(node)) walk(node);
        else {
          const t = clean(node.innerText);
          if (t) push({ type: isCta(node) ? 'cta' : 'link', text: t,
                        href: node.getAttribute('href') || '' }, node);
          // Image links (gallery/card items) wrap an <img> in a leaf anchor. Emit
          // the image too — its alt is real copy. Targeted rather than a full walk
          // so the anchor's own text isn't duplicated as a text block.
          node.querySelectorAll('img').forEach(im => {
            if (!hidden(im) && rendered(im)) {
              push({ type: 'image', alt: clean(im.getAttribute('alt')),
                     href: im.getAttribute('src') || '', text: '' }, im);
            }
          });
        }
      } else if (tag === 'BUTTON') {
        const t = clean(node.innerText); if (t && rendered(node)) push({ type: 'button', text: t }, node);
      } else if (isTextLeaf(node)) {
        const t = clean(node.innerText); if (t && rendered(node)) push({ type: 'text', text: t }, node);
      } else {
        walk(node);
      }
    }
  };

  walk(root);
  return { quality: quality, blocks: out };
}
"""


async def extract_blocks(page, url: str, base_url: Optional[str] = None) -> dict[str, Any]:
    """Extract rendered content blocks from an open Playwright page."""
    result = await page.evaluate(_EXTRACT_JS)
    blocks = result.get("blocks", []) or []
    base = base_url or url

    for block in blocks:
        if block.get("href"):
            block["href"] = _resolve(block["href"], base)
        for link in block.get("links", []) or []:
            link["href"] = _resolve(link.get("href", ""), base)
        block["font_weight"] = _weight(block.get("font_weight"))

    blocks = _assign_rows(blocks)
    for index, block in enumerate(blocks, 1):
        block["order"] = index

    return {
        "url": url,
        "extraction_quality": result.get("quality", "uncertain"),
        "blocks": blocks,
        "sections": _group_sections(blocks),
    }


def _assign_rows(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tag blocks that share a visual row within their section. Repeated siblings
    on one row are what a feature/card grid actually is, so this makes grids
    detectable without guessing at class names."""
    by_section: dict[int, list[dict[str, Any]]] = {}
    for block in blocks:
        by_section.setdefault(block.get("section", 0), []).append(block)

    for section_blocks in by_section.values():
        rows: list[int] = []  # representative y per row
        for block in section_blocks:
            y = block.get("y", 0)
            match = next((i for i, ry in enumerate(rows) if abs(ry - y) <= ROW_TOLERANCE_PX), None)
            if match is None:
                rows.append(y)
                match = len(rows) - 1
            block["row"] = match + 1
    return blocks


def _group_sections(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    index_by_id: dict[int, int] = {}
    for block in blocks:
        section_id = block.get("section", 0)
        if section_id not in index_by_id:
            index_by_id[section_id] = len(sections)
            sections.append({"section": section_id, "y": block.get("y", 0), "blocks": []})
        sections[index_by_id[section_id]]["blocks"].append(block)
    return sections


def _weight(value: Any) -> int:
    text = str(value or "").strip().lower()
    if text == "normal":
        return 400
    if text == "bold":
        return 700
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _resolve(href: str, base_url: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    return normalize_url(href, base_url) or href
