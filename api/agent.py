"""
AI mapping agent: turns raw scan metrics into UCP inputs (actor counts,
use-case counts, TCF/ECF factor ratings, migration band) *with reasoning*.

Two execution paths, chosen at runtime:
  • LLM path  — if an Anthropic API key + SDK are present, ask Claude to reason
    about the metrics and return a strict JSON mapping. Its output is passed
    through guardrails.validate_ai_mapping() before it is trusted.
  • Heuristic path (default / fallback) — a deterministic rule set derived from
    the same metrics. Always available, no network, fully explainable.

Whatever the LLM omits or gets clamped, the heuristic backfills — so the maths
is never left with a hole and never accepts an out-of-range value.
"""
from __future__ import annotations
import json
from . import guardrails, providers

# 13 TCF weights, 8 ECF weights (RFE Annexure 11)
TCF_W = [2, 1, 1, 1, 1, 0.5, 0.5, 2, 1, 1, 1, 1, 1]
ECF_W = [1.5, 0.5, 1, 0.5, 1, 2, -1, -1]


def map_metrics(metrics: dict, use_llm: bool, log) -> dict:
    heuristic = _heuristic(metrics)
    if not use_llm:
        log("info", "AI stage: heuristic mapping (LLM stages disabled)")
        heuristic["ai_mode"] = "heuristic (disabled)"
        return heuristic

    prov = providers.active_provider()
    if prov == "heuristic":
        log("warn", "AI stage: no provider configured in Settings — heuristic fallback")
        heuristic["ai_mode"] = "heuristic (no provider)"
        return heuristic

    try:
        log("info", f"AI stage: mapping via provider '{prov}'…")
        raw_text = providers.call_llm(_prompt(metrics), log)["text"]
        raw = _extract_json(raw_text)
        safe = guardrails.validate_ai_mapping(raw)     # <-- the guardrail wall
        merged = {**heuristic, **safe}                 # gap-filler: heuristic backfills gaps
        filled = [k for k in ("uaw", "uucw", "tcf", "ecf", "dm_band") if k not in safe]
        merged["ai_mode"] = f"{prov} + guardrails"
        if filled:
            log("m", "gap-filler backfilled: " + ", ".join(filled))
        if safe.get("rationale"):
            log("ok", "AI rationale: " + safe["rationale"][:180])
        log("ok", "AI mapping accepted after guardrail validation ✓")
        return merged
    except Exception as e:                              # never let the agent break the job
        log("err", f"AI stage failed ({type(e).__name__}: {e}) — heuristic fallback")
        heuristic["ai_mode"] = f"heuristic ({prov} error)"
        return heuristic


# ------------------------------------------------------------- heuristic ----

def _heuristic(m: dict) -> dict:
    endpoints = m.get("endpoints", 0)
    pages = m.get("pages", 0)
    models = m.get("models", 0)
    integrations = m.get("integrations", [])
    auth = m.get("auth", [])
    loc = m.get("total_loc", 0)

    # Actors: human roles (complex), protocol systems (average), APIs (simple)
    human_roles = 4 if pages >= 8 else 3 if pages >= 3 else 2
    protocol = min(4, 1 + sum(x in " ".join(integrations) for x in ("LDAP/AD",)) + 2)  # DB + LDAP + SMTP-ish
    apis = min(4, 1 + sum(1 for i in integrations if i in ("MS Graph", "OAuth", "AWS", "Payment")))
    uaw = {"simple": max(1, apis), "average": max(1, protocol), "complex": max(1, human_roles)}

    # Use cases: UI pages OR, for backend-only repos, derived from API surface.
    api_views = m.get("api_views", 0)
    uc = max(pages, api_views, round(endpoints / 5), 1)
    dens = endpoints / uc if uc else 0
    if dens >= 8:      complex_uc, avg_uc, simple_uc = round(uc*0.45), round(uc*0.4), max(1, uc - round(uc*0.85))
    elif dens >= 4:    complex_uc, avg_uc, simple_uc = round(uc*0.35), round(uc*0.45), max(1, uc - round(uc*0.8))
    else:              complex_uc, avg_uc, simple_uc = round(uc*0.25), round(uc*0.45), max(1, uc - round(uc*0.7))
    uucw = {"simple": max(1, simple_uc), "average": max(1, avg_uc), "complex": max(0, complex_uc)}

    # TCF ratings 0..5
    tcf = [
        4 if loc > 5000 else 3,                 # T1 distributed
        3, 4, 4, 3, 3, 3, 3, 3, 3,              # T2..T10
        5 if any(a in auth for a in ("RBAC", "LDAP", "JWT", "MFA/2FA")) else 3,  # T11 security
        4 if integrations else 2,               # T12 third-party access
        2,                                      # T13 training
    ]
    # ECF ratings 0..5
    modern = 4 if models else 3
    volatile = 2 if m.get("legacy") else 3      # legacy => less stable requirements
    ecf = [3, 3, modern, 3, 4, volatile, 1, 2]

    dm_band = _dm_band(m)

    return {
        "uaw": uaw, "uucw": uucw, "tcf": tcf, "ecf": ecf, "dm_band": dm_band,
        "rationale": (
            f"{pages} UI pages → {human_roles} human actor roles; "
            f"{endpoints} endpoints over ~{uc} use cases (density {dens:.1f}); "
            f"security-critical auth ({', '.join(auth) or 'none'}) sets TCF T11; "
            f"migration workload → {dm_band}."
        ),
    }


def _dm_band(m: dict) -> str:
    models = m.get("models", 0)
    legacy = m.get("legacy", [])
    codes = [1]
    if models > 200: codes.append(3)
    elif models > 50: codes.append(2)
    else: codes.append(1)
    if legacy:  # a real migration is underway
        codes.append(2)
    if any("MySQL" in s for s in legacy) and models > 100:
        codes.append(3)
    mx = max(codes)
    return {1: "Small", 2: "Medium", 3: "Large"}[mx]


# ---------------------------------------------------------------- LLM ----

def _prompt(metrics: dict) -> str:
    from . import agents_registry
    instruction = agents_registry.get_prompt(
        "mapping", agents_registry.DEFAULT_MAPPING_PROMPT)
    return instruction + "\n\nMETRICS:\n" + json.dumps(metrics, indent=2)


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in LLM response")
    return json.loads(text[start:end + 1])
