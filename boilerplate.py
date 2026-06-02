from __future__ import annotations

import string
from collections import defaultdict
from typing import Optional

from link_extractor import ExtractedLink


def apply_boilerplate_detection(
    links: list[ExtractedLink],
    crawled_page_count: int,
    threshold: float,
) -> Optional[int]:
    if crawled_page_count < 20:
        for link in links:
            link.is_boilerplate = None
        return None

    sources_by_group: dict[tuple[str, str], set[str]] = defaultdict(set)
    for link in links:
        if link.is_internal:
            key = (_normalize_anchor(link.anchor_text), link.target_url)
            sources_by_group[key].add(link.source_url)

    boilerplate_groups = {
        key
        for key, sources in sources_by_group.items()
        if (len(sources) / crawled_page_count) > threshold
    }

    flagged = 0
    for link in links:
        key = (_normalize_anchor(link.anchor_text), link.target_url)
        is_boilerplate = link.is_internal and key in boilerplate_groups
        link.is_boilerplate = is_boilerplate
        if is_boilerplate:
            flagged += 1
    return flagged


def _normalize_anchor(anchor_text: str) -> str:
    collapsed = " ".join((anchor_text or "").lower().split())
    return collapsed.strip(string.punctuation + " ")
