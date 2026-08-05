#!/usr/bin/env python3
"""Shared DOM element inventory — AST/parser based, NOT regex.

Counts buttons/links/inputs/images/forms/headings in an HTML document using
Python's stdlib ``html.parser`` (a real tokenizer that builds a tag stream),
instead of ``re.findall(r"<a\\b", html)``.

Why this matters (the bug it replaces):
    The regex version counted tag-like text ANYWHERE in the file — including
    inside ``<script type="text/babel">`` blocks that carry raw JSX source, and
    inside ``<style>``/comments. Pixel-clone preview pages embed the component's
    JSX as text in a <script>, so every <a>/<img>/<button> was counted twice
    (once rendered, once in the JSX source) — the clone read ~2x the original.

``html.parser`` hands ``<script>`` / ``<style>`` *content* to ``handle_data`` as
opaque text and never emits start tags for it, so those phantom tags disappear
and the inventory reflects the actual rendered DOM.

This file is the single source of truth. It is mirrored into the pixel-clone
skill (``~/.claude/skills/pixel-clone/scripts/dom_inventory.py``) so the skill and
the codeintel agent share ONE implementation rather than reproducing it.

Importable:  from api.dom_inventory import count_elements
CLI:         python3 dom_inventory.py page.html   ->  JSON on stdout
"""
from __future__ import annotations

from html.parser import HTMLParser

_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_SUBMIT_TYPES = ("submit", "button", "image", "reset")


class _Inventory(HTMLParser):
    def __init__(self) -> None:
        # convert_charrefs=True: entity text won't be mistaken for markup.
        super().__init__(convert_charrefs=True)
        self.counts: dict[str, int] = {}
        self.buttonish_inputs = 0

    def handle_starttag(self, tag, attrs):
        # Content inside <script>/<style> arrives via handle_data, never here,
        # so JSX/CSS text is inherently excluded from the tag counts.
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag == "input":
            t = dict(attrs).get("type", "text")
            if (t or "").strip().lower() in _SUBMIT_TYPES:
                self.buttonish_inputs += 1

    # NOTE: the default handle_startendtag already calls handle_starttag for
    # self-closing tags (<img/>), so void/XHTML-style elements are counted once.


def count_elements(html: str) -> dict:
    """Parse-based element inventory. Returns {} for empty input."""
    if not html:
        return {}
    p = _Inventory()
    try:
        p.feed(html)
        p.close()
    except Exception:
        # Malformed markup: keep whatever was parsed before the error.
        pass
    c = p.counts
    return {
        "buttons": c.get("button", 0) + p.buttonish_inputs,
        "links": c.get("a", 0),
        "inputs": c.get("input", 0) + c.get("select", 0) + c.get("textarea", 0),
        "images": c.get("img", 0),
        "forms": c.get("form", 0),
        "headings": sum(c.get(h, 0) for h in _HEADINGS),
    }


if __name__ == "__main__":
    import json
    import sys

    src = ""
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8", errors="ignore") as fh:
            src = fh.read()
    else:
        src = sys.stdin.read()
    print(json.dumps(count_elements(src), indent=2))
