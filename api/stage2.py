"""
Stage 2 — build-phase launch specs.

Turns the answers captured in the frontend wizards into a concrete, runnable
launch plan for the two build skills:
  • pixel-clone      — running site → pixel-perfect React+Tailwind rebuild
  • legacy-transform — migration bundle → modern rewrite on the live legacy DB

We do NOT execute the skills here (they need Claude's judgment loop, a browser,
the live app, etc.). We produce the exact spec + commands a run would use, so the
answers collected in the UI map 1:1 to how the skill is actually invoked.
"""
from __future__ import annotations

VIEWPORT_MAP = {
    "Desktop only (1280×800)": "1280x800",
    "Desktop + Mobile": "1280x800,375x812",
    "Custom": "1280x800",
}
THRESHOLD_MAP = {
    "Pixel-perfect (<0.5%)": "0.5",
    "Tight (≤1%)": "1.0",
    "Visual-approx (≤3%)": "3.0",
}


def build_pixel(a: dict) -> dict:
    url = (a.get("site_url") or "http://localhost:3000").strip()
    arch = a.get("target_arch", "React + Tailwind")
    routes = a.get("routes", "All routes (crawl)")
    vp = VIEWPORT_MAP.get(a.get("viewports", "Desktop only (1280×800)"), "1280x800")
    thr = THRESHOLD_MAP.get(a.get("threshold", "Pixel-perfect (<0.5%)"), "0.5")
    dynamic = a.get("dynamic", "Mask dynamic regions")
    login = dynamic == "Login required"

    crawl = "--crawl --depth 2" if routes == "All routes (crawl)" else ""
    mask = '--mask ".carousel,[data-dynamic],time"' if dynamic == "Mask dynamic regions" else ""
    capture = (
        f"python3 scripts/{'auth_capture' if login else 'capture'}.py "
        f"--base {url} --out captures/orig --viewports {vp} {crawl} --full-page {mask}"
    ).strip()

    commands = [
        "pip install playwright && playwright install chromium",
        capture,
        f"# scaffold {arch} pages from captured routes",
        f"# screenshot-diff loop until every page < {thr}% diff",
    ]
    notes = [
        f"Ground truth = the running site at {url}; output re-authored in {arch}.",
        f"Stop line: {thr}% pixel diff per page (loop runs until met).",
    ]
    if routes == "Named list":
        notes.append("Routes: hand-listed — provide routes.txt.")
    if login:
        notes.append("Auth-gated: login selectors captured; credentials supplied at runtime (never stored here).")
    if a.get("assets") == "Placeholder (brand constraints)":
        notes.append("Assets: placeholdered (brand/licensing constraints).")
    return {"skill": "pixel-clone", "summary": f"{arch} · {routes} · {vp} · <{thr}%",
            "commands": commands, "notes": notes, "answers": a}


def build_legacy(a: dict) -> dict:
    bundle = (a.get("bundle") or "migration-bundle/").strip()
    stack = a.get("target_stack", "React + Django/DRF")
    fidelity = a.get("ui_fidelity", "Mirror legacy")
    parity = a.get("parity_gate", "Enforce data parity (recommended)")
    micro = a.get("microservices", "None")

    commands = [
        "python3 scripts/schema_diff.py --json app/artifacts/schema_diff.json --md app/artifacts/schema_diff.md",
        "python3 scripts/gen_models.py  # schema.sql → managed=False ORM models",
        "# scaffold DRF data bridge: /api/legacy/_meta/ + /api/legacy/<slug>/",
        f"# generate {stack} UI ({'mirror legacy' if fidelity.startswith('Mirror') else 'modern redesign'})",
    ]
    if parity.startswith("Enforce"):
        commands.append("python3 scripts/data_parity.py  # prove new API rows == raw legacy SQL")
    notes = [
        "Builds parallel to the legacy app on the SAME live database (no data migration).",
        "Domain models managed=False; framework auth in a separate DB (two-DB router).",
        f"UI fidelity: {fidelity}.",
    ]
    if parity.startswith("Enforce"):
        notes.append("Parity gate ON: every page must match a raw SQL read (count + PK + fields).")
    if micro != "None":
        notes.append("Microservices: heavy modules extracted (user-confirmed).")
    return {"skill": "legacy-transform", "summary": f"{stack} · {fidelity} · {parity.split(' ')[0]}",
            "commands": commands, "notes": notes, "answers": a, "bundle": bundle}


def build(kind: str, answers: dict) -> dict:
    if kind == "pixel":
        return build_pixel(answers or {})
    if kind == "legacy":
        return build_legacy(answers or {})
    raise ValueError("unknown build kind")
