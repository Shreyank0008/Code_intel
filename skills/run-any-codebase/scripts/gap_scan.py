#!/usr/bin/env python3
"""Post-boot reachability scan -- "did the app actually come up, or just bind?"

Crawls a freshly revived app breadth-first from "/" and records the HTTP status
of every same-origin page it can reach. A container that boots but 500s on every
route is a failed revive; this is what tells the agent that.

    gap_scan.py --base URL --max-pages N --out FILE
    → {"base", "pages": [{"url", "path", "status", "bytes", "error"}],
       "ok", "broken", "total"}

api/revive_agent.py::_gap_scan counts pages with status < 400 as OK and reports
the rest as broken. Unreachable pages are recorded as status 0 so they land in
the broken bucket rather than silently vanishing.

stdlib only -- no Playwright. This checks that the server responds, not that the
page renders, so there's no reason to pay for a browser here.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

TIMEOUT = 8
MAX_BODY = 300_000
UA = "codeintel-gap-scan/1.0"

HREF_RE = re.compile(r"""<a\b[^>]*\bhref\s*=\s*(["'])(.*?)\1""", re.I | re.S)
SKIP_EXT = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|ico|css|js|mjs|json|xml|txt|pdf|zip|woff2?|ttf|eot|mp4|webm)$", re.I)
# <a href> inside a <script> is JS source, not a link. SPAs build their markup in
# template literals, so scanning raw HTML harvests things like `/${row.url}` and
# then "successfully" fetches them against a catch-all route -- inflating the OK
# count with URLs that don't exist. Strip script/style before extracting.
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
# Un-substituted server/client template placeholders: ${x}, {{ x }}, <%= x %>, {x}
PLACEHOLDER_RE = re.compile(r"\$\{|\{\{|<%|\{[a-zA-Z_]")


def fetch(url: str):
    """→ (status, body_text, nbytes, error). Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(MAX_BODY)
            ctype = (r.headers.get("Content-Type") or "").lower()
            body = raw.decode("utf-8", "ignore") if "html" in ctype else ""
            return r.status, body, len(raw), None
    except urllib.error.HTTPError as e:
        # A 404/500 is a RESULT, not a failure of the scan -- keep the status.
        try:
            raw = e.read(MAX_BODY)
        except Exception:
            raw = b""
        return e.code, "", len(raw), None
    except Exception as e:
        return 0, "", 0, f"{type(e).__name__}: {e}"


def same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return ((pa.hostname or "").lower() == (pb.hostname or "").lower()
            and (pa.port or 80) == (pb.port or 80))


def links_from(base_url: str, html: str) -> list[str]:
    out = []
    markup = SCRIPT_STYLE_RE.sub(" ", html or "")
    for _, href in HREF_RE.findall(markup):
        href = href.strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
            continue
        if PLACEHOLDER_RE.search(href):
            continue  # unrendered template expression, not a real path
        absolute = urljoin(base_url, href)
        if not same_origin(absolute, base_url):
            continue
        if SKIP_EXT.search(urlparse(absolute).path or ""):
            continue
        out.append(absolute.split("#", 1)[0])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Crawl a running app and report reachability.")
    ap.add_argument("--base", required=True)
    ap.add_argument("--max-pages", type=int, default=40)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    base = args.base if args.base.endswith("/") else args.base + "/"
    seen = {base}
    queue = deque([base])
    pages = []

    while queue and len(pages) < args.max_pages:
        url = queue.popleft()
        status, body, nbytes, err = fetch(url)
        pages.append({
            "url": url,
            "path": urlparse(url).path or "/",
            "status": status,
            "bytes": nbytes,
            "error": err,
        })
        flag = "ok " if 0 < status < 400 else "BAD"
        print(f"  [{flag}] {status:>3} {urlparse(url).path or '/'}"
              + (f"  ({err})" if err else ""), flush=True)
        if body:
            for link in links_from(url, body):
                if link not in seen and len(seen) < args.max_pages * 3:
                    seen.add(link)
                    queue.append(link)

    ok = sum(1 for p in pages if 0 < p["status"] < 400)
    broken = [p for p in pages if not (0 < p["status"] < 400)]
    result = {
        "base": args.base,
        "pages": pages,
        "ok": ok,
        "broken": len(broken),
        "total": len(pages),
    }

    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"gap-scan: {ok}/{len(pages)} pages OK, {len(broken)} broken → {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
