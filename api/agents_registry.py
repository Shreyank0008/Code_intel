"""
Agent registry + editable system prompts.

Static definitions live here; per-agent prompt overrides persist to
agents_store.json. The Mapping Agent's prompt is the one that actually drives
the LLM call (see agent._prompt), so editing it here changes real behaviour.
Deterministic / static / planned agents have no prompt — their 'rules' are code.
"""
from __future__ import annotations
import json
from pathlib import Path
from django.conf import settings

_PATH = Path(settings.BASE_DIR) / "agents_store.json"

DEFAULT_PIXEL_PROMPT = (
    "You are a senior front-end engineer performing a pixel-perfect rebuild. "
    "You are given the page's ACTUAL rendered DOM (real markup the server sent, "
    "not a guess) and the app's extracted design tokens (real colours/fonts/spacing "
    "measured off the live page). Re-author it as ONE self-contained React "
    "functional component using Tailwind CSS. Re-authoring means: SAME structure, "
    "text and visual result, but EVERY className must be a real Tailwind utility "
    "class (or Tailwind arbitrary-value syntax) — never the source's own "
    "framework classes.\n"
    "\n"
    "className RULE (breaks the page if skipped): do NOT copy the DOM's own CSS "
    "class names verbatim — e.g. `navbar navbar-dark`, `container-fluid`, "
    "`col-md-12`, `nav-link`, `row` are Bootstrap classes with NO meaning in "
    "Tailwind and will render completely unstyled (no colours, no spacing, no "
    "layout) if you emit them as-is. Instead translate what each class visually "
    "DOES into Tailwind utilities using the ACTUAL colour/spacing values from "
    "the DESIGN TOKENS block below (never invent a colour) — e.g. a dark navbar "
    "becomes `flex items-center gap-4 px-4 bg-[<token colour>]`; `col-md-12`/"
    "`col-12` becomes `w-full`; `container`/`container-fluid` becomes "
    "`max-w-6xl mx-auto px-4`; `row` becomes `flex flex-wrap`; `text-center` "
    "becomes `text-center` (Tailwind has that one natively). If in doubt "
    "whether a class is a real Tailwind utility, don't emit it verbatim — "
    "re-derive the visual effect instead.\n"
    "\n"
    "Beyond that, fidelity rules in priority order:\n"
    "1. STRUCTURE: preserve the DOM's element order, nesting, and every visible "
    "text string verbatim (nav labels, headings, button text, in the same order). "
    "Do not drop, reorder, invent, or paraphrase content that's in the DOM.\n"
    "2. COLOUR: use the EXACT colours from the DESIGN TOKENS block as Tailwind "
    "arbitrary-value classes (e.g. bg-[#123456]) rather than the closest named "
    "Tailwind colour (e.g. bg-gray-800), whenever a token value is available for "
    "that element. Only fall back to a named colour when no token covers it. If "
    "the tokens include a `navBackground` value, that is specifically the real "
    "nav/header background colour — use it for the nav/header element, don't "
    "guess a generic dark colour (e.g. plain black) instead.\n"
    "3. ICONS: if the DOM has icon markup (e.g. <span class=\"fa fa-home\">, "
    "font-icon spans, or an icon font family in the tokens), reproduce that "
    "specific icon literally as <i className=\"fa fa-home\"> etc. — the preview "
    "environment loads Font Awesome, so these render. Never substitute an icon "
    "for an emoji or drop it.\n"
    "4. LAYOUT PROPORTIONS: infer intent from the source's grid classes (see the "
    "className rule above for how to translate them) rather than copying their "
    "names — full-width columns span the container, centered wrappers center "
    "their content, a fixed-width logo/image keeps roughly its rendered width.\n"
    "5. IMAGES: keep every <img> src and alt attribute byte-for-byte from the DOM "
    "(they get rewritten to the live origin automatically) — never invent a "
    "placeholder image path.\n"
    "6. BACKGROUND: always set an explicit background colour class (e.g. bg-white, "
    "or the page's actual background token if one was extracted) on the outermost "
    "wrapping element. Never leave the page background unset — some preview "
    "environments render an unset background as black instead of the page's "
    "true (usually white) canvas.\n"
    "Use semantic HTML. Output ONLY the component code (a single default-exported "
    "component) — no prose, no markdown fences, no explanation."
)

