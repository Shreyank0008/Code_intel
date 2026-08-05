#!/usr/bin/env python3
"""Per-file artifact catalog -- the behavioural inventory a rewrite must reproduce.

Route lists tell you what a legacy app *exposes*; they say nothing about the
forms, validation rules, session usage and SQL buried in each handler. That's
the part a rewrite silently drops. This walks the source and catalogs it.

    extract_artifacts.py <root> --out FILE [--max-per N]
    → {"root", "files_scanned", "files_with_artifacts",
       "totals":  {<section>: n, ...},
       "pages":   {"<relpath>": {<section>: [...], ...}, ...}}

Section keys are fixed by the UI (frontend/index.html::CAT_SECTIONS) -- adding
one here without adding it there means it's collected but never displayed:

    forms buttons api_calls links request_params sql functions_defined
    functions_called session_read session_write validation_messages
    conditions includes

``forms`` entries are objects ({method, action, fields}); every other section is
a list of strings. api/analyzer.py ranks files by total artifact count and keeps
the busiest 120, so per-file completeness matters more than breadth.

Regex over source text -- nothing is imported or executed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

MAX_FILES = 6000
MAX_BYTES = 800_000

SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build",
    "target", "vendor", ".next", ".nuxt", "coverage", ".tox", "site-packages",
    ".idea", ".vscode", ".pytest_cache", "bower_components",
}
CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".java", ".rb", ".php", ".go",
    ".cs", ".html", ".jinja", ".jinja2", ".erb", ".ejs", ".hbs", ".vue", ".jsp",
}

SECTIONS = [
    "forms", "buttons", "api_calls", "links", "request_params", "sql",
    "functions_defined", "functions_called", "session_read", "session_write",
    "validation_messages", "conditions", "includes",
]

# --------------------------------------------------------------- regexes ----

FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
FORM_ATTR_RE = re.compile(r"""(\w[\w-]*)\s*=\s*(["'])(.*?)\2""", re.S)
FIELD_RE = re.compile(r"""<(input|select|textarea)\b([^>]*)>""", re.I)
BUTTON_RE = re.compile(
    r"""<button\b[^>]*>(.*?)</button>|<input\b[^>]*type\s*=\s*["'](?:submit|button)["'][^>]*""",
    re.I | re.S)
LINK_RE = re.compile(r"""<a\b[^>]*href\s*=\s*(["'])(.*?)\1""", re.I | re.S)
INCLUDE_RE = re.compile(
    r"""{%\s*(?:include|extends)\s+["']([^"']+)["']|"""
    r"""@(?:include|extends)\(\s*['"]([^'"]+)['"]|"""
    r"""<%-?\s*include\s*\(?\s*['"]([^'"]+)['"]|"""
    r"""\b(?:require|include)(?:_once)?\s*\(?\s*['"]([^'"]+)['"]""", re.I)

API_CALL_RE = re.compile(
    r"""\b(?:fetch|axios(?:\.\w+)?|\$\.(?:get|post|ajax)|requests\.(?:get|post|put|patch|delete)|"""
    r"""urlopen|HttpClient\.\w+)\s*\(\s*[`'"]([^`'"]+)[`'"]""", re.I)

SQL_RE = re.compile(
    r"""\b(SELECT\s+[\w*,.\s()]+?\s+FROM\s+[\w."`\[\]]+|"""
    r"""INSERT\s+INTO\s+[\w."`\[\]]+|"""
    r"""UPDATE\s+[\w."`\[\]]+\s+SET|"""
    r"""DELETE\s+FROM\s+[\w."`\[\]]+|"""
    r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\w."`\[\]]+)""", re.I | re.S)

PARAM_RE = re.compile(
    r"""\b(?:request\.(?:GET|POST|args|form|json|query_params|params|data)|req\.(?:query|body|params))"""
    r"""(?:\.get\(\s*["']([\w-]+)["']|\[\s*["']([\w-]+)["']\]|\.([\w-]+))""")

SESSION_READ_RE = re.compile(
    r"""\b(?:session|\$_SESSION|req\.session|request\.session)"""
    r"""(?:\.get\(\s*["']([\w-]+)["']|\[\s*["']([\w-]+)["']\]|\.([\w-]+))""")
SESSION_WRITE_RE = re.compile(
    r"""\b(?:session|\$_SESSION|req\.session|request\.session)"""
    r"""(?:\[\s*["']([\w-]+)["']\]|\.([\w-]+))\s*=(?!=)""")

FUNC_DEF_RE = {
    "py": re.compile(r"""^\s*(?:async\s+)?def\s+(\w+)\s*\(""", re.M),
    "js": re.compile(r"""^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(|"""
                     r"""^\s*(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(""", re.M),
    "jvm": re.compile(r"""^\s*(?:public|private|protected)\s+[\w<>\[\],\s]+\s+(\w+)\s*\(""", re.M),
    "rb": re.compile(r"""^\s*def\s+(\w+)""", re.M),
    "php": re.compile(r"""^\s*(?:public|private|protected|static|\s)*function\s+(\w+)\s*\(""", re.M),
    "go": re.compile(r"""^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(""", re.M),
}
FUNC_CALL_RE = re.compile(r"""\b([a-z_][\w]{2,})\s*\(""")
# Language noise that would otherwise dominate every "functions called" list.
CALL_NOISE = {
    "if", "for", "while", "switch", "catch", "return", "print", "console", "log",
    "str", "int", "len", "list", "dict", "set", "type", "range", "super", "self",
    "function", "require", "import", "new", "typeof", "parseint", "string",
}

VALIDATION_RE = re.compile(
    r"""(?:raise\s+\w*(?:ValidationError|ValueError)\(|"""
    r"""throw\s+new\s+\w*Error\(|"""
    r"""(?:flash|addError|setError|abort)\s*\()\s*[`'"]([^`'"]{4,140})[`'"]""", re.I)
CONDITION_RE = re.compile(
    r"""^\s*(?:el(?:se\s+)?if|if)\s*\(?\s*(.{4,120}?)\s*\)?\s*[:{]\s*$""", re.M)

# ----------------------------------------------------------------- utils ----


def func_def_re(ext: str):
    if ext == ".py":
        return FUNC_DEF_RE["py"]
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".vue"):
        return FUNC_DEF_RE["js"]
    if ext in (".java", ".cs"):
        return FUNC_DEF_RE["jvm"]
    if ext == ".rb":
        return FUNC_DEF_RE["rb"]
    if ext == ".php":
        return FUNC_DEF_RE["php"]
    if ext == ".go":
        return FUNC_DEF_RE["go"]
    return None


