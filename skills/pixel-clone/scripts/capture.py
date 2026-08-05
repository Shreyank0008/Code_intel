#!/usr/bin/env python3
"""Playwright capture driver for the pixel-clone pipeline.

Screenshots a RUNNING localhost app and saves, per route, a DOM snapshot next to
one PNG per viewport. Downstream consumers depend on that exact pairing:

    <out>/<slug>.html              rendered DOM (page.content(), post-JS)
    <out>/<slug>__<viewport>.png   screenshot, one per --viewports entry
    <out>/<slug>__styles.json      computed-style sample (for extract_tokens.py)
    <out>/manifest.json            {"routes": [...], "viewports": [...]}

api/pixel_agent.py pairs a snapshot with its shot via ``h.stem + "__*.png"``,
so the PNG must be a sibling of the HTML and carry its stem -- don't nest them.

Usage
  capture.py --base URL --out DIR [--viewports 1280x800,375x812] [--full-page]
             [--crawl --depth N] [--routes /a,/b] [--mask "sel,sel"]
             [--settle MS] [--state-js JS]

Localhost only: the agent guardrails this too, but a capture tool that will
point at arbitrary external sites is a footgun, so it's enforced here as well.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
DEFAULT_TIMEOUT = 20_000


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr, flush=True)
    sys.exit(code)


# ----------------------------------------------------------------- args ----

def parse_viewports(spec: str) -> list[tuple[str, int, int]]:
    out = []
    for chunk in (spec or "1280x800").split(","):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        m = re.fullmatch(r"(\d+)\s*x\s*(\d+)", chunk)
        if not m:
            log(f"! ignoring unparseable viewport '{chunk}'")
            continue
        out.append((f"{m.group(1)}x{m.group(2)}", int(m.group(1)), int(m.group(2))))
    return out or [("1280x800", 1280, 800)]


def slugify(route: str) -> str:
    """/ -> index, /products/new -> products_new. Feeds component_name(), which
    Title-cases and strips the Html suffix, so keep it alphanumeric+underscore."""
    r = route.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not r:
        return "index"
    s = re.sub(r"[^A-Za-z0-9]+", "_", r).strip("_").lower()
    s = re.sub(r"_(html?|php|aspx?|jsp)$", "", s)
    return (s or "index")[:60]


def is_local(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in LOCAL_HOSTS


# ---------------------------------------------------------------- crawl ----

# Same-origin only, and skip anything that isn't a page we could screenshot.
_SKIP_EXT = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|ico|css|js|mjs|json|xml|txt|pdf|zip|woff2?|ttf|eot|mp4|webm)$", re.I)


def normalise(base: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
        return None
    absolute = urljoin(base, href)
    pu, bu = urlparse(absolute), urlparse(base)
    if (pu.hostname or "").lower() != (bu.hostname or "").lower() or pu.port != bu.port:
        return None
    if _SKIP_EXT.search(pu.path or ""):
        return None
    return pu.path or "/"


def discover_routes(page, base: str, depth: int, cap: int) -> list[str]:
    """Breadth-first same-origin link crawl. Cheap on purpose -- it only needs
    to find the screens, the real capture pass happens afterwards."""
    seen, order, frontier = {"/"}, ["/"], ["/"]
    for level in range(max(0, depth)):
        nxt = []
        for route in frontier:
            if len(order) >= cap:
                break
            try:
                page.goto(urljoin(base, route), wait_until="domcontentloaded",
                          timeout=DEFAULT_TIMEOUT)
                hrefs = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => e.getAttribute('href'))")
            except Exception as e:
                log(f"! crawl skipped {route} ({type(e).__name__})")
                continue
            for h in hrefs or []:
                r = normalise(urljoin(base, route), h)
                if r and r not in seen:
                    seen.add(r)
                    order.append(r)
                    nxt.append(r)
                    if len(order) >= cap:
                        break
        frontier = nxt
        if not frontier:
            break
        log(f"· crawl depth {level + 1}: {len(order)} route(s) known")
    return order[:cap]


# -------------------------------------------------------------- capture ----

# Sampled from the live DOM so extract_tokens.py reads what actually RENDERED,
# not what the stylesheet merely declares (cascade/overrides already applied).
STYLE_PROBE = """() => {
  const out = {colors: {}, backgrounds: {}, fontFamilies: {}, fontSizes: {}, radii: {}};
  const bump = (bag, v) => { if (v) bag[v] = (bag[v] || 0) + 1; };
  const els = Array.from(document.querySelectorAll('*')).slice(0, 4000);
  for (const el of els) {
    const cs = getComputedStyle(el);
    bump(out.colors, cs.color);
    const bg = cs.backgroundColor;
    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') bump(out.backgrounds, bg);
    bump(out.fontFamilies, (cs.fontFamily || '').split(',')[0].replace(/["']/g, '').trim());
    bump(out.fontSizes, cs.fontSize);
    const r = cs.borderRadius;
    if (r && r !== '0px') bump(out.radii, r.split(' ')[0]);
  }
  const nav = document.querySelector('nav, header, [role="banner"]');
  if (nav) {
    const cs = getComputedStyle(nav);
    if (cs.backgroundColor && cs.backgroundColor !== 'rgba(0, 0, 0, 0)')
      out.navBackground = cs.backgroundColor;
  }
  return out;
}"""


def apply_mask(page, selectors: str) -> None:
    """Hide dynamic regions so two runs of the same page diff to zero."""
    if not selectors:
        return
    try:
        page.evaluate(
            """(sel) => {
                 for (const s of sel.split(',').map(x => x.trim()).filter(Boolean)) {
                   let els = [];
                   try { els = document.querySelectorAll(s); } catch (e) { continue; }
                   els.forEach(e => { e.style.visibility = 'hidden'; });
                 }
               }""", selectors)
    except Exception as e:
        log(f"! mask failed ({type(e).__name__})")


def capture_route(page, base: str, route: str, out: Path, viewports, args) -> dict | None:
    url = urljoin(base, route)
    slug = slugify(route)
    try:
        page.goto(url, wait_until="networkidle", timeout=DEFAULT_TIMEOUT)
    except Exception:
        # networkidle never settles on pages holding a socket open -- the DOM is
        # usually there anyway, so fall back rather than losing the route.
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            log(f"! {route} unreachable ({type(e).__name__})")
            return None

    if args.state_js:
        try:
            page.evaluate(args.state_js)
        except Exception as e:
            log(f"! state-js failed on {route} ({type(e).__name__})")
    if args.settle:
        page.wait_for_timeout(args.settle)
    apply_mask(page, args.mask)

    shots = []
    for name, w, h in viewports:
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(120)  # let media queries/reflow land
        png = out / f"{slug}__{name}.png"
        try:
            page.screenshot(path=str(png), full_page=args.full_page)
            shots.append(png.name)
        except Exception as e:
            log(f"! screenshot failed {route} @{name} ({type(e).__name__})")

    # DOM + styles are viewport-independent enough to sample once, at the last
    # (widest-applied) viewport state.
    try:
        (out / f"{slug}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        log(f"! DOM snapshot failed {route} ({type(e).__name__})")
        return None
    try:
        styles = page.evaluate(STYLE_PROBE)
        (out / f"{slug}__styles.json").write_text(json.dumps(styles), encoding="utf-8")
    except Exception as e:
        log(f"! style probe failed {route} ({type(e).__name__})")

    log(f"✓ {route} → {slug}.html + {len(shots)} shot(s)")
    return {"route": route, "slug": slug, "shots": shots}


# ----------------------------------------------------------------- main ----

def main() -> int:
    ap = argparse.ArgumentParser(description="Capture a running localhost app.")
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--viewports", default="1280x800")
    ap.add_argument("--full-page", action="store_true")
    ap.add_argument("--crawl", action="store_true")
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--routes", default="", help="comma-separated paths")
    ap.add_argument("--mask", default="")
    ap.add_argument("--settle", type=int, default=0, help="ms to wait before shooting")
    ap.add_argument("--state-js", dest="state_js", default="")
    ap.add_argument("--max-pages", type=int, default=12)
    args = ap.parse_args()

    base = args.base if args.base.endswith("/") else args.base + "/"
    if not is_local(base):
        die(f"refusing non-localhost target: {args.base}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        die("playwright is not installed in this interpreter. Install it with:\n"
            f"  {sys.executable} -m pip install playwright\n"
            f"  {sys.executable} -m playwright install chromium")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    viewports = parse_viewports(args.viewports)
    log(f"capture: {base} → {out} @ {', '.join(v[0] for v in viewports)}")

    captured, routes = [], []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:
            die(f"could not launch chromium ({e}). Run: "
                f"{sys.executable} -m playwright install chromium")
        page = browser.new_page(viewport={"width": viewports[0][1], "height": viewports[0][2]})
        try:
            if args.routes:
                routes = [r if r.startswith("/") else "/" + r
                          for r in (x.strip() for x in args.routes.split(",")) if r]
            elif args.crawl:
                routes = discover_routes(page, base, args.depth, args.max_pages)
            else:
                routes = ["/"]
            log(f"capturing {len(routes)} route(s)")
            for route in routes[: args.max_pages]:
                rec = capture_route(page, base, route, out, viewports, args)
                if rec:
                    captured.append(rec)
        finally:
            browser.close()

    (out / "manifest.json").write_text(json.dumps({
        "base": args.base,
        "routes": [c["route"] for c in captured],
        "viewports": [v[0] for v in viewports],
        "pages": captured,
    }, indent=2), encoding="utf-8")

    if not captured:
        die("captured nothing -- no route rendered successfully")
    log(f"done: {len(captured)} page(s), "
        f"{sum(len(c['shots']) for c in captured)} screenshot(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
