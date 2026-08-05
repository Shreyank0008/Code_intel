"""
Job store + background runner.

A job is one ingest→analyze→size pipeline. It runs on a daemon thread; its log
lines accumulate in an in-memory buffer that the frontend tails by cursor. Job
metadata + results persist to a JSON file so they survive a reload (logs are
kept in memory only — a finished job replays its stored logs).
"""
from __future__ import annotations
import json
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from . import analyzer, agent, ucp, guardrails, revive as revive_mod

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}       # id -> job dict (includes "_logs")


def _now():
    return datetime.now(timezone.utc)


def _load():
    p = Path(settings.CODEINTEL_JOBS_FILE)
    if p.exists():
        try:
            for j in json.loads(p.read_text()):
                j.setdefault("_logs", [])
                _JOBS[j["id"]] = j
        except Exception:
            pass


def _persist():
    p = Path(settings.CODEINTEL_JOBS_FILE)
    with _LOCK:
        data = []
        for j in _JOBS.values():
            data.append({**j, "_logs": j.get("_logs", [])})
    p.write_text(json.dumps(data, indent=2, default=str))


_load()


# ------------------------------------------------------------- public API ----

def create_job(source: str, stack, use_llm: bool, source_type: str = "local",
               revive: bool = False) -> dict:
    with _LOCK:
        jid = f"job_{len(_JOBS) + 1:03d}"
        while jid in _JOBS:
            jid = f"job_{len(_JOBS) + 2:03d}"
        stages = list(BASE_STAGES)
        if revive:
            stages.append("Revive & crawl")
        job = {
            "id": jid, "source": source, "source_type": source_type,
            "stack": stack, "use_llm": use_llm, "revive": revive, "stages": stages,
            "status": "queued", "progress": 0, "stage": None,
            "created": _now().isoformat(), "started_h": _now().strftime("%H:%M:%S"),
            "result": None, "metrics": None, "error": None, "revive_result": None,
            "continued": False, "finalised": False, "_logs": [],
        }
        _JOBS[jid] = job
    _persist()
    t = threading.Thread(target=_run, args=(jid,), daemon=True)
    t.start()
    return job


def _clone(url: str, jid: str, log) -> Path:
    dest = Path("/tmp") / "codeintel_clones" / jid
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        subprocess.run(["rm", "-rf", str(dest)], check=False)
    log("t", f"$ git clone --depth 1 {url}")
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", url, str(dest)],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise guardrails.GuardrailError("git clone timed out (180s cap).")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise guardrails.GuardrailError(f"git clone failed: {tail[0]}")
    for line in (proc.stderr or "").strip().splitlines()[-3:]:
        log("m", line)
    log("ok", f"cloned to {dest} ✓")
    return dest


def ensure_source_dir(job: dict, log) -> str | None:
    """Resolve a job's source directory for any post-ingest stage (Revive,
    Pixel-Clone, Legacy Transform). Git-sourced jobs re-clone automatically if
    the local clone under /tmp/codeintel_clones is missing — e.g. cleared by
    a periodic /tmp cleanup or a reboot — instead of every caller dead-ending
    with "source not available" and asking the user to re-ingest a pipeline
    that already ran successfully.

    Local-path jobs can't be recovered this way: if the path the user pointed
    at is gone, it's genuinely gone, not something we cloned ourselves.
    """
    src = job.get("source", "")
    if job.get("source_type") != "git":
        return src if src and os.path.isdir(src) else None
    clone = Path("/tmp") / "codeintel_clones" / job["id"]
    if clone.exists():
        return str(clone)
    if not src:
        return None
    log("warn", f"local clone missing (likely cleared by system /tmp cleanup) — re-cloning {src}…")
    try:
        return str(_clone(src, job["id"], log))
    except guardrails.GuardrailError as e:
        log("err", f"re-clone failed: {e}")
        return None


def get_job(jid: str):
    return _JOBS.get(jid)


def list_jobs():
    return sorted(_JOBS.values(), key=lambda j: j["created"], reverse=True)


def logs_since(jid: str, cursor: int):
    job = _JOBS.get(jid)
    if not job:
        return [], cursor
    buf = job.get("_logs", [])
    return buf[cursor:], len(buf)


# --------------------------------------------------------------- runner ----

BASE_STAGES = [
    "Connect source", "Scan tree · skip vendor", "Detect stack",
    "Static analysis", "AI mapping", "Compute UCP",
]
STAGES = BASE_STAGES  # back-compat