def first_group(m) -> str:
    for g in m.groups():
        if g:
            return g
    return ""


def dedupe(seq, cap: int) -> list:
    seen, out = set(), []
    for v in seq:
        k = json.dumps(v, sort_keys=True) if isinstance(v, dict) else v
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
        if len(out) >= cap:
            break
    return out


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def walk(root: Path):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if Path(fn).suffix.lower() in CODE_EXT:
                yield Path(dirpath) / fn
                n += 1
                if n >= MAX_FILES:
                    return


# ----------------------------------------------------------------- scan ----

def scan_forms(text: str) -> list:
    forms = []
    for m in FORM_RE.finditer(text):
        attrs = {k.lower(): v for k, _, v in FORM_ATTR_RE.findall(m.group(1))}
        fields = []
        for fm in FIELD_RE.finditer(m.group(2)):
            fattrs = {k.lower(): v for k, _, v in FORM_ATTR_RE.findall(fm.group(2))}
            name = fattrs.get("name") or fattrs.get("id")
            if name:
                fields.append(name)
        forms.append({
            "method": (attrs.get("method") or "get").upper(),
            "action": attrs.get("action") or "",
            "fields": fields,
        })
    return forms


def scan(path: Path, root: Path, cap: int) -> dict:
    try:
        if path.stat().st_size > MAX_BYTES:
            return {}
        text = path.read_text(errors="ignore")
    except OSError:
        return {}
    if not text.strip():
        return {}

    ext = path.suffix.lower()
    rec: dict = {}

    def put(key, values):
        vals = dedupe(values, cap)
        if vals:
            rec[key] = vals

    put("forms", scan_forms(text))
    put("buttons", [squash(re.sub(r"<[^>]+>", "", m.group(1) or ""))[:80]
                    for m in BUTTON_RE.finditer(text) if squash(m.group(1) or "")])
    put("links", [h for _, h in LINK_RE.findall(text)
                  if h and not h.startswith(("#", "javascript:"))])
    put("api_calls", [m.group(1) for m in API_CALL_RE.finditer(text)])
    put("sql", [squash(m.group(1))[:160] for m in SQL_RE.finditer(text)])
    put("request_params", [first_group(m) for m in PARAM_RE.finditer(text) if first_group(m)])
    put("session_read", [first_group(m) for m in SESSION_READ_RE.finditer(text) if first_group(m)])
    put("session_write", [first_group(m) for m in SESSION_WRITE_RE.finditer(text) if first_group(m)])
    put("validation_messages", [squash(m.group(1))[:140] for m in VALIDATION_RE.finditer(text)])
    put("conditions", [squash(m.group(1))[:120] for m in CONDITION_RE.finditer(text)])
    put("includes", [first_group(m) for m in INCLUDE_RE.finditer(text) if first_group(m)])

    fdre = func_def_re(ext)
    if fdre:
        defined = [first_group(m) for m in fdre.finditer(text) if first_group(m)]
        put("functions_defined", defined)
        local = set(defined)
        calls = [c for c in FUNC_CALL_RE.findall(text)
                 if c.lower() not in CALL_NOISE and c not in local]
        put("functions_called", calls)

    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="Catalog per-file behavioural artifacts.")
    ap.add_argument("root")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-per", type=int, default=80,
                    help="cap on items kept per section per file")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"root not found: {root}", file=sys.stderr)
        return 1

    pages: dict = {}
    totals = {s: 0 for s in SECTIONS}
    scanned = 0

    for path in walk(root):
        scanned += 1
        rec = scan(path, root, args.max_per)
        if not rec:
            continue
        rel = str(path.relative_to(root))
        pages[rel] = rec
        for k, v in rec.items():
            totals[k] = totals.get(k, 0) + len(v)

    result = {
        "root": str(root),
        "files_scanned": scanned,
        "files_with_artifacts": len(pages),
        "totals": {k: v for k, v in totals.items() if v},
        "pages": pages,
    }

    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"catalog: {len(pages)}/{scanned} files carry artifacts → {dest}")
    for k, v in sorted(result["totals"].items(), key=lambda kv: -kv[1])[:8]:
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
