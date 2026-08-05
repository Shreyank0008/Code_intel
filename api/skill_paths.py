"""Resolve the helper scripts the Stage-2 agents shell out to.

These agents (Revive, Pixel-Clone, Legacy Transform) don't do the heavy lifting
inline -- they run standalone scripts as subprocesses. Those scripts used to be
looked up ONLY at ``~/.claude/skills/<bundle>/scripts/<name>``, which made the
whole feature depend on an untracked directory outside the repo: on any machine
where it was absent every Stage-2 agent died with "<x> scripts not found" and
nothing in the repo hinted at why.

Resolution order (first hit wins):
  1. $CODEINTEL_SKILLS_DIR   -- explicit override
  2. <repo>/skills           -- vendored, travels with a clone (the default)
  3. ~/.claude/skills        -- the original location, kept as a fallback so an
                               existing machine-local bundle still overrides
                               nothing but still works if the repo copy is gone

``skill_script()`` always returns a Path -- the repo-local candidate when
nothing exists -- so callers keep their ``.exists()`` checks and their
missing-script messages stay meaningful.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_SKILLS = Path(__file__).resolve().parent.parent / "skills"
HOME_SKILLS = Path.home() / ".claude" / "skills"


def skill_roots() -> list[Path]:
    roots = []
    env = os.environ.get("CODEINTEL_SKILLS_DIR", "").strip()
    if env:
        roots.append(Path(env).expanduser())
    roots += [REPO_SKILLS, HOME_SKILLS]
    return roots


def skill_script(relpath: str) -> Path:
    """Resolve e.g. "pixel-clone/scripts/capture.py" to a concrete Path.

    Returns the first root where the file exists; falls back to the repo-local
    candidate when it exists nowhere, so error messages point at the place the
    file is *supposed* to live.
    """
    for root in skill_roots():
        p = root / relpath
        if p.exists():
            return p
    return REPO_SKILLS / relpath


def missing_hint(relpath: str) -> str:
    """Human-readable 'where should this be' for a script that didn't resolve."""
    return (f"{relpath} not found -- looked in: "
            + ", ".join(str(r) for r in skill_roots()))
