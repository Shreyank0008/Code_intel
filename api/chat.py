"""
Agent Chatbot — two-way channel between the agents and the user.

  • push(job, …)   agents post messages/notifications to the user (with optional
                   one-click actions like "Retry boot").
  • send(jid,text) the user chats; we ground the LLM in the job's real data and
                   return a concise reply, optionally PROPOSING a whitelisted action.
  • run_action     executes ONLY a fixed catalog of safe actions, each surfaced to
                   the user as a button they click to confirm.

GUARDRAILS
  • Whitelisted actions only (ACTIONS) — no shell, no file writes, no arbitrary ops.
  • The LLM may only *propose* an action; the user clicks to run it (confirm step).
  • Scoped to the active job; provider-gated (degrades to a rules helper with no key).
  • LLM output is never executed as code — only matched to the catalog by name.
"""
from __future__ import annotations
import json
import re
import threading
from datetime import datetime, timezone
from django.conf import settings as django_settings
from . import jobs, providers

# Agents blocked waiting for a user answer: jid -> {ev, answer}
_PENDING: dict = {}

# --- the ONLY things the assistant can trigger (each needs a user click) ------
ACTIONS = {
    "retry_boot":  {"label": "Retry boot", "desc": "Re-run Revive on this job"},
    "stop_app":    {"label": "Stop app", "desc": "Stop the revived app / clone"},
    "rerun_pixel": {"label": "Re-run Pixel-Clone", "desc": "Run the Pixel-Clone agent again"},
    "open_pixel":  {"label": "Open Pixel-Clone", "desc": "Go to the UI Pixel-Clone page"},
    "open_legacy": {"label": "Open Legacy Transform", "desc": "Go to the Legacy Transform page"},
    "open_settings": {"label": "Open Settings", "desc": "Go to provider / pricing settings"},
}

SYSTEM = (
    "You are the in-app assistant for the Code Modernisation platform. You help the "
    "user understand and fix issues with the CURRENT job using ONLY the data below. "
    "Be concise (2-4 sentences). If the user's request clearly maps to one of these "
    "actions, append a final line exactly like ACTION: <name> where <name> is one of: "
    + ", ".join(ACTIONS) + ". Never invent actions or claim to have done anything — "
    "you only propose; the user clicks to run it."
)


def _now():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def push(job: dict, level: str, text: str, actions=None, choices=None, awaiting=False):
    """Agent → user notification. level: info | ok | warn | err."""
    job.setdefault("messages", []).append({
        "role": "agent", "level": level, "text": text,
        "actions": [a for a in (actions or []) if a in ACTIONS],
        "choices": list(choices or []), "awaiting": bool(awaiting), "t": _now(),
    })
    try:
        jobs._persist()
    except Exception:
        pass


def ask(job: dict, question: str, choices=None, timeout: int = 300, default=None):
    """HUMAN-IN-THE-LOOP: an agent pauses and asks the user, blocking until they
    answer (via a choice button or a chat message) or the timeout elapses.
    Returns the user's answer, or `default` on timeout."""
    jid = job["id"]
    ev = threading.Event()
    _PENDING[jid] = {"ev": ev, "answer": None}
    push(job, "warn", "❓ " + question, choices=choices or [], awaiting=True)
    ev.wait(timeout)
    p = _PENDING.pop(jid, {})
    ans = p.get("answer")
    if ans is None:
        push(job, "info", f"(no response in time — proceeding with: {default})")
        return default
    return ans


def answer(jid: str, text: str) -> dict:
    """Resolve a pending ask() with the user's answer."""
    job = jobs.get_job(jid)
    if job:
        job.setdefault("messages", []).append({"role": "user", "text": text, "t": _now()})
    p = _PENDING.get(jid)
    if p:
        p["answer"] = text
        p["ev"].set()
    try:
        jobs._persist()
    except Exception:
        pass
    return {"ok": bool(p)}


def history(jid: str):
    job = jobs.get_job(jid)
    return job.get("messages", []) if job else []


