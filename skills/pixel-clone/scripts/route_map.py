#!/usr/bin/env python3
"""Static route extraction -- the routes a codebase INTENDS to serve.

capture.py can only find screens reachable by following links from "/". This
reads the source instead, so routes behind a form POST, a login wall, or a
JS-built menu still show up in the map. The two lists are complementary: the
agent reports crawl coverage against this.

    route_map.py --src DIR --json FILE
    → {"routes": [{"method", "path", "file", "line", "framework"}, ...],
       "frameworks": [...], "counts": {...}}

Pure regex over source text -- no imports, no execution of the target codebase.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

MAX_FILES = 4000
MAX_BYTES = 600_000
MAX_ROUTES = 500

SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build",
    "target", "vendor", ".next", ".nuxt", "coverage", ".tox", "site-packages",
    ".idea", ".vscode", "migrations",
}

# (regex, method group | None, path group, framework label)
# method=None means the framework doesn't state one at the declaration site.
PATTERNS = {
    ".py": [
        (re.compile(r"""\b(?:path|re_path|url)\(\s*[rbfu]*['"]([^'"]*)['"]"""), None, 1, "Django URLconf"),
        (re.compile(r"""@(?:app|router|bp|blueprint|api)\.(get|post|put|patch|delete|head|options)\(\s*[rbfu]*['"]([^'"]*)['"]""", re.I), 1, 2, "FastAPI/Flask"),
        (re.compile(r"""@(?:app|bp|blueprint)\.route\(\s*[rbfu]*['"]([^'"]*)['"](?:[^)]*methods\s*=\s*\[([^\]]*)\])?""", re.I), None, 1, "Flask"),
        (re.compile(r"""router\.register\(\s*[rbfu]*['"]([^'"]*)['"]"""), None, 1, "DRF router"),
    ],
    ".js": [
        (re.compile(r"""\b(?:app|router|server)\.(get|post|put|patch|delete|all)\(\s*['"`]([^'"`]*)['"`]""", re.I), 1, 2, "Express"),
        (re.compile(r"""<Route\s+[^>]*path\s*=\s*['"{]([^'"}]*)['"}]""", re.I), None, 1, "React Router"),
    ],
    ".ts": None,   # same as .js, filled in below
    ".jsx": None,
    ".tsx": None,
    ".mjs": None,
    ".java": [
        (re.compile(r"""@(Get|Post|Put|Patch|Delete)Mapping\(\s*(?:value\s*=\s*)?['"]([^'"]*)['"]""" ), 1, 2, "Spring"),
        (re.compile(r"""@RequestMapping\(\s*(?:value\s*=\s*)?['"]([^'"]*)['"]"""), None, 1, "Spring"),
    ],
    ".rb": [
        (re.compile(r"""^\s*(get|post|put|patch|delete)\s+['"]([^'"]*)['"]""", re.M | re.I), 1, 2, "Rails"),
    ],
    ".go": [
        (re.compile(r"""\.(?:HandleFunc|Handle)\(\s*['"]([^'"]*)['"]"""), None, 1, "net/http"),
        (re.compile(r"""\b\w+\.(GET|POST|PUT|PATCH|DELETE)\(\s*['"]([^'"]*)['"]"""), 1, 2, "Gin/Echo"),
    ],
    ".php": [
        (re.compile(r"""Route::(get|post|put|patch|delete|any)\(\s*['"]([^'"]*)['"]""", re.I), 1, 2, "Laravel"),
    ],
}
PATTERNS[".ts"] = PATTERNS[".jsx"] = PATTERNS[".tsx"] = PATTERNS[".mjs"] = PATTERNS[".js"]

# Server-rendered templates are screens too, even with no route declaration.
TEMPLATE_DIRS = ("templates", "views", "pages")
TEMPLATE_EXT = (".html", ".jinja", ".jinja2", ".erb", ".ejs", ".hbs", ".blade.php")


def walk(root: Path):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            yield Path(dirpath) / fn
            n += 1
            if n >= MAX_FILES:
                return


def read(p: Path) -> str:
    try:
        if p.stat().st_size > MAX_BYTES:
            return ""
        return p.read_text(errors="ignore")
    except OSError:
        return ""


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def scan_file(path: Path, root: Path, routes: list, frameworks: set) -> None:
    pats = PATTERNS.get(path.suffix.lower())
    if not pats:
        return
    text = read(path)
    if not text:
        return
    rel = str(path.relative_to(root))
    for rx, m_group, p_group, label in pats:
        for m in rx.finditer(text):
            raw = (m.group(p_group) or "").strip()
            if not raw or raw.startswith(("http://", "https://")):
                continue
            method = (m.group(m_group).upper() if m_group else "ANY")
            path_str = raw if raw.startswith("/") else "/" + raw
            routes.append({
                "method": method,
                "path": path_str,
                "file": rel,
                "line": line_of(text, m.start()),
                "framework": label,
            })
            frameworks.add(label)
            if len(routes) >= MAX_ROUTES:
                return


def scan_templates(root: Path, routes: list, frameworks: set) -> None:
    for path in walk(root):
        name = path.name.lower()
        if not name.endswith(TEMPLATE_EXT):
            continue
        parts = {p.lower() for p in path.parts}
        if not parts & set(TEMPLATE_DIRS):
            continue
        rel = str(path.relative_to(root))
        routes.append({
            "method": "VIEW",
            "path": "/" + path.stem,
            "file": rel,
            "line": 1,
            "framework": "template",
        })
        frameworks.add("template")
        if len(routes) >= MAX_ROUTES:
            return


def main() -> int:
    ap = argparse.ArgumentParser(description="Map the routes declared in a codebase.")
    ap.add_argument("--src", required=True)
    ap.add_argument("--json", dest="json_out", required=True)
    args = ap.parse_args()

    root = Path(args.src).expanduser().resolve()
    if not root.is_dir():
        print(f"source dir not found: {root}", file=sys.stderr)
        return 1

    routes: list = []
    frameworks: set = set()
    for path in walk(root):
        scan_file(path, root, routes, frameworks)
        if len(routes) >= MAX_ROUTES:
            break
    if len(routes) < MAX_ROUTES:
        scan_templates(root, routes, frameworks)

    # Same path can be declared per-method (GET + POST handlers); keep both, but
    # drop exact dupes from overlapping patterns matching one declaration twice.
    seen, deduped = set(), []
    for r in routes:
        key = (r["method"], r["path"], r["file"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    counts: dict = {}
    for r in deduped:
        counts[r["framework"]] = counts.get(r["framework"], 0) + 1

    out = {
        "src": str(root),
        "routes": deduped,
        "frameworks": sorted(frameworks),
        "counts": counts,
    }
    dest = Path(args.json_out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"route map: {len(deduped)} route(s) across "
          f"{len(frameworks)} framework(s) → {dest}")
    for fw, n in sorted(counts.items(), key=lambda kv: -kv[1])[:6]:
        print(f"  {fw}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
