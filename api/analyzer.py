"""
Real static analysis of an ingested codebase.

Walks the tree once, classifies files by language, and runs light regex probes
for the signals that matter to UCP sizing: DB entities, API endpoints, UI pages,
external integrations, auth model, and legacy-migration hints. Framework-aware
for Django/DRF and React, with generic fallbacks so *any* codebase yields
something usable.

Emits progress through a `log(level, msg)` callback so the job console streams
live while the scan runs.
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from .guardrails import enforce_scan_budget

SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".hg", ".svn", "venv", ".venv",
    "env", "dist", "build", "target", "out", ".next", ".nuxt", "vendor",
    "staticfiles", "site-packages", ".mypy_cache", ".pytest_cache", "coverage",
    ".idea", ".vscode", "bin", "obj", "Pods", ".gradle",
}
CODE_EXT = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".java": "Java", ".cs": "C#", ".go": "Go", ".rb": "Ruby",
    ".php": "PHP", ".sql": "SQL", ".kt": "Kotlin", ".rs": "Rust", ".vue": "Vue",
    ".html": "HTML", ".css": "CSS", ".scss": "CSS",
    # server-rendered templates (also a frontend layer)
    ".jsp": "JSP", ".aspx": "ASPX", ".cshtml": "Razor", ".erb": "ERB",
    # mainframe / legacy
    ".cob": "COBOL", ".cbl": "COBOL", ".cpy": "COBOL", ".cobol": "COBOL",
    ".jcl": "JCL", ".pli": "PL/I", ".pl1": "PL/I", ".asm": "Assembly",
    # other common
    ".pl": "Perl", ".pm": "Perl", ".scala": "Scala", ".swift": "Swift",
    ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir", ".lua": "Lua",
    ".vb": "VB.NET", ".pas": "Pascal", ".c": "C", ".cpp": "C++", ".h": "C/C++",
    # schema-driven APIs
    ".graphql": "GraphQL", ".gql": "GraphQL", ".proto": "Protobuf",
}
INTEGRATION_SIGNALS = {
    "MS Graph": r"graph\.microsoft|msgraph",
    "OAuth": r"\boauth\b|oauth2",
    "SMTP/Email": r"\bsmtp\b|send_mail|EmailMessage|nodemailer",
    "LDAP/AD": r"\bldap\b|active.?directory",
    "Azure": r"\bazure\b",
    "AWS": r"\bboto3\b|aws-sdk|\bs3\b",
    "Payment": r"stripe|razorpay|paypal|payment.?gateway",
    "Kafka/MQ": r"\bkafka\b|rabbitmq|amqp|celery",
    "Redis": r"\bredis\b",
    "Webhooks": r"webhook",
}
AUTH_SIGNALS = {
    "JWT": r"\bjwt\b|jsonwebtoken|SimpleJWT",
    "OAuth": r"oauth",
    "LDAP": r"ldap",
    "SAML": r"saml",
    "RBAC": r"rbac|has_perm|permission_classes|role",
    "MFA/2FA": r"\bmfa\b|totp|two.?factor",
}


def scan(root: Path, log) -> dict:
    log("info", f"walking file tree at {root}")
    files = []            # (path, ext, lang)
    total_bytes = 0
    skipped = 0
    per_lang_loc = {}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            fp = os.path.join(dirpath, fn)
            if ext not in CODE_EXT:
                skipped += 1
                continue
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            total_bytes += sz
            files.append((fp, ext, CODE_EXT[ext]))

    enforce_scan_budget(len(files), total_bytes)
    log("warn", f"skipped {skipped} non-code / vendored files")
    log("m", f"{len(files)} source files kept ({total_bytes // 1024} KB)")

    # LOC per language (bounded read)
    for fp, ext, lang in files:
        try:
            with open(fp, "r", errors="ignore") as fh:
                n = sum(1 for _ in fh)
        except OSError:
            n = 0
        per_lang_loc[lang] = per_lang_loc.get(lang, 0) + n

    langs = sorted(per_lang_loc, key=per_lang_loc.get, reverse=True)
    total_loc = sum(per_lang_loc.values())
    log("m", "languages: " + ", ".join(f"{l} ({per_lang_loc[l]} LOC)" for l in langs[:4]))

    py = [f for f in files if f[1] == ".py"]
    jsx = [f for f in files if f[1] in (".js", ".jsx", ".ts", ".tsx", ".vue")]

    frameworks = _detect_frameworks(root, files, log)

    # ---- entity / endpoint / page probes ----
    models = _count_regex(py, r"class\s+\w+\s*\([^)]*\bModel\b[^)]*\)\s*:")
    # framework-aware endpoint detection across ALL languages (Django/Flask/FastAPI,
    # Express/Nest, Spring/JAX-RS, ASP.NET, Rails, Laravel, GraphQL, gRPC)
    _endpoints = _collect_endpoints(root, files)
    routes = len(_endpoints)
    drf_views = _count_regex(py, r"\b\w+\s*\(.*(?:ViewSet|APIView)\)") \
        + _count_regex(py, r"@api_view")

    pages = _count_pages(root, jsx)
    components = _count_dir_files(root, ("components",), jsx)

    integrations = _match_signals(files, INTEGRATION_SIGNALS)
    auth = _match_signals(files, AUTH_SIGNALS)
    legacy = _legacy_signals(root, files)

    log("m", f"entities={models}  endpoints={routes}  api_views={drf_views}  pages={pages}")
    if integrations:
        log("m", "integrations: " + ", ".join(integrations))
    if auth:
        log("m", "auth: " + ", ".join(auth))
    if legacy:
        log("warn", "legacy-migration signals: " + ", ".join(legacy))

    log("info", "assembling run-any-codebase artifacts…")
    artifacts = _artifacts(root, files, py, jsx, per_lang_loc, frameworks,
                           integrations, auth, legacy, log)
    log("ok", "artifacts captured ✓")

    return {
        "artifacts": artifacts,
        "root": str(root),
        "languages": langs,
        "per_lang_loc": per_lang_loc,
        "total_loc": total_loc,
        "file_count": len(files),
        "frameworks": frameworks,
        "models": models,
        "endpoints": routes,
        "api_views": drf_views,
        "pages": pages,
        "components": components,
        "integrations": integrations,
        "auth": auth,
        "legacy": legacy,
    }


# ---------------------------------------------------------------- helpers ----

def _read(fp, limit=400_000):
    try:
        with open(fp, "r", errors="ignore") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _count_regex(fileset, pattern):
    rx = re.compile(pattern, re.MULTILINE)
    return sum(len(rx.findall(_read(fp))) for fp, *_ in fileset)


def _match_signals(files, table):
    found = []
    blobs = None
    for name, pat in table.items():
        rx = re.compile(pat, re.IGNORECASE)
        hit = False
        for fp, *_ in files:
            if rx.search(_read(fp, 120_000)):
                hit = True
                break
        if hit:
            found.append(name)
    return found


def _detect_frameworks(root, files, log):
    fw = []
    names = {os.path.basename(f[0]).lower() for f in files}
    all_txt = ""
    # cheap manifest sniffing
    for manifest in ("requirements.txt", "pyproject.toml", "package.json", "pom.xml",
                     "go.mod", "Gemfile", "composer.json", "build.gradle", "mix.exs"):
        p = _find_first(root, manifest)
        if p:
            all_txt += _read(p, 40_000).lower()
    def has(*keys): return any(k in all_txt for k in keys)
    if has("django"): fw.append("Django")
    if has("djangorestframework", "rest_framework"): fw.append("DRF")
    if has("fastapi"): fw.append("FastAPI")
    if has("flask"): fw.append("Flask")
    if has("\"react\"", "react-dom"): fw.append("React")
    if has("vue"): fw.append("Vue")
    if has("@angular"): fw.append("Angular")
    if has("express"): fw.append("Express")
    if has("spring-boot", "springframework"): fw.append("Spring Boot")
    if has("gin-gonic"): fw.append("Gin")
    if has("laravel/framework"): fw.append("Laravel")
    if has("symfony/"): fw.append("Symfony")
    if has("codeigniter"): fw.append("CodeIgniter")
    if has("rails"): fw.append("Rails")
    if has("phoenix"): fw.append("Phoenix")
    # language-family signals from file extensions present
    exts = {f[1] for f in files}
    if exts & {".cob", ".cbl", ".cpy", ".cobol"}: fw.append("COBOL / Mainframe")
    if ".jcl" in exts: fw.append("JCL")
    if not fw:
        fw.append("(framework not identified — generic scan)")
    log("m", "frameworks: " + ", ".join(fw))
    return fw


def _find_first(root, name):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS and not d.startswith(".")]
        if name in fn:
            return os.path.join(dp, name)
    return None


def _count_pages(root, jsx):
    # count files living in a "pages" or "views" or "screens" directory
    return _count_dir_files(root, ("pages", "views", "screens", "routes"), jsx)


def _count_dir_files(root, dirnames, fileset):
    n = 0
    for fp, *_ in fileset:
        parts = {p.lower() for p in Path(fp).parts}
        if parts & set(dirnames):
            n += 1
    return n


def _legacy_signals(root, files):
    sig = []
    names = {os.path.basename(f[0]).lower() for f in files}
    if any("mysql" in n for n in names) or _find_first(root, "my.cnf"):
        sig.append("MySQL backend")
    if any("migrate_to" in n or "migration" in n for n in names):
        sig.append("migration scripts")
    if _find_first(root, "web.xml") or _find_first(root, "pom.xml"):
        sig.append("J2EE/legacy Java")
    for fp, *_ in files:
        if fp.endswith((".jsp", ".asp", ".aspx", ".php")):
            sig.append("server-rendered legacy pages")
            break
    exts = {os.path.splitext(f[0])[1].lower() for f in files}
    if exts & {".cob", ".cbl", ".cpy", ".cobol"}:
        sig.append("COBOL / mainframe")
    if ".jcl" in exts:
        sig.append("JCL batch jobs")
    if ".pas" in exts or ".vb" in exts:
        sig.append("legacy Pascal/VB")
    return sorted(set(sig))


# ============================================================ ARTIFACTS ====
# Detailed, itemised outputs — the "migration bundle" a resurrect pass would
# hand off. Everything here is capped so a huge repo can't blow up the payload.

CAP = 250


def _rel(root, fp):
    try:
        return os.path.relpath(fp, root)
    except ValueError:
        return fp


def _artifacts(root, files, py, jsx, per_lang_loc, frameworks,
               integrations, auth, legacy, log):
    endpoints = _collect_endpoints(root, files)
    models = _collect_models(root, py)
    ui = _collect_ui(root, files)
    ui_total = len(ui["pages"]) + len(ui["templates"])
    deps = _parse_deps(root)
    database = _db_artifacts(root, files)
    functions = _collect_functions(root, files)
    imports = _collect_imports(files)
    classes = _collect_classes(root, files)
    tests = _collect_tests(root, files)
    ext_pkgs = len(deps["python"]) + len(deps["node"])

    # Deep per-file catalog from the run-any-codebase skill's own extractor.
    catalog = _skill_catalog(root, log)

    stats = [
        {"label": "API endpoints", "value": len(endpoints), "sub": "routes · forms · ajax"},
        {"label": "Functions & methods", "value": len(functions), "sub": "defined in the code"},
        {"label": "Imports", "value": imports["count"], "sub": "files & modules depended on"},
        {"label": "External packages", "value": ext_pkgs, "sub": "third-party libraries"},
        {"label": "Data entities", "value": len(models), "sub": "models / tables"},
        {"label": "UI pages", "value": ui_total,
         "sub": "API-only — no frontend" if ui["api_only"] else "pages + templates"},
        {"label": "DB tables", "value": len(database["tables"]), "sub": "from DDL"},
        {"label": "Classes", "value": len(classes), "sub": "types defined"},
    ]
    if catalog:
        t = catalog.get("totals", {})
        stats += [
            {"label": "Forms", "value": t.get("forms", 0), "sub": "with fields captured"},
            {"label": "SQL queries", "value": t.get("sql", 0), "sub": "embedded in code"},
            {"label": "Request params", "value": t.get("request_params", 0), "sub": "inputs read"},
            {"label": "Validation rules", "value": t.get("conditions", 0), "sub": "conditions / checks"},
        ]

    return {
        "stats": stats,
        "overview": {
            "root": str(root),
            "frameworks": frameworks,
            "languages": [{"lang": l, "loc": per_lang_loc[l]}
                          for l in sorted(per_lang_loc, key=per_lang_loc.get, reverse=True)],
            "file_count": len(files),
            "total_loc": sum(per_lang_loc.values()),
        },
        "structure": _tree(root, files),
        "endpoints": endpoints,
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "models": models,
        "ui": ui,
        "integrations": _integration_locations(root, files),
        "auth": auth,
        "dependencies": deps,
        "database": database,
        "tests": tests,
        "config": _config_artifacts(root, files),
        "catalog": catalog,
        "migration": _migration_notes(frameworks, integrations, auth, legacy),
    }


# ---- rich modern-code extraction (functions, imports, classes, tests) ----

_FN_PY = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.M)
_FN_JS = re.compile(r"\bfunction\s+(\w+)\s*\(|\b(\w+)\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*=>")
_FN_JAVA = re.compile(r"(?:public|private|protected|static|\s)+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*(?:throws[\w,\s]+)?\{")
_FN_PHP = re.compile(r"\bfunction\s+(\w+)\s*\(")
_FN_GO = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(", re.M)
_FN_RUBY = re.compile(r"^\s*def\s+([\w?!.]+)", re.M)
# COBOL "functions" are paragraphs / sections + the program id
_FN_COBOL = re.compile(r"^\s*(?:PROGRAM-ID\.\s*([\w-]+)|([A-Z0-9][A-Z0-9-]+)\s+SECTION\s*\.)", re.M | re.I)
_CLASS_RX = re.compile(r"^\s*(?:public\s+|abstract\s+|final\s+)?class\s+(\w+)", re.M)
_IMPORT_RX = {
    ".py": re.compile(r"^\s*(?:import\s+([\w\.]+)|from\s+([\w\.]+)\s+import)", re.M),
    ".js": re.compile(r"""import\s+.*?\s+from\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]"""),
    ".java": re.compile(r"^\s*import\s+([\w\.]+)", re.M),
}
_IMPORT_RX[".jsx"] = _IMPORT_RX[".ts"] = _IMPORT_RX[".tsx"] = _IMPORT_RX[".js"]


def _collect_functions(root, files):
    out = []
    for fp, ext, lang in files:
        txt = _read(fp)
        rel = _rel(root, fp)
        if ext == ".py":
            for m in _FN_PY.finditer(txt):
                out.append({"name": m.group(1), "kind": "def", "file": rel})
        elif ext in (".js", ".jsx", ".ts", ".tsx"):
            for m in _FN_JS.finditer(txt):
                out.append({"name": m.group(1) or m.group(2), "kind": "fn", "file": rel})
        elif ext == ".java":
            for m in _FN_JAVA.finditer(txt):
                out.append({"name": m.group(1), "kind": "method", "file": rel})
        elif ext == ".php":
            for m in _FN_PHP.finditer(txt):
                out.append({"name": m.group(1), "kind": "function", "file": rel})
        elif ext == ".go":
            for m in _FN_GO.finditer(txt):
                out.append({"name": m.group(1), "kind": "func", "file": rel})
        elif ext == ".rb":
            for m in _FN_RUBY.finditer(txt):
                out.append({"name": m.group(1), "kind": "def", "file": rel})
        elif ext in (".cob", ".cbl", ".cpy", ".cobol"):
            for m in _FN_COBOL.finditer(txt):
                out.append({"name": m.group(1) or m.group(2),
                            "kind": "program" if m.group(1) else "section", "file": rel})
        if len(out) > CAP * 4:
            break
    return out[:CAP * 4]


def _collect_classes(root, files):
    out = []
    for fp, ext, lang in files:
        if ext not in (".py", ".java", ".ts", ".tsx"):
            continue
        for m in _CLASS_RX.finditer(_read(fp)):
            out.append({"name": m.group(1), "file": _rel(root, fp)})
        if len(out) > CAP * 2:
            break
    return out[:CAP * 2]


def _collect_imports(files):
    modules = {}
    count = 0
    for fp, ext, lang in files:
        rx = _IMPORT_RX.get(ext)
        if not rx:
            continue
        for m in rx.finditer(_read(fp)):
            mod = next((g for g in m.groups() if g), None)
            if mod:
                top = mod.split(".")[0].split("/")[0]
                modules[top] = modules.get(top, 0) + 1
                count += 1
    top_modules = sorted(modules.items(), key=lambda kv: kv[1], reverse=True)[:CAP]
    return {"count": count, "top_modules": [{"module": k, "uses": v} for k, v in top_modules]}


def _collect_tests(root, files):
    out = []
    for fp, ext, lang in files:
        base = os.path.basename(fp).lower()
        if "test" in base or "spec" in base:
            n = len(re.findall(r"\bdef\s+test_|\b(?:it|test|describe)\s*\(", _read(fp)))
            out.append({"file": _rel(root, fp), "cases": n})
        if len(out) > CAP:
            break
    return out[:CAP]


def _skill_catalog(root, log):
    """Run the run-any-codebase skill's extract_artifacts.py for the deep catalog."""
    import subprocess, sys, tempfile
    from django.conf import settings
    script = getattr(settings, "CODEINTEL_SKILL_EXTRACTOR", "")
    if not script or not os.path.exists(script):
        log("warn", "skill extractor not found — deep catalog skipped")
        return {}
    try:
        with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tf:
            out_path = tf.name
        log("info", "running run-any-codebase extract_artifacts.py …")
        proc = subprocess.run(
            [sys.executable, script, str(root), "--out", out_path, "--max-per", "80"],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            log("warn", "skill extractor returned non-zero — deep catalog skipped")
            return {}
        with open(out_path) as fh:
            data = json.load(fh)
        os.unlink(out_path)
        tot = data.get("totals", {})
        log("ok", f"deep catalog: {data.get('files_with_artifacts',0)} files, "
                  f"{tot.get('forms',0)} forms, {tot.get('sql',0)} SQL, "
                  f"{tot.get('request_params',0)} params ✓")
        # trim per-file pages to the busiest CAP files to bound payload
        pages = data.get("pages", {})
        ranked = sorted(pages.items(),
                        key=lambda kv: sum(len(v) for v in kv[1].values() if isinstance(v, list)),
                        reverse=True)[:120]
        data["pages"] = dict(ranked)
        return data
    except Exception as e:
        log("warn", f"skill extractor error ({type(e).__name__}) — deep catalog skipped")
        return {}


def _tree(root, files):
    """Top-level directory breakdown: files + LOC per first-level folder."""
    agg = {}
    for fp, ext, lang in files:
        rel = _rel(root, fp)
        top = rel.split(os.sep, 1)[0] if os.sep in rel else "(root)"
        d = agg.setdefault(top, {"dir": top, "files": 0, "loc": 0})
        d["files"] += 1
        try:
            with open(fp, "r", errors="ignore") as fh:
                d["loc"] += sum(1 for _ in fh)
        except OSError:
            pass
    return sorted(agg.values(), key=lambda x: x["files"], reverse=True)[:CAP]


# Framework-aware endpoint patterns → (regex, method_group, path_group, label).
# method_group=None means the label IS the method (e.g. URLconf).
_EP_BY_EXT = {
    ".py": [
        (re.compile(r"""\b(?:path|re_path|url)\(\s*[rf]?['"]([^'"]*)['"]"""), None, 1, "URLconf"),
        (re.compile(r"""@(?:app|router|bp|blueprint)\.(get|post|put|patch|delete|route)\(\s*['"]([^'"]*)['"]""", re.I), 1, 2, None),
        (re.compile(r"""router\.register\(\s*[rf]?['"]([^'"]*)['"]"""), None, 1, "router"),
    ],
    # Express / NestJS / Fastify (JS & TS)
    ".js": [
        (re.compile(r"""\b(?:app|router|api|server)\.(get|post|put|patch|delete|all)\(\s*['"`]([^'"`]+)""", re.I), 1, 2, None),
        (re.compile(r"""@(Get|Post|Put|Patch|Delete)\(\s*['"]?([^'")]*)""",), 1, 2, "nest"),
    ],
    # Spring / JAX-RS (Java & Kotlin)
    ".java": [
        (re.compile(r"""@(Get|Post|Put|Patch|Delete|Request)Mapping\b(?:\s*\(\s*(?:value\s*=\s*)?['"]?([^'")\s,]*))?""",), 1, 2, "spring"),
        (re.compile(r"""@(GET|POST|PUT|PATCH|DELETE)\b"""), 1, None, "jaxrs"),
    ],
    # ASP.NET Core (controllers, attribute routing, minimal APIs)
    ".cs": [
        (re.compile(r"""\[Http(Get|Post|Put|Patch|Delete)(?:\(\s*['"]?([^'")]*))?""",), 1, 2, "aspnet"),
        (re.compile(r"""\[Route\(\s*['"]([^'"]*)['"]"""), None, 1, "route"),
        (re.compile(r"""\.(MapGet|MapPost|MapPut|MapDelete)\(\s*['"]?([^'",)]*)""",), 1, 2, "minimal-api"),
    ],
    # Rails routes
    ".rb": [
        (re.compile(r"""^\s*(get|post|put|patch|delete)\s+['"]([^'"]+)""", re.M | re.I), 1, 2, None),
        (re.compile(r"""^\s*resources?\s+:(\w+)""", re.M), None, 1, "resource"),
    ],
    # Laravel / Slim (PHP)
    ".php": [
        (re.compile(r"""Route::(get|post|put|patch|delete|any)\(\s*['"]([^'"]+)""", re.I), 1, 2, None),
    ],
}
_EP_BY_EXT[".ts"] = _EP_BY_EXT[".tsx"] = _EP_BY_EXT[".jsx"] = _EP_BY_EXT[".js"]
_EP_BY_EXT[".kt"] = _EP_BY_EXT[".java"]
# GraphQL & gRPC (schema-driven; count operations as endpoints)
_GRAPHQL = re.compile(r"""^\s*(?:type\s+(?:Query|Mutation|Subscription)\b|(\w+)\s*\([^)]*\)\s*:)""", re.M)
_GRPC = re.compile(r"""\brpc\s+(\w+)\s*\(""")


def _collect_endpoints(root, files):
    out = []
    for fp, ext, lang in files:
        pats = _EP_BY_EXT.get(ext)
        rel = _rel(root, fp)
        txt = None
        if pats:
            txt = _read(fp)
            for rx, mg, pg, label in pats:
                for m in rx.finditer(txt):
                    method = (m.group(mg).upper() if mg else (label or "GET"))
                    path = ""
                    if pg and m.lastindex and pg <= m.lastindex and m.group(pg):
                        path = m.group(pg)
                    if not path:
                        path = "—" if mg else (label or "—")
                    out.append({"method": method, "path": path, "file": rel})
        if ext in (".graphql", ".gql", ".proto"):
            txt = txt if txt is not None else _read(fp)
            rx = _GRPC if ext == ".proto" else _GRAPHQL
            for m in rx.finditer(txt):
                name = (m.group(1) if m.lastindex else None) or "op"
                out.append({"method": "rpc" if ext == ".proto" else "graphql", "path": name, "file": rel})
        if len(out) > CAP:
            break
    return out[:CAP]


_MODEL_RX = re.compile(r"class\s+(\w+)\s*\([^)]*\bModel\b[^)]*\)\s*:")
_FIELD_RX = re.compile(r"^\s*(\w+)\s*=\s*models\.(\w+)", re.M)


def _collect_models(root, py):
    out = []
    for fp, *_ in py:
        txt = _read(fp)
        rel = _rel(root, fp)
        for m in _MODEL_RX.finditer(txt):
            # count fields in the ~60 lines after the class declaration
            tail = txt[m.end(): m.end() + 3000]
            nfields = len(_FIELD_RX.findall(tail))
            out.append({"name": m.group(1), "fields": nfields, "file": rel})
        if len(out) > CAP:
            break
    return out[:CAP]


def _collect_ui(root, files):
    """
    Detect the frontend layer in all its forms:
      - SPA pages/components  (JSX/TSX/Vue in pages|views|screens|routes|components)
      - server-rendered templates (Django/Jinja HTML, JSP, ASPX, Razor, ERB, PHP)
    api_only is True when a codebase has NO frontend of any kind — e.g. a pure
    REST API like django-realworld (its UI lives in a separate repo).
    """
    pages, comps, templates = [], [], []
    for fp, ext, lang in files:
        rel = _rel(root, fp)
        parts = {p.lower() for p in Path(fp).parts}
        name = os.path.basename(fp)
        if ext in (".js", ".jsx", ".ts", ".tsx", ".vue"):
            if parts & {"pages", "views", "screens", "routes"}:
                pages.append({"name": name, "file": rel, "kind": "SPA page"})
            elif parts & {"components", "component"}:
                comps.append({"name": name, "file": rel})
        elif ext == ".html":
            if parts & {"templates", "template"}:
                templates.append({"name": name, "file": rel, "kind": "template"})
        elif ext in (".jsp", ".aspx", ".cshtml", ".erb", ".php"):
            templates.append({"name": name, "file": rel, "kind": "server page"})
    api_only = not (pages or comps or templates)
    return {"pages": pages[:CAP], "components": comps[:CAP],
            "templates": templates[:CAP], "api_only": api_only}


def _integration_locations(root, files):
    out = []
    for name, pat in INTEGRATION_SIGNALS.items():
        rx = re.compile(pat, re.IGNORECASE)
        for fp, *_ in files:
            if rx.search(_read(fp, 120_000)):
                out.append({"name": name, "sample_file": _rel(root, fp)})
                break
    return out


def _parse_deps(root):
    py_deps, node_deps = [], []
    req = _find_first(root, "requirements.txt")
    if req:
        for line in _read(req, 40_000).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                py_deps.append(line)
    pkg = _find_first(root, "package.json")
    if pkg:
        try:
            data = json.loads(_read(pkg, 60_000))
            for section in ("dependencies", "devDependencies"):
                for k, v in (data.get(section) or {}).items():
                    node_deps.append(f"{k}@{v}")
        except Exception:
            pass
    return {"python": py_deps[:CAP], "node": node_deps[:CAP]}


_CREATE_TBL = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?([\w\.]+)", re.I)


def _db_artifacts(root, files):
    migrations = 0
    tables = []
    dbtype = []
    for fp, ext, lang in files:
        low = fp.lower()
        if "migrations" in low and fp.endswith(".py") and "__init__" not in low:
            migrations += 1
        if ext == ".sql":
            for m in _CREATE_TBL.finditer(_read(fp)):
                tables.append(m.group(1))
    blob = ""
    for mani in ("settings.py", "docker-compose.yml", "docker-compose.yaml", ".env"):
        p = _find_first(root, mani)
        if p:
            blob += _read(p, 30_000).lower()
    for name, key in (("PostgreSQL", "postgres"), ("MySQL", "mysql"),
                      ("SQLite", "sqlite"), ("MongoDB", "mongo"),
                      ("SQL Server", "mssql"), ("Oracle", "oracle")):
        if key in blob:
            dbtype.append(name)
    return {"engines": sorted(set(dbtype)), "migrations": migrations,
            "tables": sorted(set(tables))[:CAP]}


def _config_artifacts(root, files):
    services = []
    envs = []
    comp = _find_first(root, "docker-compose.yml") or _find_first(root, "docker-compose.yaml")
    if comp:
        txt = _read(comp, 30_000)
        m = re.search(r"^services:\s*$", txt, re.M)
        if m:
            for sm in re.finditer(r"^\s{2}(\w[\w-]*):\s*$", txt[m.end():], re.M):
                services.append(sm.group(1))
    env = _find_first(root, ".env") or _find_first(root, ".env.example")
    if env:
        for line in _read(env, 20_000).splitlines():
            if "=" in line and not line.strip().startswith("#"):
                envs.append(line.split("=", 1)[0].strip())
    dockerfiles = [ _rel(root, fp) for fp, *_ in files
                    if os.path.basename(fp).lower().startswith("dockerfile") ]
    return {"compose_services": services[:CAP], "env_keys": envs[:CAP],
            "dockerfiles": dockerfiles[:20], "has_compose": bool(comp)}


def _migration_notes(frameworks, integrations, auth, legacy):
    notes = []
    if legacy:
        notes.append(f"Legacy signals present: {', '.join(legacy)} — plan a data/schema bridge.")
    if "MySQL backend" in legacy:
        notes.append("MySQL → target DB migration required (type/charset/collation mapping).")
    if integrations:
        notes.append(f"Re-integrate external systems: {', '.join(integrations)}.")
    if auth:
        notes.append(f"Preserve auth model: {', '.join(auth)} (security-critical).")
    if not notes:
        notes.append("No blocking legacy signals detected — greenfield-style rebuild feasible.")
    return notes
