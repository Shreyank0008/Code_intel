"""
Stage 2 EXECUTION — actually run the build-skill scripts as background jobs.

Unlike stage2.py (which only builds the plan), this shells out to the real skill
scripts and streams their output:
  • pixel  → pixel-clone/scripts/capture.py  (Playwright screenshot capture of a
             LIVE running site — the ground-truth capture the diff loop needs)
  • legacy → legacy-transform/scripts/schema_diff.py (harvest table refs from the
             legacy source, report schema completeness)

Guardrails: pixel capture is restricted to localhost / 127.0.0.1 (clone YOUR own
running app, not arbitrary external sites). Hard timeouts. Runs on a daemon thread;
state + logs live in job['stage2'][kind]; the frontend polls.
"""
from __future__ import annotations
import base64
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from django.conf import settings
from . import jobs

SKILLS = Path.home() / ".claude/skills"
PIXEL_CAPTURE = SKILLS / "pixel-clone/scripts/capture.py"
LEGACY_SCHEMA = SKILLS / "legacy-transform/scripts/schema_diff.py"
WORK = Path("/tmp/codeintel_stage2")
CAPTURE_TIMEOUT = 240
SCHEMA_TIMEOUT = 120


def _now():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def start(jid: str, kind: str, answers: dict):
    job = jobs.get_job(jid)
    if not job:
        return {"error": "job not found"}
    slot = {"status": "running", "logs": [], "result": None,
            "started": datetime.now(timezone.utc).isoformat()}
    job.setdefault("stage2", {})[kind] = {**(job.get("stage2", {}).get(kind) or {}), **slot}
    t = threading.Thread(target=_run, args=(jid, kind, answers), daemon=True)
    t.start()
    return {"status": "running"}


def _run(jid, kind, answers):
    job = jobs.get_job(jid)
    slot = job["stage2"][kind]

    def log(level, msg):
        slot["logs"].append({"t": _now(), "level": level, "msg": msg})

    try:
        if kind == "pixel":
            from . import pixel_agent
            slot["result"] = pixel_agent.run(job, answers, log)
        elif kind == "legacy":
            slot["result"] = _run_legacy(jid, job, answers, log)
        else:
            raise ValueError("unknown kind")
        slot["status"] = "done" if slot["result"].get("ok") else "failed"
    except Exception as e:
        slot["status"] = "failed"
        log("err", f"✗ {type(e).__name__}: {e}")
        slot["result"] = {"ok": False, "reason": str(e)}
    finally:
        jobs._persist()


# ------------------------------------------------------------- pixel ----

def _run_pixel(jid, answers, log):
    if not PIXEL_CAPTURE.exists():
        return {"ok": False, "reason": "pixel-clone capture.py not found"}
    url = (answers.get("site_url") or "").strip()
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":")[0].lower() if "//" in url else ""
    if host not in ("localhost", "127.0.0.1", "0.0.0.0"):
        return {"ok": False, "reason": f"capture is restricted to localhost (got '{host or url}'). "
                                       "Point it at your own running app."}
    # reachable?
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=5)
    except Exception:
        return {"ok": False, "reason": f"nothing is running at {url}. Start the app there, "
                "or use the ⚡ Revive stage to boot the ingested app first (its URL auto-fills here)."}

    vp = {"Desktop only (1280×800)": "1280x800", "Desktop + Mobile": "1280x800,375x812"}.get(
        answers.get("viewports", "Desktop only (1280×800)"), "1280x800")
    out = WORK / f"pixel_{jid}" / "captures"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(PIXEL_CAPTURE), "--base", url, "--out", str(out),
           "--viewports", vp, "--full-page"]
    routes = answers.get("routes", "All routes (crawl)")
    if routes == "All routes (crawl)":
        cmd += ["--crawl", "--depth", "2"]
    elif routes == "Named list" and answers.get("routes_list"):
        cmd += ["--routes", answers["routes_list"].replace(" ", "")]
    if answers.get("dynamic") == "Mask dynamic regions":
        cmd += ["--mask", ".carousel,[data-dynamic],time"]

    log("t", "$ " + " ".join(cmd[1:]))
    log("info", f"Playwright capturing {url} @ {vp} …")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CAPTURE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"capture timed out ({CAPTURE_TIMEOUT}s)"}
    for line in (proc.stdout or "").strip().splitlines()[-12:]:
        log("m", line)
    if proc.returncode != 0:
        for line in (proc.stderr or "").strip().splitlines()[-4:]:
            log("err", line)
        return {"ok": False, "reason": "capture.py returned non-zero"}

    # parse manifest + screenshots
    manifest_path = out / "manifest.json"
    routes_out, viewports_out = [], []
    if manifest_path.exists():
        try:
            man = json.loads(manifest_path.read_text())
            routes_out = man.get("routes") or list(man.keys())
            viewports_out = man.get("viewports") or vp.split(",")
        except Exception:
            pass
    pngs = sorted(out.rglob("*.png"))
    sample = None
    if pngs:
        try:
            data = pngs[0].read_bytes()
            if len(data) <= 900_000:
                sample = "data:image/png;base64," + base64.b64encode(data).decode()
        except Exception:
            pass
    log("ok", f"captured {len(pngs)} screenshots · {len(routes_out) or '?'} routes ✓")
    return {"ok": True, "url": url, "viewports": vp, "screenshots": len(pngs),
            "routes": routes_out[:60], "captures_dir": str(out), "sample": sample}


# ------------------------------------------------------------- legacy ----

def _run_legacy(jid, job, answers, log):
    if not LEGACY_SCHEMA.exists():
        return {"ok": False, "reason": "legacy-transform schema_diff.py not found"}
    src = jobs.ensure_source_dir(job, log)
    if not src:
        return {"ok": False, "reason": "legacy source not available locally and could not be re-cloned — re-ingest as a local path"}
    outdir = WORK / f"legacy_{jid}"
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(LEGACY_SCHEMA), "--source", src,
           "--json", str(outdir / "schema_diff.json"), "--md", str(outdir / "schema_diff.md")]
    log("t", "$ " + " ".join(cmd[1:]))
    log("info", f"harvesting table references from {src} …")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SCHEMA_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"schema_diff timed out ({SCHEMA_TIMEOUT}s)"}
    for line in (proc.stdout or "").strip().splitlines()[-20:]:
        log("m", line)
    data = {}
    jf = outdir / "schema_diff.json"
    if jf.exists():
        try:
            data = json.loads(jf.read_text())
        except Exception:
            pass
    log("ok", "schema analysis complete ✓")
    return {"ok": True, "confident_missing": proc.returncode,
            "report": str(outdir / "schema_diff.md"), "data": data,
            "stdout_tail": (proc.stdout or "").strip().splitlines()[-15:]}