DEFAULT_MAPPING_PROMPT = (
    "You are a software-sizing analyst applying the Use Case Point (UCP) "
    "method (RFE Annexure 11). Given these static-analysis metrics of a "
    "codebase, return ONLY a JSON object with keys: uaw{simple,average,"
    "complex}, uucw{simple,average,complex}, tcf (list of 13 ints 0-5), "
    "ecf (list of 8 ints 0-5), dm_band ('Small'|'Medium'|'Large'), "
    "rationale (short string). No prose outside JSON."
)

AGENTS = [
    {
        "id": "mapping", "name": "Mapping Agent", "icon": "🧭", "type": "llm",
        "tagline": "The core sizing brain",
        "description": "Reads the static-scan metrics and classifies actors, use "
                       "cases and TCF/ECF factor ratings into UCP inputs.",
        "io": "Input: scan metrics · Output: UCP mapping (guardrailed)",
        "editable": True,
        "default_prompt": DEFAULT_MAPPING_PROMPT,
        "details": [
            "Runs once per job during the 'AI mapping' stage.",
            "Its JSON output is validated + clamped by the Guardrail before use.",
            "Any missing field is backfilled by the Gap-Filler agent.",
            "The prompt below is the editable instruction — the scan-metrics JSON "
            "is appended automatically at call time.",
        ],
    },
    {
        "id": "gapfiller", "name": "Gap-Filler Agent", "icon": "🧩",
        "type": "deterministic", "tagline": "No hole in the maths",
        "description": "When the LLM omits or the guardrail rejects a field, "
                       "backfills it from the deterministic heuristic.",
        "io": "Backfills: uaw · uucw · tcf · ecf · dm_band",
        "editable": False,
        "details": [
            "Deterministic — derived from the same scan metrics, no LLM.",
            "Merges as {**heuristic, **validated_llm}: LLM wins where valid.",
            "Logs exactly which fields it backfilled each run.",
            "Guarantees the UCP computation always has every input.",
        ],
    },
    {
        "id": "guardrail", "name": "Guardrail Validator", "icon": "🛡️",
        "type": "deterministic", "tagline": "Wall between model and numbers",
        "description": "Whitelists and clamps every value the LLM returns before "
                       "it can influence pricing.",
        "io": "Enforces: ratings 0–5 · counts ≥ 0 · bands ∈ {Small,Medium,Large}",
        "editable": False,
        "details": [
            "Drops unknown keys; coerces types; clamps ranges.",
            "A hallucinated rating can never push UCP out of range.",
            "Also guards ingest: path sandbox + git-URL allow-list.",
            "Implemented in guardrails.validate_ai_mapping().",
        ],
    },
    {
        "id": "revive", "name": "Revive Agent", "icon": "♻️", "type": "llm",
        "tagline": "Resurrect the app so it runs",
        "description": "Boots the ingested app using the run-any-codebase skill (recon "
                       "+ gap-scan) and, when the build fails, an LLM diagnoses the error "
                       "and fixes the Dockerfile/compose, then retries.",
        "io": "Input: ingested repo · Output: a LIVE URL handed to the UI-clone step",
        "editable": False,
        "details": [
            "Phases: Recon (recon.sh) → Boot → Self-heal (LLM) → Health → Gap-scan.",
            "Self-heal edits ONLY build/config files on a SANDBOXED COPY — original untouched.",
            "Guardrail — max 2 boot attempts; container-isolated; per-phase timeouts.",
            "Guardrail — LLM may only rewrite Dockerfile/compose/.env inside the sandbox.",
            "Degrades to a plain deterministic boot with no LLM provider.",
            "Uses run-any-codebase scripts: recon.sh, gap_scan.py.",
        ],
    },
    {
        "id": "cataloguer", "name": "Artifact Cataloguer", "icon": "📦",
        "type": "static", "tagline": "Harvest everything the app does",
        "description": "Runs the run-any-codebase extractor to harvest forms, "
                       "buttons, api-calls, SQL, params, session and the call graph.",
        "io": "Skill: extract_artifacts.py · static, no LLM",
        "editable": False,
        "details": [
            "Invokes the actual run-any-codebase skill script (read-only).",
            "Produces the per-file Catalog tab on the Artifacts page.",
            "Bounded: caps per-file lists, ranks busiest files.",
            "Runs in a 120s-timeout subprocess sandbox.",
        ],
    },
    {
        "id": "pixelclone", "name": "Pixel-Clone Agent", "icon": "🎨", "type": "llm",
        "tagline": "Running site → React+Tailwind rebuild",
        "description": "Orchestrates the full pixel-clone pipeline: captures the running "
                       "app, extracts design tokens, then generates a React+Tailwind "
                       "component per page from the DOM + tokens.",
        "io": "Input: live localhost URL · Output: captured screenshots + generated components",
        "editable": True,
        "default_prompt": DEFAULT_PIXEL_PROMPT,
        "details": [
            "Phases: Capture (Playwright) → Design tokens → Route map → LLM code-gen → Report.",
            "Guardrail — localhost-only capture (never an arbitrary external site).",
            "Guardrail — sandboxed output; filenames sanitised; generated code is NEVER executed.",
            "Guardrail — iteration cap (≤6 pages) + LLM-call budget (≤8 calls) per run.",
            "Guardrail — every model output validated (looks like code, size-capped, fences stripped).",
            "Degrades gracefully: with no provider it still captures + tokenises, skipping code-gen.",
        ],
    },
    {
        "id": "reviving", "name": "Reviving Agent", "icon": "♻️", "type": "planned",
        "tagline": "Retry on failure", "editable": True,
        "description": "On a failed / invalid LLM response, re-prompts with the "
                       "error and retries N times before falling back.",
        "io": "Planned — see roadmap",
        "default_prompt": (
            "The previous response was invalid: {error}. Re-read the schema and "
            "return ONLY the corrected JSON object. Do not repeat the mistake."
        ),
        "details": [
            "NOT YET WIRED — shown for transparency.",
            "Will wrap the Mapping Agent in a retry loop (default 2 retries).",
            "Feeds the parse/guardrail error back into the next prompt.",
            "Falls back to the heuristic only after retries are exhausted.",
        ],
    },
    {
        "id": "codegen", "name": "Codegen Agent", "icon": "🚀", "type": "planned",
        "tagline": "Rebuild in the target stack", "editable": True,
        "description": "Generates the rebuilt app in the chosen target stack "
                       "(Frontend · Backend · DB) from the artifacts + sizing.",
        "io": "Planned — downstream of sizing",
        "default_prompt": (
            "You are a modernization engineer. Given the artifact catalog and the "
            "target stack {frontend}/{backend}/{database}, scaffold the equivalent "
            "application preserving every endpoint, form, rule and entity."
        ),
        "details": [
            "NOT YET WIRED — shown for transparency.",
            "Consumes the Artifacts catalog + target-stack wizard selection.",
            "Would emit a scaffolded repo, not just a plan.",
        ],
    },
]

