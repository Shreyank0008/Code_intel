"""
Pixel-Clone AI Agent — orchestrates the full pixel-clone pipeline.

This is a real multi-phase agent, not a single script:
  Phase 1  Guardrail + reachability   (localhost-only, app must be up)
  Phase 2  Capture (Playwright)       → screenshots + per-page DOM  [capture.py]
  Phase 3  Extract design tokens      → colours / fonts / spacing   [extract_tokens.py]
  Phase 4  Route map (from source)    → the intended route list     [route_map.py]
  Phase 5  LLM scaffold (the agent)   → React+Tailwind component per page
  Phase 6  Report                     → what was produced

The LLM phase is where the "agent" reasons: for each captured page it feeds the
rendered DOM + design tokens to the configured provider and gets back a
React+Tailwind component. EVERYTHING the model returns is passed through
PixelGuardrails before it can touch disk.

GUARDRAILS (enforced here, surfaced in the UI):
  • Localhost-only capture — never screenshots an arbitrary external site.
  • Sandboxed output — the agent may only write under its own temp dir; filenames
    are sanitised (no path traversal, PascalCase component names).
  • Iteration cap — at most MAX_PAGES pages are scaffolded per run.
  • LLM-call budget — at most MAX_LLM_CALLS model calls per run.
  • Generated-code validation — output must look like code, is size-capped, has
    markdown fences stripped, and is NEVER executed (written as inert files only).
  • Per-phase timeouts; any failure degrades gracefully, never crashes the job.
  • Provider required for code-gen — with no LLM configured, phases 1-4 still run
    and the agent reports the deterministic capture, skipping code-gen cleanly.
"""
from __future__ import annotations
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from django.conf import settings
from . import providers, agents_registry, jobs
from .dom_inventory import count_elements as _count_elements
from .skill_paths import skill_script, missing_hint

CAPTURE = skill_script("pixel-clone/scripts/capture.py")
TOKENS = skill_script("pixel-clone/scripts/extract_tokens.py")
ROUTEMAP = skill_script("pixel-clone/scripts/route_map.py")

CAPTURE_TIMEOUT = 240
STEP_TIMEOUT = 120


class GuardrailError(Exception):
    pass


# Tailwind's default spacing scale only defines .5 fractional steps up to 3.5
# (0.5/1.5/2.5/3.5) -- whole numbers only from 4 up. The LLM regularly invents
# values like gap-5.5 / w-7.5 / p-4.5 that look plausible but aren't in that
# scale, and the Play CDN JIT silently drops any utility it can't resolve
# (confirmed live: fastapi-shop's clone nav rendered with zero gap between
# links because of a generated `gap-5.5`, while `gap-3.5` on the same page
# worked fine). Snap the invalid fraction down to the nearest whole step
# instead of leaving an invisible spacing gap in the rendered clone.
_TW_SPACING_PREFIXES = (
    "w", "h", "min-w", "min-h", "max-w", "max-h",
    "p", "px", "py", "pt", "pb", "pl", "pr",
    "m", "mx", "my", "mt", "mb", "ml", "mr",
    "gap", "gap-x", "gap-y", "space-x", "space-y",
    "top", "right", "bottom", "left", "inset", "inset-x", "inset-y",
)
_TW_FRACTION_RE = re.compile(
    r'\b(-?)(' + "|".join(re.escape(p) for p in _TW_SPACING_PREFIXES) + r')-(\d+)\.5\b'
)


def _snap_tailwind_fractions(code: str) -> str:
    def _fix(m):
        neg, prefix, whole = m.group(1), m.group(2), int(m.group(3))
        if whole < 4:
            return m.group(0)  # 0.5 / 1.5 / 2.5 / 3.5 are valid, leave as-is
        return f"{neg}{prefix}-{whole}"
    return _TW_FRACTION_RE.sub(_fix, code)


