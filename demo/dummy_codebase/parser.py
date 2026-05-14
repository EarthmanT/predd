"""Data parsing utilities."""

import re


def paginate(items: list, page: int, page_size: int) -> list:
    """Return one page of items (0-indexed).

    BUG: off-by-one on last page — see DEMO-11.
    """
    start = page * page_size
    end = page * page_size      # <-- bug: should be (page + 1) * page_size
    return items[start:end]


def extract_key_value(text: str, key: str) -> str:
    """Extract 'key: value' from multi-line text."""
    m = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""
