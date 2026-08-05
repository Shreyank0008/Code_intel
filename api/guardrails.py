"""
Guardrails for Code Intelligence.

Two classes of guardrail:
  1. Ingest guardrails  — sandbox which filesystem paths may be scanned, cap
     scan size, and refuse anything that smells like path traversal or a
     sensitive system directory.
  2. AI guardrails       — validate / clamp anything an LLM returns before it is
     allowed to influence the UCP sizing, so a hallucinated rating can never
     push the numbers out of range.
"""
from __future__ import annotations
import os
from pathlib import Path
from django.conf import settings


class GuardrailError(Exception):
    """Raised when an input violates a guardrail. Surfaced to the user verbatim."""


# ---------------------------------------------------------------- ingest ----

_BLOCKED_SEGMENTS = {".ssh", ".aws", ".gnupg", ".config", "Library", "System"}


def resolve_ingest_path(raw: str) -> Path:
    """
    Turn a user-supplied path into a real, allow-listed directory or raise.

    Blocks: empty input, non-existent paths, files (must be a dir), paths that
    escape every allowed root, and paths that dip into known-sensitive folders.
    """
    if not raw or not raw.strip():
        raise GuardrailError("No path supplied.")
    raw = raw.strip()

    try:
        p = Path(raw).expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError, OSError):
        raise GuardrailError(f"Path does not exist on the server: {raw}")

    if not p.is_dir():
        raise GuardrailError(f"Path is not a directory: {p}")

    roots = [Path(r).resolve() for r in settings.CODEINTEL_ALLOWED_ROOTS]
    if not any(_is_within(p, r) for r in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise GuardrailError(
            f"Path is outside the allowed roots. Allowed: {allowed}"
        )

    for part in p.parts:
        if part in _BLOCKED_SEGMENTS:
            raise GuardrailError(
                f"Refusing to scan a sensitive directory segment: '{part}'"
            )
    return p


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


_GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "git.sr.ht")


def resolve_git_url(raw: str) -> str:
    """
    Validate a public git URL before we shell out to `git clone`. Only https to
    a known code host, no shell metacharacters, no credentials embedded.
    """
    if not raw or not raw.strip():
        raise GuardrailError("No git URL supplied.")
    url = raw.strip()
    if not url.startswith("https://"):
        raise GuardrailError("Only https:// git URLs are allowed (no ssh/git/http).")
    if "@" in url.split("//", 1)[1]:
        raise GuardrailError("Refusing a URL with embedded credentials.")
    if any(c in url for c in ";|&`$<>\n\r \t"):
        raise GuardrailError("URL contains illegal characters.")
    host = url.split("//", 1)[1].split("/", 1)[0].lower()
    if host not in _GIT_HOSTS:
        raise GuardrailError(
            f"Host '{host}' is not an allowed code host. Allowed: {', '.join(_GIT_HOSTS)}"
        )
    return url


def enforce_scan_budget(n_files: int, n_bytes: int) -> None:
    if n_files > settings.CODEINTEL_MAX_FILES:
        raise GuardrailError(
            f"Codebase too large: {n_files} files exceeds the "
            f"{settings.CODEINTEL_MAX_FILES} cap."
        )
    if n_bytes > settings.CODEINTEL_MAX_BYTES:
        raise GuardrailError(
            f"Codebase too large: {n_bytes // (1024*1024)} MB exceeds the "
            f"{settings.CODEINTEL_MAX_BYTES // (1024*1024)} MB cap."
        )


# -------------------------------------------------------------------- AI ----

def clamp_rating(v, lo=0, hi=5, default=0) -> int:
    """Force an LLM-provided factor rating into the legal 0..5 integer range."""
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def clamp_count(v, default=0, hi=100000) -> int:
    """Force an LLM-provided count into a sane non-negative integer."""
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return default
    return max(0, min(hi, v))


def validate_ai_mapping(obj: dict) -> dict:
    """
    Whitelist + clamp every field of an AI-produced UCP mapping. Anything
    missing or malformed is dropped; the caller then fills gaps from the
    deterministic heuristic. This is the wall between the LLM and the maths.
    """
    if not isinstance(obj, dict):
        raise GuardrailError("AI mapping was not a JSON object.")
    out = {}
    ua = obj.get("uaw", {})
    if isinstance(ua, dict):
        out["uaw"] = {k: clamp_count(ua.get(k)) for k in ("simple", "average", "complex")}
    uc = obj.get("uucw", {})
    if isinstance(uc, dict):
        out["uucw"] = {k: clamp_count(uc.get(k)) for k in ("simple", "average", "complex")}
    if isinstance(obj.get("tcf"), list):
        out["tcf"] = [clamp_rating(x) for x in obj["tcf"]][:13]
    if isinstance(obj.get("ecf"), list):
        out["ecf"] = [clamp_rating(x) for x in obj["ecf"]][:8]
    dm = obj.get("dm_band")
    if dm in ("Small", "Medium", "Large"):
        out["dm_band"] = dm
    if isinstance(obj.get("rationale"), str):
        out["rationale"] = obj["rationale"][:2000]
    return out