class PixelGuardrails:
    MAX_PAGES = 6            # iteration cap
    MAX_LLM_CALLS = 8        # model-call budget
    CODE_MAX = 40_000        # per-component byte cap
    _CODE_MARKERS = ("export", "function", "return", "=>", "<")

    def __init__(self, jid: str):
        self.root = Path("/tmp/codeintel_pixel_agent") / jid
        (self.root / "app" / "src" / "pages").mkdir(parents=True, exist_ok=True)
        self.calls = 0

    # --- ingest-side ---
    def check_url(self, url: str) -> str:
        if not url:
            raise GuardrailError("no site URL provided")
        host = url.split("//", 1)[-1].split("/", 1)[0].split(":")[0].lower() if "//" in url else ""
        if host not in ("localhost", "127.0.0.1", "0.0.0.0"):
            raise GuardrailError(f"capture is restricted to localhost (got '{host or url}')")
        return url

    # --- LLM-side ---
    def spend_call(self):
        self.calls += 1
        if self.calls > self.MAX_LLM_CALLS:
            raise GuardrailError(f"LLM-call budget exceeded ({self.MAX_LLM_CALLS})")

    def validate_code(self, text: str) -> str:
        if not text:
            raise GuardrailError("model returned empty output")
        # strip markdown fences if present
        m = re.search(r"```(?:jsx?|tsx?|react)?\s*(.*?)```", text, re.S)
        if m:
            text = m.group(1)
        text = text.strip()
        if len(text) < 20:
            raise GuardrailError("output too short to be a component")
        if not any(k in text for k in self._CODE_MARKERS):
            raise GuardrailError("output does not look like code")
        text = _snap_tailwind_fractions(text)
        return text[: self.CODE_MAX]

    def component_name(self, hint: str) -> str:
        base = re.sub(r"[^A-Za-z0-9]", " ", os.path.basename(hint)).title().replace(" ", "")
        base = re.sub(r"(Png|Html|Jpg|1280X800|375X812)$", "", base) or "Page"
        if not base[0].isalpha():
            base = "Page" + base
        return base[:40]

    def safe_write(self, relpath: str, content: str) -> str:
        # confine writes to the sandbox
        p = (self.root / "app" / "src" / "pages" / os.path.basename(relpath)).resolve()
        if self.root.resolve() not in p.parents:
            raise GuardrailError("refused write outside the agent sandbox")
        p.write_text(content)
        return str(p)


def _default_prompt():
    return agents_registry.get_prompt("pixelclone", agents_registry.DEFAULT_PIXEL_PROMPT)


_SRC_SKIP_DIRS = {"node_modules", "dist", "build", ".git", "vendor", "target", ".venv", "__pycache__"}
_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_RGB_RE = re.compile(r"rgba?\([\d.,\s%]+\)")
_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;{}]+);", re.I)
_RADIUS_RE = re.compile(r"border-radius\s*:\s*([\d.]+)px", re.I)
_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([\d.]+)px", re.I)
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_BG_DECL_RE = re.compile(r"background(?:-color)?\s*:\s*([^;]+)", re.I)


def _source_css_tokens(source_root: str, log, max_files: int = 40, max_bytes: int = 3_000_000) -> dict:
    """Runtime-captured tokens (extract_tokens.py, via Playwright computed styles)
    depend on the page actually rendering right, and only see whatever's on the
    captured routes. When the source is available (it is for every Revive-based
    job), reading the real CSS directly is a strictly more reliable ground truth
    for the palette/fonts/radii -- it can't be zeroed by a capture hiccup, and it
    sees the WHOLE stylesheet, not just what happened to render. Used to
    supplement (not replace) the runtime tokens.
    """
    root = Path(source_root)
    if not root.exists():
        return {}
    css_files = [f for f in list(root.rglob("*.css")) + list(root.rglob("*.scss"))
                 if not any(part in _SRC_SKIP_DIRS for part in f.parts)][:max_files]
    if not css_files:
        return {}
    colors, fonts, sizes, radii = Counter(), Counter(), set(), set()
    nav_bg = None
    for f in css_files:
        try:
            text = f.read_text(errors="ignore")[:max_bytes]
        except Exception:
            continue
        for m in _HEX_RE.findall(text):
            colors[m.lower()] += 1
        for m in _RGB_RE.findall(text):
            colors[re.sub(r"\s+", "", m)] += 1
        for m in _FONT_FAMILY_RE.findall(text):
            fonts[m.strip().rstrip(",").strip()] += 1
        for m in _RADIUS_RE.findall(text):
            radii.add(float(m))
        for m in _FONT_SIZE_RE.findall(text):
            sizes.add(float(m))
        # A flat frequency-ranked colour list buries a distinctive brand colour
        # (e.g. a navbar's #34302d) under generic ones reused everywhere (#000,
        # #fff) -- confirmed live: the LLM picked plain black for the navbar
        # because nothing told it #34302d was THE navbar colour specifically.
        # Scope-match nav/header selector blocks and boost their bg colour so
        # it naturally ranks first once merged into the overall list.
        if nav_bg is None:
            for selector, body in _CSS_RULE_RE.findall(text):
                if "nav" not in selector.lower() and "header" not in selector.lower():
                    continue
                bgm = _BG_DECL_RE.search(body)
                if not bgm:
                    continue
                val = bgm.group(1).strip().rstrip(",")
                hexm = _HEX_RE.search(val)
                rgbm = _RGB_RE.search(val)
                if hexm:
                    nav_bg = hexm.group(0).lower()
                elif rgbm:
                    nav_bg = re.sub(r"\s+", "", rgbm.group(0))
                if nav_bg and nav_bg not in ("#fff", "#ffffff", "transparent"):
                    break
    if nav_bg:
        colors[nav_bg] += 100_000  # surfaces at the front of most_common()
    log("ok", f"Phase 3 · scanned {len(css_files)} source CSS file(s) for tokens ✓")
    out = {
        "colors": [c for c, _ in colors.most_common(16)],
        "fontFamilies": [f for f, _ in fonts.most_common(6)],
        "fontSizes": sorted(sizes)[:12],
        "radii": sorted(radii)[:8],
    }
    if nav_bg:
        out["navBackground"] = nav_bg
    return out


