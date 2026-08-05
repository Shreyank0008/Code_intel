"""
Local secrets store for provider API keys.

Keys are written to secrets.json with 0600 permissions and NEVER returned to the
frontend in full — reads are masked (last 4 chars only). This file is
git-ignored. This is a dev-grade store (plaintext on disk, owner-only); for
production put these in a real secret manager / KMS.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from django.conf import settings

_PATH = Path(settings.BASE_DIR) / "secrets.json"

_DEFAULT = {
    "provider": "heuristic",  # heuristic | openai | bedrock | anthropic
    "openai": {"api_key": "", "model": "gpt-4o"},
    "bedrock": {
        "access_key_id": "", "secret_access_key": "", "region": "us-east-1",
        "model": "anthropic.claude-3-5-sonnet-20240620-v1:0",
    },
    "anthropic": {"api_key": "", "model": "claude-sonnet-5"},
    # Pricing rates applied to UCP points / migration TB (RFE bid bands).
    "pricing": {
        "currency": "₹",
        "ucp": {"Small": 0, "Medium": 0, "Large": 0},   # cost per UCP point
        "tb":  {"Small": 0, "Medium": 0, "Large": 0},   # cost per migration TB
    },
}

_SECRET_FIELDS = {
    "openai": ["api_key"],
    "bedrock": ["access_key_id", "secret_access_key"],
    "anthropic": ["api_key"],
}


def _num(v):
    try:
        return max(0, float(v))
    except (TypeError, ValueError):
        return 0


def _read_raw() -> dict:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text())
            # merge onto defaults so new keys appear
            out = json.loads(json.dumps(_DEFAULT))
            for k, v in data.items():
                if isinstance(v, dict) and k in out:
                    out[k].update(v)
                else:
                    out[k] = v
            return out
        except Exception:
            pass
    return json.loads(json.dumps(_DEFAULT))


def load() -> dict:
    """Full config WITH secrets — server-side use only (providers)."""
    return _read_raw()


def _mask(val: str) -> str:
    if not val:
        return ""
    return "••••••••" + val[-4:] if len(val) > 4 else "••••"


def masked() -> dict:
    """Config safe to send to the browser: secrets replaced with masks + flags."""
    raw = _read_raw()
    out = json.loads(json.dumps(raw))
    for prov, fields in _SECRET_FIELDS.items():
        for f in fields:
            v = raw.get(prov, {}).get(f, "")
            out[prov][f] = _mask(v)
            out[prov][f + "_set"] = bool(v)
    return out


def save(patch: dict) -> dict:
    """
    Merge a patch from the UI. A secret field that arrives empty or still masked
    (starts with the bullet char) is treated as 'unchanged' — so re-saving the
    form without retyping a key does not wipe it.
    """
    cur = _read_raw()
    if "provider" in patch and patch["provider"] in (
        "heuristic", "openai", "bedrock", "anthropic"
    ):
        cur["provider"] = patch["provider"]
    for prov in ("openai", "bedrock", "anthropic"):
        incoming = patch.get(prov)
        if not isinstance(incoming, dict):
            continue
        for f, v in incoming.items():
            if f in _SECRET_FIELDS.get(prov, []):
                if not v or str(v).startswith("•"):
                    continue  # keep existing secret
            cur.setdefault(prov, {})[f] = v
    # Pricing (non-secret). Currency is free text; band rates are clamped >= 0.
    pr = patch.get("pricing")
    if isinstance(pr, dict):
        dst = cur.setdefault("pricing", json.loads(json.dumps(_DEFAULT["pricing"])))
        if isinstance(pr.get("currency"), str) and pr["currency"].strip():
            dst["currency"] = pr["currency"].strip()[:4]
        for kind in ("ucp", "tb"):
            band_in = pr.get(kind)
            if isinstance(band_in, dict):
                for band in ("Small", "Medium", "Large"):
                    if band in band_in:
                        dst.setdefault(kind, {})[band] = _num(band_in[band])
    _PATH.write_text(json.dumps(cur, indent=2))
    try:
        os.chmod(_PATH, 0o600)
    except OSError:
        pass
    return masked()
