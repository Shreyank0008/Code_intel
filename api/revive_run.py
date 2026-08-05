"""
On-demand revive — boot the ingested app from anywhere (e.g. the Pixel-Clone
page), not just as a stage of the ingest job. Runs revive.revive() on a daemon
thread; state + logs live in job['revive_result'] / job['_revive_logs'] so the
frontend can poll and then auto-fill the live URL for capture.
"""
from __future__ import annotations
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from . import jobs, revive as revive_mod


def _now():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def start(jid: str) -> dict:
    job = jobs.get_job(jid)
    if not job:
        return {"error": "not found"}
    rr = job.get("revive_result") or {}
    if rr.get("status") == "running" and rr.get("url"):
        return {"status": "running", "url": rr["url"]}   # already booted
    job["_revive_logs"] = []
    job["revive_result"] = {"status": "booting", "started_at": datetime.now(timezone.utc).isoformat()}
    threading.Thread(target=_run, args=(jid,), daemon=True).start()
    return {"status": "booting"}


def _run(jid: str):
    job = jobs.get_job(jid)
    logs = job.setdefault("_revive_logs", [])

    def log(level, msg, lane=None):
        entry = {"t": _now(), "level": level, "msg": msg}
        if lane:
            entry["lane"] = lane
        logs.append(entry)

    try:
        root = jobs.ensure_source_dir(job, log)
        if not root:
            job["revive_result"] = {"status": "failed",
                                    "reason": "source not available locally and could not be re-cloned — re-ingest as a local path"}
            return
        endpoints = (job.get("metrics") or {}).get("artifacts", {}).get("endpoints", [])
        # Agentic revive (recon + self-heal via run-any-codebase + LLM) when a
        # provider is configured; else the plain deterministic boot.
        from . import providers, revive_agent
        if providers.active_provider() != "heuristic":
            log("info", "using the Revive Agent (run-any-codebase + LLM self-heal)")
            r = revive_agent.run(job, log)
        else:
            r = revive_mod.revive(Path(root), endpoints, log)
        r["logs"] = logs
        job["revive_result"] = r
        _notify(job, r)
    except Exception as e:
        job["revive_result"] = {"status": "failed", "reason": f"{type(e).__name__}: {e}"}
        _notify(job, job["revive_result"])
    finally:
        jobs._persist()


def _notify(job, r):
    """Post the revive outcome to the assistant — and ask a question if it can't proceed."""
    from . import chat
    st = r.get("status")
    if st == "running":
        chat.push(job, "ok", f"✓ App is live at {r.get('url')} — the Pixel-Clone URL is filled in. "
                             f"{r.get('pages_working','?')}/{r.get('pages_total','?')} pages responded. "
                             "Want me to run the Pixel-Clone agent?", actions=["rerun_pixel"])
    elif st == "running_no_http":
        chat.push(job, "warn", f"Booted, but {r.get('reason','no web endpoint responded')} "
                               "If this stack is backend/DB-only there's nothing to clone — "
                               "Legacy Transform is the right tool. Open it?", actions=["open_legacy"])
    elif st == "skipped":
        chat.push(job, "info", f"Revive skipped: {r.get('reason','boot disabled')}.")
    else:
        chat.push(job, "err", f"Boot failed: {r.get('reason','unknown error')}. Want me to retry?",
                  actions=["retry_boot"])


def status(jid: str) -> dict:
    job = jobs.get_job(jid)
    if not job:
        return {"status": "unknown"}
    rr = job.get("revive_result") or {}
    elapsed = rr.get("elapsed_seconds")
    if elapsed is None and rr.get("started_at"):
        try:
            started = datetime.fromisoformat(rr["started_at"])
            elapsed = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
        except ValueError:
            elapsed = None
    return {
        "status": rr.get("status", "idle"),
        "url": rr.get("url"),
        "reason": rr.get("reason"),
        "containers": rr.get("containers"),
        "fixes": rr.get("fixes", []),
        "containerized": rr.get("containerized", False),
        "logs": job.get("_revive_logs", []),
        "started_at": rr.get("started_at"),
        "elapsed_seconds": elapsed,
        "attempts": rr.get("attempts"),
        "max_attempts": rr.get("max_attempts"),
        # Live values (mutated in place on `job` while the run is still in
        # progress) take priority over the final result snapshot, so the
        # scope card, lane board, and token counter all update during
        # "booting" too, not just once the run has finished.
        "scope": job.get("_revive_scope") or rr.get("scope"),
        "lanes": job.get("_revive_lanes") or rr.get("lanes"),
        "usage": job.get("_revive_usage") or rr.get("usage"),
    }