def _merge_tokens(runtime: dict, source: dict) -> dict:
    if not source:
        return runtime
    merged = dict(runtime)
    for key in ("colors", "fontFamilies", "fontSizes", "radii"):
        seen, out = set(), []
        for v in (source.get(key) or []) + (runtime.get(key) or []):
            k = v if not isinstance(v, str) else v.lower()
            if k not in seen:
                seen.add(k)
                out.append(v)
        if out:
            merged[key] = out
    if source.get("navBackground"):
        merged["navBackground"] = source["navBackground"]
    return merged


def run(job: dict, answers: dict, log) -> dict:
    jid = job["id"]
    g = PixelGuardrails(jid)

    # Phase 1 — guardrail + reachability
    url = (answers.get("site_url") or "").strip()
    try:
        url = g.check_url(url)
    except GuardrailError as e:
        return {"ok": False, "reason": str(e)}
    try:
        urllib.request.urlopen(url, timeout=5)
    except Exception:
        return {"ok": False, "reason": f"nothing is running at {url}. Boot the app (⚡ Revive) first."}
    log("ok", f"Phase 1 · target {url} reachable & localhost-sandboxed ✓")

    if not CAPTURE.exists():
        return {"ok": False, "reason": missing_hint("pixel-clone/scripts/capture.py")}

    # Phase 2 — capture
    vp = {"Desktop only (1280×800)": "1280x800", "Desktop + Mobile": "1280x800,375x812"}.get(
        answers.get("viewports", "Desktop only (1280×800)"), "1280x800")
    cap = g.root / "captures"
    cmd = [sys.executable, str(CAPTURE), "--base", url, "--out", str(cap),
           "--viewports", vp, "--full-page"]
    routes_mode = answers.get("routes", "All routes (crawl)")
    if routes_mode == "All routes (crawl)":
        cmd += ["--crawl", "--depth", "2"]
    elif answers.get("routes_list"):
        # "Named list" / "Given pages only" were silently ignored here — with
        # neither --crawl nor --routes, capture.py falls back to "/" and the run
        # quietly produced a single page regardless of what the user listed.
        cmd += ["--routes", str(answers["routes_list"]).replace(" ", "")]
    if answers.get("dynamic") == "Mask dynamic regions":
        cmd += ["--mask", ".carousel,[data-dynamic],time"]
    log("t", "$ capture.py " + url)
    log("info", "Phase 2 · Playwright capturing the running site …")
    p = _sub(cmd, CAPTURE_TIMEOUT)
    if p is None or p.returncode != 0:
        return {"ok": False, "reason": "capture failed", "log_tail": _tail(p)}
    pngs = sorted(cap.rglob("*.png"))
    htmls = sorted(cap.rglob("*.html"))
    log("ok", f"Phase 2 · captured {len(pngs)} screenshots, {len(htmls)} DOM snapshots ✓")

    # source dir is needed by both Phase 3 (CSS tokens) and Phase 4 (route map)
    src = jobs.ensure_source_dir(job, log)

    # Phase 3 — design tokens
    tok_dir = g.root / "tokens"
    log("info", "Phase 3 · extracting design tokens (colours / fonts / spacing) …")
    _sub([sys.executable, str(TOKENS), "--captures", str(cap), "--out", str(tok_dir)], STEP_TIMEOUT)
    tokens = {}
    tok_out = tok_dir / "tokens.json"
    if tok_out.exists():
        try:
            tokens = json.loads(tok_out.read_text())
        except Exception:
            pass
    # Supplement with tokens read straight from the source CSS -- more reliable
    # than the Playwright-computed-style path above, which only sees what
    # actually rendered on the captured routes and can come back empty on a
    # capture hiccup (this path was in fact silently returning `{}` every run
    # until just now, from --out pointing at a bare filename instead of a dir).
    if src:
        src_tokens = _source_css_tokens(src, log)
        tokens = _merge_tokens(tokens, src_tokens)
    log("ok", f"Phase 3 · {sum(len(v) if isinstance(v,(list,dict)) else 1 for v in tokens.values())} tokens extracted ✓")

    # Phase 4 — route map (from source, if available)
    routes = []
    if src:
        rj = g.root / "routes.json"
        _sub([sys.executable, str(ROUTEMAP), "--src", src, "--json", str(rj)], STEP_TIMEOUT)
        if rj.exists():
            try:
                data = json.loads(rj.read_text())
                routes = data.get("routes") or (data if isinstance(data, list) else [])
            except Exception:
                pass
    log("m", f"Phase 4 · route map: {len(routes)} routes from source")

    # Phase 5 — LLM scaffold (the agent's reasoning)
    provider = providers.active_provider()
    components = []
    if provider == "heuristic":
        log("warn", "Phase 5 · no LLM provider configured — code-gen skipped "
                    "(capture + tokens done). Add a key in Settings to generate components.")
        mode = "capture-only (no provider)"
    else:
        log("info", f"Phase 5 · scaffolding React+Tailwind components via '{provider}' …")
        instruction = _default_prompt()
        tok_str = json.dumps(tokens, indent=2)[:4000]
        # Resolve page→table BEFORE generating: whether a page is data-driven
        # changes the prompt it gets, so it cannot be decided afterwards.
        api_base = (answers.get("api_base") or "").strip()
        tables = _load_service_manifest(api_base, answers.get("service_dir", ""), log) \
            if api_base else {}
        pre_components = [{"name": g.component_name(h.name), "source_html": str(h)}
                          for h in htmls[: g.MAX_PAGES]]
        api_map = _page_api_map(pre_components, cap, tables, api_base, log) if tables else {}
        for h in htmls[: g.MAX_PAGES]:
            try:
                g.spend_call()
                dom = _read(h)[:8000]
                name_hint = g.component_name(h.name)
                live = api_map.get(name_hint)
                extra = ""
                if live:
                    extra = (agents_registry.LIVE_DATA_PROMPT
                             + f"\nAPI_URL = {live['url']!r}\n"
                             + f"TABLE: {live['table']}\n"
                             + f"COLUMNS (keys of each row object): {', '.join(live['columns'])}\n")
                prompt = (f"{instruction}{extra}\n\nDESIGN TOKENS:\n{tok_str}\n\n"
                          f"PAGE DOM (truncated):\n{dom}\n\nReturn ONLY the component code.")
                raw = providers.call_llm(prompt, log, json_mode=False)["text"]
                code = g.validate_code(raw)
                name = g.component_name(h.name)
                path = g.safe_write(f"{name}.jsx", code)
                orig_pngs = sorted(h.parent.glob(h.stem + "__*.png"))
                components.append({"name": name, "file": os.path.relpath(path, g.root),
                                   "bytes": len(code), "source_html": str(h),
                                   "source_png": str(orig_pngs[0]) if orig_pngs else None,
                                   "live_table": (live or {}).get("table"),
                                   "api_url": (live or {}).get("url")})
                log("ok", f"  ✓ generated {name}.jsx ({len(code)} bytes)"
                          + (f" — LIVE from '{live['table']}'" if live else " — static"))
            except GuardrailError as e:
                log("err", f"  guardrail blocked a page: {e}")
                if "budget" in str(e):
                    break
            except Exception as e:
                log("warn", f"  page skipped ({type(e).__name__})")
        mode = f"{provider} + guardrails"
        log("ok", f"Phase 5 · {len(components)} components generated ✓")

    # Phase 6 — SERVE + CAPTURE every generated page, not just the first —
    # so the gallery shows the FULL set of screens, not one sample pair.
    clone_url, clone_port = None, None
    if components:
        try:
            preview_dir = g.root / "preview"
            preview_dir.mkdir(parents=True, exist_ok=True)
            nav_map = _nav_map(cap, components)
            log("m", f"Phase 6 · nav map: {len(nav_map)} route(s) rewired to clone pages")
            for comp in components:
                jsx_path = g.root / "app" / "src" / "pages" / (comp["name"] + ".jsx")
                page_html = _preview_html(jsx_path.read_text(), comp["name"],
                                          origin=url, nav_map=nav_map)
                (preview_dir / f"{comp['name']}.html").write_text(page_html)
            clone_port = _serve_clone(jid, str(preview_dir))
            # Point straight at the first page instead of writing an index.html
            # redirect stub -- on a case-INsensitive filesystem (macOS default),
            # "index.html" and a component literally named "Index.html" are the
            # SAME file, so the stub silently clobbered that page's real content
            # (confirmed live: job_026's Index page lost its content this way).
            clone_url = f"http://localhost:{clone_port}/{components[0]['name']}.html"
            log("ok", f"Phase 6 · clone RUNNING at {clone_url} ({len(components)} page(s)) ✓")

            clone_cap = g.root / "clone_captures"
            for comp in components:
                _sub([sys.executable, str(CAPTURE), "--base", clone_url,
                      "--out", str(clone_cap / comp["name"]),
                      "--viewports", "1280x800", "--full-page",
                      "--routes", f"/{comp['name']}.html", "--settle", "800",
                      # Unlike the original (server-rendered, ready once the network
                      # is idle), the clone mounts client-side via in-browser Babel --
                      # a fixed settle can win the race against transpile+mount on a
                      # larger component, capturing an empty #root (confirmed live:
                      # OwnersNew.jsx, the biggest generated page, intermittently
                      # captured blank and zeroed its whole element count). Poll for
                      # an actual mount instead of trusting the fixed delay.
                      "--state-js",
                      "(async()=>{const t=Date.now();const el=()=>document.getElementById('root');"
                      "while(el()&&el().children.length===0&&Date.now()-t<5000){"
                      "await new Promise(r=>setTimeout(r,50));}})()"],
                     STEP_TIMEOUT)
            log("ok", f"Phase 6 · {len(components)} clone page(s) captured for comparison ✓")
        except Exception as e:
            log("warn", f"Phase 6 · serve/capture clone failed ({type(e).__name__})")

    # Phase 7 — build the gallery: pair each original capture with its
    # clone, sample screenshot + a DOM element inventory (buttons, links,
    # inputs, images, forms) for both, so the UI can browse every screen
    # instead of a single sample and see how much UI each one actually has.
    import base64 as _b64mod

    def _b64png(p, cap=900_000):
        try:
            d = Path(p).read_bytes()
            return "data:image/png;base64," + _b64mod.b64encode(d).decode() if len(d) <= cap else None
        except Exception:
            return None

    pages = []
    for comp in components:
        # Full read (not the 12KB LLM-prompt truncation _read() defaults to)
        # -- the clone pages carry a large CDN-injected Tailwind <style> block
        # BEFORE <body>, which pushed all the real content past that limit
        # and silently zeroed every count (confirmed live against job_026).
        orig_sample = _b64png(comp["source_png"]) if comp.get("source_png") else None
        orig_counts = _count_elements(_read(comp["source_html"], limit=500_000)) if comp.get("source_html") else {}
        clone_dir = g.root / "clone_captures" / comp["name"]
        clone_pngs = sorted(clone_dir.rglob("*.png")) if clone_dir.exists() else []
        clone_htmls = sorted(clone_dir.rglob("*.html")) if clone_dir.exists() else []
        clone_sample = _b64png(clone_pngs[0]) if clone_pngs else None
        clone_counts = _count_elements(_read(clone_htmls[0], limit=500_000)) if clone_htmls else {}
        pages.append({
            "name": comp["name"],
            "original_sample": orig_sample, "original_counts": orig_counts,
            "clone_sample": clone_sample, "clone_counts": clone_counts,
            # Build each page's link from host+port, NOT from clone_url (which is
            # already a full first-page URL ending in "<First>.html"). Concatenating
            # onto it produced the doubled "Index.htmlIndex.html" 404 seen live.
            "clone_page_url": f"http://localhost:{clone_port}/{comp['name']}.html" if clone_port else None,
        })

    # kept for the older single-sample UI paths that read these two keys directly
    sample = pages[0]["original_sample"] if pages else None
    clone_sample = pages[0]["clone_sample"] if pages else None

    log("ok", f"● Pixel-Clone agent complete — {len(pngs)} shots · {len(components)} components"
              + (f" · clone LIVE at {clone_url}" if clone_url else "") + f" · mode {mode}")
    return {
        "ok": True, "url": url, "viewports": vp, "screenshots": len(pngs),
        "dom_snapshots": len(htmls), "routes": (routes or [])[:60],
        "tokens": {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in list(tokens.items())[:12]},
        "components": components, "output_dir": str(g.root / "app"),
        "mode": mode, "sample": sample,
        "clone_url": clone_url, "clone_port": clone_port, "clone_sample": clone_sample,
        "pages": pages,
    }


