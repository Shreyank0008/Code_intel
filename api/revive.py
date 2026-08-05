"""
Revive stage — actually boot the ingested app and crawl it to PROVE it runs.

Container-isolated only: we boot via `docker compose` (never run arbitrary host
commands from the repo). Strongly guardrailed:
  - OFF unless settings.CODEINTEL_ALLOW_BOOT is true (env CODEINTEL_ALLOW_BOOT=1)
  - requires the `docker` CLI and a compose file in the repo
  - hard timeouts on build / up / health / crawl
  - never blocks the pipeline: any failure returns a status, not an exception

Output (job['revive']):
  {status, url, port, pages_working, pages_total, containers, started, log[]}
status ∈ running | running_no_http | failed | skipped
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from django.conf import settings

COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
BUILD_TIMEOUT = 600
HEALTH_TIMEOUT = 90
CRAWL_CAP = 40
# Ports that are databases / caches / queues / infra — never a web endpoint.
NON_HTTP_PORTS = {
    3306, 5432, 5433, 1433, 1521, 27017, 6379, 11211,        # DBs + cache
    5672, 15672, 9092, 2181, 4222, 6650,                     # queues
    9200, 9300, 7000, 7001, 9042,                            # search / cassandra
    25, 587, 465, 22, 2222,                                  # mail / ssh
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _find_compose(root: Path):
    # shallow search (root + one level) to avoid deep vendored composes
    for name in COMPOSE_NAMES:
        p = root / name
        if p.exists():
            return str(p)
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            for name in COMPOSE_NAMES:
                p = child / name
                if p.exists():
                    return str(p)
    return None


def _published_ports(compose_path: str):
    """Parse (host, container) port pairs from the compose `ports:` entries.
    The CONTAINER port reliably identifies the service (3306 = MySQL etc.),
    even when the host port is remapped."""
    try:
        txt = Path(compose_path).read_text(errors="ignore")
    except OSError:
        return []
    pairs = []
    for m in re.finditer(r'-\s*["\']?(\d{2,5}):(\d{2,5})', txt):
        try:
            pairs.append((int(m.group(1)), int(m.group(2))))
        except ValueError:
            pass
    seen, out = set(), []
    for h, c in pairs:
        if h not in seen:
            seen.add(h); out.append((h, c))
    return sorted(out, key=lambda hc: (hc[0] not in (80, 8080, 3000, 8000, 5000, 8765), hc[0]))


def _http_status(url: str, timeout=4):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "codeintel-revive"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code            # 4xx/5xx still means the server answered
    except Exception:
        return None


def _wait_http(base: str, log, timeout=HEALTH_TIMEOUT):
    import time
    deadline_checks = max(1, timeout // 3)
    for _ in range(deadline_checks):
        st = _http_status(base)
        if st is not None and st < 500:
            return True
        # cheap busy-wait via a subprocess sleep is blocked; use socket timeout loop
        _sleep(3)
    return False


def _sleep(sec):
    # foreground sleep is fine inside a daemon job thread
    import time
    time.sleep(sec)


def revive(root: Path, endpoints, log) -> dict:
    started_at = datetime.now(timezone.utc)

    def finish(result: dict) -> dict:
        result["elapsed_seconds"] = round((datetime.now(timezone.utc) - started_at).total_seconds(), 1)
        # No LLM calls happen on this deterministic (non-agentic) path — an
        # honest all-zero/None shape, not a guess, so the UI can render the
        # same stats strip either way without special-casing this path.
        result.setdefault("usage", {"provider": None, "model": None, "prompt_tokens": None,
                                     "completion_tokens": None, "total_tokens": None, "calls": 0})
        result.setdefault("started_at", started_at.isoformat())
        return result

    if not settings.CODEINTEL_ALLOW_BOOT:
        log("warn", "revive skipped — CODEINTEL_ALLOW_BOOT is off (set env to enable)")
        return finish({"status": "skipped", "reason": "boot disabled (CODEINTEL_ALLOW_BOOT=0)"})
    if shutil.which("docker") is None:
        return finish({"status": "skipped", "reason": "docker CLI not found"})
    compose = _find_compose(root)
    if not compose:
        return finish({"status": "skipped", "reason": "no docker-compose file — only compose-based revive is supported"})

    cwd = os.path.dirname(compose)
    rlog = []

    def L(level, msg):
        log(level, msg); rlog.append({"level": level, "msg": msg})

    # tear down any prior instance of this project, then boot fresh
    L("t", f"$ docker compose -f {os.path.basename(compose)} up -d --build")
    try:
        subprocess.run(["docker", "compose", "-f", compose, "down", "--remove-orphans"],
                       cwd=cwd, capture_output=True, text=True, timeout=120)
    except Exception:
        pass
    try:
        proc = subprocess.run(["docker", "compose", "-f", compose, "up", "-d", "--build"],
                              cwd=cwd, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        L("err", f"revive failed — build/up exceeded {BUILD_TIMEOUT}s")
        return finish({"status": "failed", "reason": "build timeout", "log": rlog, "compose": compose})
    for line in (proc.stderr or "").strip().splitlines()[-4:]:
        L("m", line)
    if proc.returncode != 0:
        L("err", "revive failed — docker compose up returned non-zero")
        return finish({"status": "failed", "reason": "compose up failed", "log": rlog, "compose": compose})

    # count containers
    try:
        ps = subprocess.run(["docker", "compose", "-f", compose, "ps", "-q"],
                            cwd=cwd, capture_output=True, text=True, timeout=30)
        containers = len([x for x in ps.stdout.splitlines() if x.strip()])
    except Exception:
        containers = 0
    L("ok", f"containers up: {containers}")

    # health check — only try WEB ports (skip databases/caches/queues).
    # Filter by the CONTAINER port (reliable) AND the host port.
    pairs = _published_ports(compose)
    ports = [h for h, c in pairs]
    web_ports = [h for h, c in pairs if c not in NON_HTTP_PORTS and h not in NON_HTTP_PORTS]
    db_ports = [h for h, c in pairs if c in NON_HTTP_PORTS or h in NON_HTTP_PORTS]
    L("info", f"published ports: {ports or 'none'} · web candidates: {web_ports or 'none'}"
              + (f" · skipping DB/infra ports {db_ports}" if db_ports else ""))
    if not web_ports:
        reason = (f"no web/HTTP port is published — only {db_ports or 'infra'} "
                  "(database/cache ports). This stack has no web frontend to boot, "
                  "so there's nothing to capture or clone.")
        L("warn", reason)
        return finish({"status": "running_no_http", "containers": containers, "ports": ports,
                "reason": reason, "compose": compose, "cwd": cwd,
                "started": _now_iso(), "log": rlog})
    base, port = None, None
    for p in web_ports:
        cand = f"http://localhost:{p}/"
        L("m", f"waiting for {cand} …")
        if _wait_http(cand, L):
            base, port = cand, p
            break
    if not base:
        reason = f"web port(s) {web_ports} published but no HTTP response — the web service may have failed to start (check its logs)."
        L("warn", reason)
        return finish({"status": "running_no_http", "containers": containers, "ports": ports,
                "reason": reason, "compose": compose, "cwd": cwd,
                "started": _now_iso(), "log": rlog})

    L("ok", f"app is LIVE at {base} ✓")

    # crawl: root + a sample of discovered endpoint paths
    paths = ["/"]
    for e in (endpoints or []):
        pth = (e.get("path") or "").strip()
        if pth.startswith("/") and "<" not in pth and "(" not in pth and "?P" not in pth:
            paths.append(pth)
        if len(paths) >= CRAWL_CAP:
            break
    paths = list(dict.fromkeys(paths))[:CRAWL_CAP]
    working, results = 0, []
    for pth in paths:
        st = _http_status(base.rstrip("/") + pth)
        ok = st is not None and st < 400
        if ok:
            working += 1
        results.append({"path": pth, "status": st, "ok": ok})
    L("ok", f"crawl: {working}/{len(paths)} paths returned OK")

    # RUNTIME endpoint verification — the real route table, not a regex estimate.
    oa = _probe_openapi(base, L)
    static_n = len(endpoints or [])
    api_routes = oa["routes"] if oa else []
    if oa:
        L("ok", f"runtime endpoints verified via {oa['source']}: {len(api_routes)} "
                f"(static estimate was {static_n})")

    return finish({
        "status": "running", "url": base, "port": port,
        "containers": containers, "pages_working": working, "pages_total": len(paths),
        "crawl": results, "compose": compose, "cwd": cwd,
        "api_routes": api_routes, "api_source": (oa or {}).get("source"),
        "static_endpoints": static_n, "started": _now_iso(), "log": rlog,
    })


OPENAPI_PATHS = [
    "/openapi.json", "/v3/api-docs", "/swagger/v1/swagger.json", "/swagger.json",
    "/api/schema/?format=openapi-json", "/api-docs", "/api/docs.json",
]


def _probe_openapi(base: str, log):
    """Fetch the app's real route table from its OpenAPI/Swagger spec, if exposed."""
    import json as _json
    for p in OPENAPI_PATHS:
        url = base.rstrip("/") + p
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "codeintel-revive"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = _json.loads(r.read())
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("paths"), dict):
            routes = []
            for path, ops in data["paths"].items():
                if isinstance(ops, dict):
                    for method in ops:
                        if method.lower() in ("get", "post", "put", "patch", "delete"):
                            routes.append({"method": method.upper(), "path": path})
            if routes:
                log("m", f"OpenAPI spec found at {p}")
                return {"source": p, "routes": routes[:300]}
    return None


def stop(compose: str, cwd: str) -> bool:
    try:
        subprocess.run(["docker", "compose", "-f", compose, "down", "--remove-orphans"],
                       cwd=cwd, capture_output=True, text=True, timeout=120)
        return True
    except Exception:
        return False