def _run(jid: str):
    job = _JOBS[jid]
    stages = job.get("stages", BASE_STAGES)

    def log(level, msg):
        job["_logs"].append({
            "t": _now().strftime("%H:%M:%S"), "level": level, "msg": msg,
        })

    def set_stage(i, name):
        job["stage"] = name
        job["progress"] = int(i / len(stages) * 100)

    try:
        job["status"] = "running"
        _persist()

        # 1. Connect / guardrail the source (local path or git clone)
        set_stage(0, STAGES[0])
        log("t", f"$ codeintel ingest --source {job['source']}")
        if job.get("source_type") == "git":
            url = guardrails.resolve_git_url(job["source"])
            root = _clone(url, jid, log)
        else:
            root = guardrails.resolve_ingest_path(job["source"])
        log("ok", f"source connected & sandboxed ✓  ({root})")
        stack = job.get("stack")
        if isinstance(stack, dict):
            sc = stack.get("scope") or {}
            parts = []
            if sc.get("frontend") and stack.get("frontend"):
                fe = stack["frontend"]
                parts.append(f"Frontend: {fe.get('framework')} ({fe.get('styling')}, {fe.get('state')})")
            if sc.get("backend") and stack.get("backend"):
                be = stack["backend"]
                parts.append(f"Backend: {be.get('framework')} ({be.get('api')}, {be.get('auth')})")
            if sc.get("database") and stack.get("database"):
                db = stack["database"]
                parts.append(f"DB migration: {db.get('source')} → {db.get('target')} ({db.get('strategy')})")
            for p in parts:
                log("m", "scope → " + p)
            if not parts:
                log("m", "scope → (nothing selected)")

        # 2 + 3 + 4. Scan (analyzer emits its own logs)
        set_stage(1, STAGES[1])
        metrics = analyzer.scan(root, log)
        job["metrics"] = metrics
        set_stage(2, STAGES[2])
        log("ok", "stack detected: " + ", ".join(metrics["frameworks"]))
        set_stage(3, STAGES[3])
        log("ok", "static analysis complete ✓")

        # 5. AI mapping (guardrailed)
        set_stage(4, STAGES[4])
        mapping = agent.map_metrics(metrics, job["use_llm"], log)
        log("m", f"mapping mode: {mapping.get('ai_mode')}")

        # 6. UCP
        set_stage(5, STAGES[5])
        result = ucp.compute(mapping)
        result["ai_mode"] = mapping.get("ai_mode")
        result["rationale"] = mapping.get("rationale", "")
        job["result"] = result
        job["mapping"] = {k: mapping[k] for k in ("uaw", "uucw", "tcf", "ecf")}
        log("ok", f"UUCP={result['uucp']} · TCF={result['tcf']} · ECF={result['ecf']}")
        log("ok", f"UCP = {result['ucp']} → band {result['band']} · migration {result['dm_band']} ✓")

        # Optional revive stage — boot the app in containers and crawl it.
        if job.get("revive") and "Revive & crawl" in stages:
            set_stage(stages.index("Revive & crawl"), "Revive & crawl")
            log("info", "revive requested — attempting container boot + crawl")
            try:
                endpoints = (metrics.get("artifacts") or {}).get("endpoints", [])
                rr = revive_mod.revive(root, endpoints, log)
            except Exception as e:
                rr = {"status": "failed", "reason": f"{type(e).__name__}: {e}"}
                log("err", f"revive error — {rr['reason']}")
            job["revive_result"] = rr
            _persist()

        job["status"] = "done"
        job["progress"] = 100
        job["stage"] = "complete"
        rp = job.get("revive_result") or {}
        proof = ""
        if rp.get("status") == "running":
            proof = f" · LIVE {rp['url']} ({rp['pages_working']}/{rp['pages_total']} pages)"
        log("ok", f"● JOB COMPLETE — UCP {result['ucp']} ({result['band']}) · migration {result['dm_band']}{proof}")
        # agent → user messages
        from . import chat
        chat.push(job, "ok", f"Sizing done: UCP {result['ucp']} ({result['band']}), "
                             f"migration {result['dm_band']}. Continue to Artifacts to review.")
        if rp.get("status") == "failed":
            chat.push(job, "warn", f"Boot failed: {rp.get('reason','unknown')}. Want me to retry?",
                      actions=["retry_boot"])
        elif job.get("revive") and rp.get("status") != "running":
            chat.push(job, "info", "Revive was requested but the app isn't running. Retry the boot?",
                      actions=["retry_boot"])
    except guardrails.GuardrailError as e:
        job["status"] = "failed"
        job["error"] = str(e)
        log("err", f"✗ guardrail: {e}")
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        log("err", f"✗ {type(e).__name__}: {e}")
        log("err", traceback.format_exc().splitlines()[-1])
    finally:
        _persist()