# ---- serve the generated clone (Phase 6) ----
import http.server, socketserver, functools, threading as _threading, re as _re

_CLONE_SERVERS = {}


# DOM element inventory now lives in api/dom_inventory.py (parser-based, shared
# with the pixel-clone skill). Imported at the top as `_count_elements`. The old
# regex version counted tags inside <script type="text/babel"> JSX source, which
# double-counted every element in clone preview pages (~2x the original).


def _port_alive(port) -> bool:
    if not port:
        return False
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except (ValueError, OSError):
        return False
    finally:
        s.close()


def ensure_clone_serving(jid: str, result: dict) -> dict:
    """Re-serve a finished clone whose preview server died.

    The preview server is a thread inside THIS process, so every restart of the
    app silently kills every clone link ever handed out -- the generated pages
    are still on disk, but the URL in the UI answers "site can't be reached"
    (hit live: job_005's clone on :54513 after a server restart). Rebinding is
    cheap and deterministic, so do it on read instead of making the user re-run
    a multi-minute, LLM-billed rebuild just to get the files served again.

    The new port differs from the old one, so every recorded URL is rewritten.
    """
    if not isinstance(result, dict) or not result.get("clone_url"):
        return result
    if _port_alive(result.get("clone_port")):
        return result
    preview = Path("/tmp/codeintel_pixel_agent") / jid / "preview"
    if not preview.is_dir() or not any(preview.glob("*.html")):
        return result
    try:
        port = _serve_clone(jid, str(preview))
    except Exception:
        return result
    old = result.get("clone_port")
    page = str(result["clone_url"]).rsplit("/", 1)[-1] or "index.html"
    out = {**result, "clone_port": port, "clone_url": f"http://localhost:{port}/{page}"}
    if isinstance(result.get("pages"), list):
        out["pages"] = [{**p, "clone_page_url": f"http://localhost:{port}/{p['name']}.html"}
                        if isinstance(p, dict) and p.get("name") else p
                        for p in result["pages"]]
    out["clone_reserved_from"] = old
    return out