def _context(job: dict) -> str:
    r = job.get("result") or {}
    rev = job.get("revive_result") or {}
    s2 = job.get("stage2") or {}
    a = (job.get("metrics") or {}).get("artifacts", {})
    logs = [l["msg"] for l in job.get("_logs", [])[-6:]]
    ctx = {
        "source": job.get("source"), "status": job.get("status"),
        "ucp": r.get("ucp"), "band": r.get("band"), "migration": r.get("dm_band"),
        "endpoints": len(a.get("endpoints", [])), "ui_api_only": a.get("ui", {}).get("api_only"),
        "revive_status": rev.get("status"), "revive_url": rev.get("url"),
        "revive_reason": rev.get("reason"),
        "pixel": {k: {"status": (v or {}).get("status"),
                      "clone_url": (v or {}).get("result", {}).get("clone_url")}
                  for k, v in s2.items()},
        "recent_logs": logs, "error": job.get("error"),
    }
    return json.dumps(ctx, indent=2, default=str)


DEMO_NOTICE = "🔒 The Agent Assistant is not available in this Demo version."


def send(jid: str, text: str) -> dict:
    job = jobs.get_job(jid)
    if not job:
        return {"error": "job not found"}
    # Demo mode: the whole agent (free-text, slash-commands, pending-answer
    # resolution) is off. Checked before anything else so a pending ask()
    # never gets a real answer either — it just rides out its own timeout.
    if django_settings.CODEINTEL_DEMO_MODE:
        return {"role": "assistant", "text": DEMO_NOTICE, "t": _now(), "actions": []}
    # If an agent is waiting for input, this message IS the answer.
    if jid in _PENDING:
        answer(jid, text)
        return {"role": "assistant", "text": "Got it — continuing.", "t": _now(), "actions": []}
    # Slash-commands: run a pipeline stage directly, order-enforced.
    if text.strip().startswith("/"):
        return _command(job, text.strip())
    job.setdefault("messages", []).append({"role": "user", "text": text, "t": _now()})

    prov = providers.active_provider()
    if prov == "heuristic":
        reply, action = _rules_reply(job, text)
    else:
        try:
            prompt = f"{SYSTEM}\n\nJOB DATA:\n{_context(job)}\n\nUSER: {text}"
            raw = providers.call_llm(prompt, lambda l, m: None, json_mode=False)["text"]
            action = None
            m = re.search(r"ACTION:\s*(\w+)", raw)
            if m and m.group(1) in ACTIONS:
                action = m.group(1)
            reply = re.sub(r"\n?ACTION:\s*\w+\s*$", "", raw).strip()
        except Exception as e:
            reply, action = f"(assistant unavailable: {type(e).__name__}) — falling back.", None
            r2, action = _rules_reply(job, text)
            reply = r2

    msg = {"role": "assistant", "text": reply, "t": _now(),
           "actions": [action] if action else []}
    job["messages"].append(msg)
    try:
        jobs._persist()
    except Exception:
        pass
    return msg


# Slash-command → pipeline stage. Order is HARD-LOCKED by prerequisites.
COMMANDS = {
    "/help": "help", "/?": "help",
    "/run-any-codebase": "revive", "/revive": "revive",
    "/pixel-clone": "pixel", "/ui-clone": "pixel", "/pixel": "pixel",
    "/legacy-transform": "legacy", "/legacy": "legacy",
    "/artifacts": "artifacts", "/ucp": "ucp",
}


