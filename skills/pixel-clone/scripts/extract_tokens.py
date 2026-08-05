#!/usr/bin/env python3
"""Design-token extraction from a capture directory.

Reads what capture.py left behind and distils it into the palette/type/radius
vocabulary the scaffolding prompt needs:

    <out>/tokens.json  {"colors": [...], "fontFamilies": [...],
                        "fontSizes": [...], "radii": [...],
                        "navBackground": "..."}

Two sources, in order of trust:
  1. ``*__styles.json`` -- computed styles sampled from the live DOM by
     capture.py. Authoritative: the cascade has already been resolved.
  2. ``*.html`` -- inline <style> blocks and style="" attributes, parsed as a
     fallback for captures taken before the probe existed (or where it failed).

Tokens are ranked by how often they occur, so the returned head of each list is
the page's actual visual vocabulary rather than a long tail of one-off values.

NOTE --out is a DIRECTORY, not a filename. api/pixel_agent.py reads
``<out>/tokens.json``; pointing this at a bare filename is what made the whole
token stage silently return {} in an earlier revision.

Usage: extract_tokens.py --captures DIR --out DIR
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Chrome reports every colour as rgb()/rgba(); source CSS uses hex just as often.
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3,4}){1,2}\b")
RGB_RE = re.compile(r"rgba?\([^)]*\)")
FONT_DECL_RE = re.compile(r"font-family\s*:\s*([^;}\"']+)", re.I)
SIZE_DECL_RE = re.compile(r"font-size\s*:\s*([\d.]+(?:px|rem|em|pt))", re.I)
RADIUS_DECL_RE = re.compile(r"border-radius\s*:\s*([\d.]+(?:px|rem|em|%))", re.I)
STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
STYLE_ATTR_RE = re.compile(r"""style\s*=\s*(["'])(.*?)\1""", re.S | re.I)

# Neutral defaults every page inherits from the UA stylesheet -- they say nothing
# about the design, and left in they crowd out the real brand colours.
UA_NOISE = {"rgb(0, 0, 0)", "rgba(0, 0, 0, 0)", "transparent", "#000", "#000000"}


def norm_color(v: str) -> str:
    v = v.strip().lower()
    return re.sub(r"\s+", " ", v)


def norm_font(v: str) -> str:
    return v.split(",")[0].strip().strip("\"'")


def from_style_probes(cap: Path, colors, backgrounds, fonts, sizes, radii) -> tuple:
    """Merge capture.py's computed-style samples. Each bag is {value: count}.
    Returns (navBackground | None, number of probe files read)."""
    nav_bg = None
    probes = sorted(cap.rglob("*__styles.json"))
    for p in probes:
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        for v, n in (data.get("colors") or {}).items():
            colors[norm_color(v)] += n
        for v, n in (data.get("backgrounds") or {}).items():
            backgrounds[norm_color(v)] += n
        for v, n in (data.get("fontFamilies") or {}).items():
            if v.strip():
                fonts[norm_font(v)] += n
        for v, n in (data.get("fontSizes") or {}).items():
            sizes[v.strip()] += n
        for v, n in (data.get("radii") or {}).items():
            radii[v.strip()] += n
        if not nav_bg and data.get("navBackground"):
            nav_bg = norm_color(data["navBackground"])
    return nav_bg, len(probes)


def from_html(cap: Path, colors, fonts, sizes, radii) -> int:
    """Fallback: scrape declarations out of the DOM snapshots themselves."""
    files = sorted(cap.rglob("*.html"))
    for f in files:
        try:
            html = f.read_text(errors="ignore")
        except OSError:
            continue
        css = "\n".join(STYLE_BLOCK_RE.findall(html))
        css += "\n" + "\n".join(m.group(2) for m in STYLE_ATTR_RE.finditer(html))
        for m in HEX_RE.findall(css):
            colors[norm_color(m)] += 1
        for m in RGB_RE.findall(css):
            colors[norm_color(m)] += 1
        for m in FONT_DECL_RE.findall(css):
            v = norm_font(m)
            if v:
                fonts[v] += 1
        for m in SIZE_DECL_RE.findall(css):
            sizes[m.strip()] += 1
        for m in RADIUS_DECL_RE.findall(css):
            radii[m.strip()] += 1
    return len(files)


def size_key(v: str) -> float:
    m = re.match(r"([\d.]+)", v)
    n = float(m.group(1)) if m else 0.0
    return n * 16 if v.endswith(("rem", "em")) else n


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract design tokens from captures.")
    ap.add_argument("--captures", required=True)
    ap.add_argument("--out", required=True, help="OUTPUT DIRECTORY (tokens.json is written inside)")
    args = ap.parse_args()

    cap = Path(args.captures)
    if not cap.exists():
        print(f"captures dir not found: {cap}", file=sys.stderr)
        return 1

    colors, backgrounds = Counter(), Counter()
    fonts, sizes, radii = Counter(), Counter(), Counter()

    nav_bg, n_probes = from_style_probes(cap, colors, backgrounds, fonts, sizes, radii)
    n_html = 0
    if not (colors or fonts or sizes):
        n_html = from_html(cap, colors, fonts, sizes, radii)

    for noise in UA_NOISE:
        colors.pop(noise, None)
        backgrounds.pop(noise, None)

    # Surfaces first: a page's background colours carry more of its identity than
    # its (mostly near-black) text colours.
    palette = [c for c, _ in backgrounds.most_common(10)]
    palette += [c for c, _ in colors.most_common(16) if c not in palette]

    tokens = {
        "colors": palette[:16],
        "fontFamilies": [f for f, _ in fonts.most_common(6)],
        "fontSizes": sorted({s for s, _ in sizes.most_common(20)}, key=size_key)[:12],
        "radii": sorted({r for r, _ in radii.most_common(12)}, key=size_key)[:8],
    }
    if nav_bg and nav_bg not in UA_NOISE:
        tokens["navBackground"] = nav_bg

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tokens.json").write_text(json.dumps(tokens, indent=2), encoding="utf-8")

    src = f"{n_probes} style probe(s)" if n_probes else f"{n_html} HTML snapshot(s)"
    print(f"tokens from {src}: {len(tokens['colors'])} colours, "
          f"{len(tokens['fontFamilies'])} fonts, {len(tokens['fontSizes'])} sizes, "
          f"{len(tokens['radii'])} radii → {out / 'tokens.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