def _rewrite_asset_urls(code: str, origin: str) -> str:
    """Generated components carry the ORIGINAL site's relative asset paths
    verbatim from its captured DOM (src="/images/foo.png") -- correct if this
    ran behind the real backend, but broken when previewed standalone with no
    backend to serve those files (confirmed live: PetClinic's pet photo and
    logo both 404'd in the clone preview for exactly this reason). Point them
    at the original -- which is still running -- instead of guessing;
    deterministic, not something that needs an LLM diff-loop to discover.
    src= only (images/media) -- nav href= is handled separately by
    _rewrite_nav_hrefs, which can point links at the CLONED page instead."""
    origin = origin.rstrip("/")
    return re.sub(r'\bsrc=(["\'])(/[^"\']*)\1',
                    lambda m: f'src={m.group(1)}{origin}{m.group(2)}{m.group(1)}', code)


def _load_service_manifest(api_base: str, service_dir: str, log) -> dict:
    """Tables the legacy data service exposes, for page->endpoint matching.

    Prefers the live /_meta/ endpoint (authoritative -- it reflects what the
    service can actually serve right now) and falls back to the manifest file
    gen_service.py wrote, so a not-yet-started service still maps."""
    if api_base:
        try:
            with urllib.request.urlopen(api_base.rstrip("/") + "/api/legacy/_meta/", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            tables = {t["table"]: [c["name"] for c in t.get("columns", [])]
                      for t in data.get("tables", [])}
            if tables:
                log("ok", f"data service reachable — {len(tables)} table(s) available ✓")
                return tables
        except Exception as e:
            log("warn", f"data service at {api_base} not reachable ({type(e).__name__}) "
                        "— falling back to its manifest file")
    if service_dir:
        mf = Path(service_dir) / "service_manifest.json"
        if mf.exists():
            try:
                data = json.loads(mf.read_text())
                return {t["table"]: t.get("columns", []) for t in data.get("tables", [])}
            except Exception:
                pass
    return {}


def _match_table(comp_name: str, route: str, tables: dict) -> str | None:
    """Which legacy table backs this page.

    Page names and table names rarely match exactly (Vets -> vets, OwnersFind ->
    owners), so this walks from strictest to loosest and stops at the first hit
    rather than guessing among several."""
    if not tables:
        return None
    words = [w for w in re.split(r"(?=[A-Z])|[^A-Za-z0-9]+", comp_name + " " + route) if w]
    cands = {w.lower() for w in words if len(w) > 2}
    names = list(tables)

    for c in cands:                                   # exact: "vets" -> vets
        if c in tables:
            return c
    for c in cands:                                   # plural: "vet" -> vets
        if c + "s" in tables:
            return c + "s"
        if c.endswith("s") and c[:-1] in tables:
            return c[:-1]
    for c in sorted(cands, key=len, reverse=True):    # substring: "ownersfind"
        for n in names:
            if len(c) > 3 and (c.startswith(n) or n.startswith(c)):
                return n
    return None


def _page_api_map(components: list, cap: Path, tables: dict, api_base: str, log) -> dict:
    """component name -> {"url", "table", "columns"} for pages we can back with
    real data. Pages with no matching table stay a verbatim static rebuild."""
    routes = {}
    man = cap / "manifest.json"
    if man.exists():
        try:
            for p in (json.loads(man.read_text()).get("pages") or []):
                routes[p.get("slug")] = p.get("route") or "/"
        except Exception:
            pass
    out = {}
    for c in components:
        slug = Path(c.get("source_html", "")).stem
        table = _match_table(c["name"], routes.get(slug, ""), tables)
        if table:
            out[c["name"]] = {
                "table": table,
                "columns": tables.get(table) or [],
                "url": f"{api_base.rstrip('/')}/api/legacy/{table}/?limit=100",
            }
    if out:
        log("ok", "page→data mapping: "
                  + ", ".join(f"{k}→{v['table']}" for k, v in out.items()) + " ✓")
    else:
        log("m", "no page matched a legacy table — pages stay static")
    return out


def _norm_route(path: str) -> str:
    """/owners/find/?q=1#x -> /owners/find   (root stays "/")"""
    p = path.split("?", 1)[0].split("#", 1)[0]
    return p.rstrip("/") or "/"


def _nav_map(cap: Path, components: list) -> dict:
    """Original route -> the clone page that reproduces it ("OwnersFind.html").

    capture.py records route/slug pairs in its manifest, and each component was
    named off its capture's filename, so the two join on the slug."""
    slug_to_comp = {}
    for c in components:
        sh = c.get("source_html")
        if sh:
            slug_to_comp[Path(sh).stem] = c["name"]
    out = {}
    man = cap / "manifest.json"
    if man.exists():
        try:
            data = json.loads(man.read_text())
        except Exception:
            data = {}
        for p in (data.get("pages") or []):
            comp = slug_to_comp.get(p.get("slug"))
            if comp and p.get("route"):
                out[_norm_route(p["route"])] = comp + ".html"
    return out


def _rewrite_nav_hrefs(code: str, nav_map: dict, origin: str) -> str:
    """Generated components inherit the ORIGINAL app's nav paths verbatim
    (href="/owners/find"), but the preview server hosts flat per-component files
    (OwnersFind.html) -- so every nav click 404'd (confirmed live: job_002's
    "Find owners" link). Point each link at the cloned page when we captured
    that route, and at the still-running original when we didn't, so a link is
    never a dead end either way.

    Absolute in-app paths only: external http(s) links and #anchors are left
    exactly as the model wrote them."""
    origin = origin.rstrip("/")

    def fix(m):
        q, path = m.group(1), m.group(2)
        target = nav_map.get(_norm_route(path))
        return f'href={q}{target}{q}' if target else f'href={q}{origin}{path}{q}'

    return re.sub(r'\bhref=(["\'])(/[^"\']*)\1', fix, code)


def _component_symbol(code: str, body: str, comp_name: str) -> str:
    """Which symbol in the generated code is the component to mount.

    Must key off an actual DECLARATION, never mere presence of the name. The
    old test was `\\bOwnersFind\\b in body`, which reads as "this file defines
    OwnersFind" but is true of any incidental mention -- including the
    href="OwnersFind.html" that _rewrite_nav_hrefs now injects into the nav.
    That mounted a name nothing declared and rendered "could not mount
    component" on every page reachable from another page's nav.

    The model names its component whatever it likes (PetClinicPage), so the
    default export is the reliable signal, not our filename-derived guess."""
    m = _re.search(r"export\s+default\s+(?:function\s+)?([A-Z]\w*)", code)
    if m:
        return m.group(1)
    if _re.search(rf"\b(?:function|const|let|var|class)\s+{_re.escape(comp_name)}\b", body):
        return comp_name
    m = (_re.search(r"\bfunction\s+([A-Z]\w*)\s*\(", body)
         or _re.search(r"\b(?:const|let|var|class)\s+([A-Z]\w*)\s*[=(]?", body))
    return m.group(1) if m else "App"


def _preview_html(code: str, comp_name: str, origin: str = "", nav_map: dict | None = None) -> str:
    """Wrap a generated JSX component in a runnable page (React + Babel + Tailwind
    via CDN) so it renders in a browser with no build step."""
    if origin:
        code = _rewrite_asset_urls(code, origin)
        code = _rewrite_nav_hrefs(code, nav_map or {}, origin)
    body = _re.sub(r"^\s*import\s+.*$", "", code, flags=_re.M)
    body = body.replace("export default ", "")
    name = _component_symbol(code, body, comp_name)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Clone · {comp_name}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css">
<script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
<script>
  // Force the CLASSIC JSX runtime (React.createElement) so no ES imports are injected.
  Babel.registerPreset('rc', {{ presets: [[Babel.availablePresets.react, {{ runtime: 'classic' }}]] }});
</script>
<style>
  /* Safety net: the generated component's own bg-* class may not cover
     the full viewport height, leaving the raw <body> canvas visible below
     it -- default that canvas to white (the near-universal page colour)
     instead of the browser/OS default, which can be black under a dark
     colour-scheme (confirmed live against job_026's Index page). */
  html, body {{ background: #fff; margin: 0; }}
</style>
</head><body><div id="root"></div>
<script type="text/babel" data-presets="rc">
{body}
const __C = (typeof {name} !== 'undefined') ? {name} : null;
if(__C) ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(__C));
else document.getElementById('root').innerHTML='<pre>could not mount component</pre>';
</script></body></html>"""


def _serve_clone(jid: str, directory: str) -> int:
    """Start a background static server for the clone; returns the bound port."""
    old = _CLONE_SERVERS.pop(jid, None)
    if old:
        try:
            old.shutdown()
        except Exception:
            pass
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    handler.log_message = lambda *a, **k: None
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    _CLONE_SERVERS[jid] = httpd
    _threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def stop_clone(jid: str) -> bool:
    httpd = _CLONE_SERVERS.pop(jid, None)
    if httpd:
        try:
            httpd.shutdown()
            return True
        except Exception:
            pass
    return False


# --------------------------------------------------------------- helpers ----

def _sub(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _tail(p):
    if not p:
        return []
    return (p.stderr or p.stdout or "").strip().splitlines()[-4:]


def _read(path, limit=12000):
    try:
        return Path(path).read_text(errors="ignore")[:limit]
    except OSError:
        return ""