def _command(job: dict, text: str) -> dict:
    jid = job["id"]
    cmd = text.split()[0].lower()

    def reply(t, goto=None, actions=None):
        job.setdefault("messages", []).append({"role": "user", "text": text, "t": _now()})
        m = {"role": "assistant", "text": t, "t": _now(), "actions": actions or [], "goto": goto}
        job["messages"].append(m)
        try:
            jobs._persist()
        except Exception:
            pass
        return m

    target = COMMANDS.get(cmd)
    if target == "help" or target is None:
        listing = ("Commands (order-enforced):\n"
                   "`/revive` (a.k.a. `/run-any-codebase`) — boot the app (agentic)\n"
                   "`/pixel-clone` — clone the running UI\n"
                   "`/legacy-transform` — rewrite on the live DB\n"
                   "`/artifacts` · `/ucp` — jump to a page")
        prefix = "" if target == "help" else f"Unknown command `{cmd}`.\n\n"
        return reply(prefix + listing)

    # HARD-LOCK — Stage-2 skills require a finalised job.
    if target in ("revive", "pixel", "legacy") and not job.get("finalised"):
        return reply("🔒 Finalise **UCP & Pricing** first — that unlocks the Stage-2 skills.",
                     goto="result")
    if target == "revive":
        from . import revive_run
        revive_run.start(jid)
        return reply("♻️ Running **/run-any-codebase** (Revive Agent) — booting the app now. "
                     "I'll ask here if I need a decision.", goto="revive")
    if target == "pixel":
        if (job.get("revive_result") or {}).get("status") != "running":
            return reply("🔒 The app must be **running** first. Run `/revive` to boot it.", goto="revive")
        return reply("🎨 Opening **UI Pixel-Clone** — the live URL is filled in; press *Run*.", goto="pixel")
    if target == "legacy":
        return reply("🏗️ Opening **Legacy Transform**.", goto="legacy")
    if target == "artifacts":
        return reply("📦 Opening **Artifacts**.", goto="artifacts")
    if target == "ucp":
        return reply("📐 Opening **UCP & Pricing**.", goto="result")
    return reply("(no-op)")


def _rules_reply(job, text):
    """No-LLM fallback — canned but useful, still proposes real actions."""
    t = text.lower()
    rev = job.get("revive_result") or {}
    r = job.get("result") or {}
    if any(w in t for w in ("boot", "revive", "run the app", "start")):
        return ("I can boot the ingested app in Docker for you.", "retry_boot")
    if "stop" in t:
        return ("I can stop the running app / clone.", "stop_app")
    if any(w in t for w in ("clone", "pixel", "component")):
        return ("I can re-run the Pixel-Clone agent (needs the app running + an LLM key).", "rerun_pixel")
    if any(w in t for w in ("key", "provider", "settings", "pricing")):
        return ("Configure your provider key and pricing in Settings.", "open_settings")
    if "ucp" in t or "size" in t:
        return (f"This job sized to UCP {r.get('ucp','—')} ({r.get('band','—')}). "
                "UCP = UUCP × TCF × ECF; it's low when few endpoints/pages are detected.", None)
    if rev.get("status") == "failed":
        return (f"The last boot failed: {rev.get('reason','unknown')}. Want me to retry?", "retry_boot")
    return ("I'm the platform assistant. I can boot the app, stop it, re-run the "
            "Pixel-Clone agent, or explain the sizing. (Add an LLM key in Settings for full chat.)", None)


def run_action(jid: str, action: str) -> dict:
    if action not in ACTIONS:
        return {"error": "unknown action"}
    job = jobs.get_job(jid)
    if not job:
        return {"error": "job not found"}
    if action == "retry_boot":
        from . import revive_run
        revive_run.start(jid)
        push(job, "info", "Booting the app… watch the Pixel-Clone page for the live URL.")
        return {"ok": True, "did": "boot started"}
    if action == "stop_app":
        from . import revive_run, pixel_agent
        rr = job.get("revive_result") or {}
        if rr.get("compose") and rr.get("cwd"):
            revive_run  # noqa
            from . import revive as revive_mod
            revive_mod.stop(rr["compose"], rr["cwd"])
        pixel_agent.stop_clone(jid)
        push(job, "ok", "Stopped the running app and clone.")
        return {"ok": True, "did": "stopped"}
    if action in ("rerun_pixel", "open_pixel"):
        return {"ok": True, "did": "navigate", "goto": "pixel"}
    if action == "open_legacy":
        return {"ok": True, "did": "navigate", "goto": "legacy"}
    if action == "open_settings":
        return {"ok": True, "did": "navigate", "goto": "settings"}
    return {"error": "no-op"}