_BY_ID = {a["id"]: a for a in AGENTS}


def _load() -> dict:
    if _PATH.exists():
        try:
            return json.loads(_PATH.read_text())
        except Exception:
            pass
    return {}


def get_all() -> list:
    ov = _load()
    out = []
    for a in AGENTS:
        item = dict(a)
        if a.get("editable"):
            item["system_prompt"] = ov.get(a["id"], {}).get(
                "system_prompt", a.get("default_prompt", ""))
        out.append(item)
    return out


def get_prompt(agent_id: str, default: str) -> str:
    ov = _load().get(agent_id, {})
    p = ov.get("system_prompt")
    return p if p else default


def save_prompt(agent_id: str, prompt: str) -> dict:
    if agent_id not in _BY_ID or not _BY_ID[agent_id].get("editable"):
        raise ValueError("agent not found or not editable")
    ov = _load()
    ov.setdefault(agent_id, {})["system_prompt"] = (prompt or "")[:8000]
    _PATH.write_text(json.dumps(ov, indent=2))
    return {"id": agent_id, "system_prompt": ov[agent_id]["system_prompt"]}


def reset_prompt(agent_id: str) -> dict:
    ov = _load()
    ov.pop(agent_id, None)
    _PATH.write_text(json.dumps(ov, indent=2))
    default = _BY_ID.get(agent_id, {}).get("default_prompt", "")
    return {"id": agent_id, "system_prompt": default}
