"""
Revive Agent — agentic resurrection using the run-any-codebase skill + an LLM.

This is the AI-agent version of revival (vs. plain `revive.py`). It:
  1. RECON     — runs the skill's recon.sh to detect the stack / how to run it
  2. SCOPE     — classify the repo into its Frontend / Backend / Database blocks
                 BEFORE attempting anything, instead of only discovering gaps
                 reactively after a failed/incomplete boot
  3. BOOT      — docker compose up --build on a SANDBOXED COPY of the repo.
                 When the compose has infra but no app service, the Database
                 lane (bring up what's already defined) and the Backend lane
                 (generate the missing service) run CONCURRENTLY rather than
                 sequentially — the LLM call for "what should the backend
                 look like" doesn't need the DB containers to be up first.
  4. SELF-HEAL — if a build fails, the LLM diagnoses the error + the offending
                 Dockerfile/compose and returns a corrected file; we apply it (only
                 build/config files, inside the sandbox) and retry
  5. HEALTH    — waits for the web endpoint
  6. GAP-SCAN  — runs the skill's gap_scan.py on the running app (broken pages)
  7. HANDOFF   — returns the live URL for the UI-clone step

GUARDRAILS
  • Works on a COPY — the original repo is never mutated.
  • Self-heal may only write build/config files (Dockerfile*, *compose*, .env,
    requirements.txt, package.json) INSIDE the sandbox; nothing else, no shell.
  • Max 2 boot attempts; per-phase timeouts; container-isolated (docker only).
  • Provider-gated — with no LLM it still recons + boots + reports (no self-heal).
"""
from __future__ import annotations
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from django.conf import settings
from . import jobs
from . import providers
from . import revive as R   # reuse ports/health/openapi helpers
from .skill_paths import skill_script

RECON = skill_script("run-any-codebase/scripts/recon.sh")
GAPSCAN = skill_script("run-any-codebase/scripts/gap_scan.py")
WORK = Path("/tmp/codeintel_revive_agent")
BUILD_TIMEOUT = 600
MAX_ATTEMPTS = 2
_EDITABLE = re.compile(r"(dockerfile|docker-compose|compose|\.env|requirements\.txt|package\.json|\.ya?ml)$", re.I)

_INFRA_LABELS = {
    3306: "MySQL", 5432: "PostgreSQL", 5433: "PostgreSQL", 1433: "SQL Server",
    1521: "Oracle", 27017: "MongoDB", 6379: "Redis", 11211: "Memcached",
    5672: "RabbitMQ", 15672: "RabbitMQ management", 9092: "Kafka", 2181: "Zookeeper",
    4222: "NATS", 6650: "Pulsar", 9200: "Elasticsearch", 9300: "Elasticsearch (transport)",
    7000: "Cassandra", 7001: "Cassandra", 9042: "Cassandra (CQL)",
    25: "SMTP", 587: "SMTP", 465: "SMTP", 22: "SSH", 2222: "SSH",
}


def _now():
    return datetime.now(timezone.utc)


DOCKER_START_TIMEOUT = 90


def _docker_daemon_up() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


def _ensure_docker_running(log) -> bool:
    """Self-heal: if the Docker daemon isn't reachable, try to start Docker
    Desktop ourselves and wait for it, instead of dead-ending on a "Cannot
    connect to the Docker daemon" build error the user then has to diagnose
    by hand. Only surfaces back to the user if this genuinely can't be done
    automatically (non-macOS, Docker Desktop not installed, or it doesn't
    come up in time)."""
    if _docker_daemon_up():
        return True
    if sys.platform != "darwin":
        log("warn", "Docker daemon isn't reachable and I can't auto-start it on this OS — "
                     "start Docker manually and retry.")
        return False
    log("warn", "Docker daemon isn't running — launching Docker Desktop automatically…")
    try:
        subprocess.run(["open", "-a", "Docker"], capture_output=True, timeout=10)
    except Exception as e:
        log("warn", f"could not launch Docker Desktop automatically ({e}) — start it manually and retry.")
        return False
    waited = 0
    while waited < DOCKER_START_TIMEOUT:
        _sleep(3)
        waited += 3
        if _docker_daemon_up():
            log("ok", f"Docker is ready ✓ (took {waited}s to start)")
            return True
    log("warn", f"Docker Desktop was launched but didn't become ready within {DOCKER_START_TIMEOUT}s — "
                "check it manually and retry.")
    return False


def _sleep(sec):
    import time
    time.sleep(sec)


def _accumulate_usage(usage: dict, call_usage: dict | None, log) -> None:
    """Fold one LLM call's token usage into the run's running total. Token
    counts stay None (not 0) if a provider/model never reported them, so the
    UI can tell "0 tokens" from "not measurable" apart."""
    if not call_usage:
        return
    usage["provider"] = call_usage.get("provider") or usage["provider"]
    usage["model"] = call_usage.get("model") or usage["model"]
    usage["calls"] += 1
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = call_usage.get(k)
        if v is None:
            continue
        usage[k] = (usage[k] or 0) + v
    if call_usage.get("total_tokens") is not None:
        log("m", f"tokens: +{call_usage['total_tokens']} this call "
                 f"(running total {usage['total_tokens']})")


def _container_logs_tail(compose: str, cwd: str, log, lines: int = 6) -> str:
    """Grab a short tail of `docker compose logs` so a no-HTTP-response failure
    says WHY (crashed, still starting, exception on boot) instead of just THAT."""
    try:
        proc = subprocess.run(["docker", "compose", "-f", compose, "logs", "--tail", str(lines)],
                              cwd=cwd, capture_output=True, text=True, timeout=20)
        out = (proc.stdout or "").strip().splitlines()
        return " | ".join(out[-lines:]) if out else ""
    except Exception:
        return ""


def _check_container_crashed(compose: str, cwd: str, log) -> str:
    """`docker compose up` reports success once containers START — it does
    NOT mean they stayed up. A container can build fine and then crash
    moments later on a runtime error (bad datasource config, missing env
    var, uncaught startup exception) that a build-exit-code check can never
    see. Give it a moment, then look for anything already exited and pull
    ITS specific log — so a runtime crash gets diagnosed concretely instead
    of silently falling into a doomed HTTP-wait that just times out."""
    time.sleep(3)
    try:
        proc = subprocess.run(["docker", "compose", "-f", compose, "ps", "-a", "--format", "json"],
                              cwd=cwd, capture_output=True, text=True, timeout=15)
    except Exception:
        return ""
    for line in (proc.stdout or "").strip().splitlines():
        try:
            info = json.loads(line)
        except Exception:
            continue
        if (info.get("State") or "").lower() not in ("exited", "dead"):
            continue
        name = info.get("Service") or info.get("Name") or "?"
        try:
            logs_proc = subprocess.run(["docker", "compose", "-f", compose, "logs", "--tail", "25", name],
                                       cwd=cwd, capture_output=True, text=True, timeout=15)
            tail = (logs_proc.stdout or logs_proc.stderr or "").strip()
        except Exception:
            tail = ""
        log("err", f"container '{name}' exited shortly after starting — a runtime crash, not a build failure")
        for tail_line in tail.splitlines()[-6:]:
            log("err", tail_line)
        return f"service '{name}' started but exited immediately (exit code {info.get('ExitCode')}). Its log:\n{tail[-2500:]}"
    return ""


def _port_free(port: int) -> bool:
    for family, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
            s.close()
        except OSError:
            return False
    return True


def _free_port_from(start: int) -> int:
    port = start
    while port < 65535:
        if _port_free(port):
            return port
        port += 1
    return start  # exhausted the range; fall back to the original (will surface via self-heal)


def _avoid_port_conflicts(compose_path: str, log) -> list:
    """Pre-flight: remap any HOST ports the compose file wants that are already
    bound on this machine, before the first `docker compose up`.

    Without this, port conflicts get discovered one at a time by the self-heal
    loop — and since a compose file often has multiple services (e.g. mysql +
    postgres), a single-attempt-budget self-heal can fix one and still run out
    of attempts before reaching the next. Checking every mapped port upfront
    avoids that entirely instead of patching them reactively.
    """
    try:
        txt = Path(compose_path).read_text()
    except OSError:
        return []
    remapped = []

    def _sub(m):
        host, container = int(m.group(1)), int(m.group(2))
        if _port_free(host):
            return m.group(0)
        new_port = _free_port_from(host + 1)
        remapped.append((host, new_port, container))
        return m.group(0).replace(f"{host}:{container}", f"{new_port}:{container}")

    new_txt = re.sub(r'-\s*["\']?(\d{2,5}):(\d{2,5})', _sub, txt)
    if remapped:
        Path(compose_path).write_text(new_txt)
        for host, new_port, container in remapped:
            log("info", f"Revive Agent · host port {host} already in use — "
                         f"remapped to {new_port} before the first boot attempt "
                         f"(container port {container} unchanged)")
    return remapped


_FRONTEND_DIRS = ("frontend", "client", "web", "ui")
_FRONTEND_FRAMEWORK_DEPS = ("react", "vue", "angular", "svelte", "next", "@angular/core", "nuxt")
_BACKEND_FILES = {
    "pom.xml": "Java/Maven", "build.gradle": "Java/Gradle", "build.gradle.kts": "Java/Gradle",
    "requirements.txt": "Python", "pyproject.toml": "Python", "go.mod": "Go",
    "Gemfile": "Ruby", "composer.json": "PHP", "Cargo.toml": "Rust",
}


def _identify_scope(sandbox: Path, compose: str | None, recon: dict, log) -> dict:
    """SCOPE phase — classify the repo into the three blocks a full-stack app
    is made of, BEFORE attempting anything. Boot used to discover "oh, the
    backend's missing" only after a failed/incomplete `docker compose up`;
    this makes that upfront and drives which lanes actually run."""
    stack = recon.get("stack") or []
    backend_evidence = [name for name in _BACKEND_FILES if _find_first(sandbox, name)]
    backend = {
        "detected": bool(backend_evidence) or bool(stack),
        "label": ", ".join(stack) if stack else
                 (_BACKEND_FILES.get(backend_evidence[0]) if backend_evidence else "unknown"),
        "evidence": backend_evidence,
    }

    engines = []
    if compose and Path(compose).exists():
        try:
            txt = Path(compose).read_text(errors="ignore")
            for m in re.finditer(r':(\d{2,5})(?:["\']|\s|$)', txt):
                label = _INFRA_LABELS.get(int(m.group(1)))
                if label and label not in engines and "SMTP" not in label and "SSH" not in label:
                    engines.append(label)
        except OSError:
            pass
    database = {"detected": bool(engines), "engines": engines}

    # A separate frontend only counts with its OWN package.json in a
    # dedicated folder AND a real UI framework dep — a root package.json for
    # a Node backend, or server-rendered templates, don't count as one.
    frontend = {"detected": False, "where": None}
    for d in _FRONTEND_DIRS:
        pkg_path = sandbox / d / "package.json"
        if not pkg_path.exists():
            continue
        try:
            deps = json.loads(pkg_path.read_text(errors="ignore"))
        except Exception:
            continue
        all_deps = {**deps.get("dependencies", {}), **deps.get("devDependencies", {})}
        if any(fw in all_deps for fw in _FRONTEND_FRAMEWORK_DEPS):
            frontend = {"detected": True, "where": d}
            break

    log("m", f"scope · backend: {'✓ ' + backend['label'] if backend['detected'] else '✗ not detected'}"
             f" · database: {'✓ ' + ', '.join(engines) if engines else '✗ not detected'}"
             f" · frontend: {'✓ separate (' + frontend['where'] + ')' if frontend['detected'] else '(none — bundled or server-rendered)'}")
    return {"backend": backend, "database": database, "frontend": frontend}


def _boot_db_services(compose: str, cwd: str, log) -> bool:
    """DATABASE LANE — bring up whatever infra services the compose already
    defines. Runs on its own thread, concurrently with the backend lane
    generating the missing app service, since the two don't depend on each
    other yet (only the FINAL combined boot needs both defined together)."""
    names = _compose_db_service_names(compose)
    if not names:
        log("warn", "database lane — no infra services found to bring up", lane="database")
        return True  # nothing to fail on
    log("info", f"database lane — bringing up {', '.join(names)}…", lane="database")
    try:
        # Named explicitly (not the whole file) so this doesn't race the
        # backend lane concurrently writing a new service into the same
        # compose file on another thread.
        proc = subprocess.run(["docker", "compose", "-f", compose, "up", "-d", "--build", *names],
                              cwd=cwd, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("err", "database lane — build timed out", lane="database")
        return False
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        for line in tail:
            log("err", line, lane="database")
        return False
    log("ok", "database lane — up ✓", lane="database")
    return True


def _compose_db_service_names(compose_path: str) -> list[str]:
    """Service names whose published ports are all known DB/infra ports —
    lets the database lane target `docker compose up` at just those
    services, instead of the whole file, while the backend lane may still be
    concurrently writing a new service into that same file on another thread."""
    try:
        txt = Path(compose_path).read_text(errors="ignore")
    except OSError:
        return []
    lines = txt.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "services:")
    except StopIteration:
        return []
    blocks, cur_name, cur_lines = [], None, []
    for line in lines[start + 1:]:
        m = re.match(r"^ {2}(\S+):\s*$", line)
        if m:
            if cur_name:
                blocks.append((cur_name, cur_lines))
            cur_name, cur_lines = m.group(1), []
        elif line and not line[0].isspace():
            break  # dedented past the services: block
        else:
            cur_lines.append(line)
    if cur_name:
        blocks.append((cur_name, cur_lines))

    names = []
    for name, block_lines in blocks:
        block_txt = "\n".join(block_lines)
        ports = [(int(a), int(b)) for a, b in re.findall(r'-\s*["\']?(\d{2,5}):(\d{2,5})', block_txt)]
        if ports and all(c in R.NON_HTTP_PORTS or h in R.NON_HTTP_PORTS for h, c in ports):
            names.append(name)
    return names


def run(job: dict, log) -> dict:
    started_at = _now()
    usage = {"provider": None, "model": None, "prompt_tokens": 0,
             "completion_tokens": 0, "total_tokens": 0, "calls": 0}
    job["_revive_usage"] = usage  # live -- same dict _accumulate_usage() mutates,
                                   # so the stats chip updates during "booting" too
    scope = None
    lanes = {"database": "pending", "backend": "pending", "frontend": "pending"}
    job["_revive_lanes"] = lanes

    def set_lane(name, status):
        lanes[name] = status

    def finish(result: dict) -> dict:
        result["elapsed_seconds"] = round((_now() - started_at).total_seconds(), 1)
        result["usage"] = usage
        result["scope"] = scope
        result["lanes"] = dict(lanes)
        result.setdefault("started_at", started_at.isoformat())
        return result

    if not settings.CODEINTEL_ALLOW_BOOT:
        return finish({"status": "skipped", "reason": "boot disabled (CODEINTEL_ALLOW_BOOT=0)"})
    if shutil.which("docker") is None:
        return finish({"status": "skipped", "reason": "docker CLI not found"})
    if not _ensure_docker_running(log):
        return finish({"status": "skipped",
                       "reason": "Docker Desktop isn't running and couldn't be started automatically"})
    src = jobs.ensure_source_dir(job, log)
    if not src:
        return finish({"status": "skipped", "reason": "source not available locally and could not be re-cloned"})

    sandbox = WORK / job["id"]
    if sandbox.exists():
        subprocess.run(["rm", "-rf", str(sandbox)], check=False)
    log("info", "Revive Agent · copying repo to a sandbox (original untouched)…")
    shutil.copytree(src, sandbox, ignore=shutil.ignore_patterns(
        "node_modules", ".git", ".venv", "__pycache__", "dist", "build", "target"), dirs_exist_ok=True,
        symlinks=True, ignore_dangling_symlinks=True)

    # Phase 1 — recon (skill)
    recon = _recon(sandbox, log)

    compose = R._find_compose(sandbox)
    provider = providers.active_provider()

    # Phase 2 — SCOPE: classify the repo into its blocks BEFORE attempting
    # anything, and set the lane state the UI renders live from here on.
    scope = _identify_scope(sandbox, compose, recon, log)
    job["_revive_scope"] = scope  # live, not just in the final result -- the
                                   # scope card should show during "booting" too
    # Revive's job is proving the app boots and answers HTTP — building a
    # SEPARATE frontend bundle is Pixel-Clone's job, not this lane's. Always
    # "skipped" here; scope.frontend still reports whether one was detected,
    # so the UI can show *that* without this lane falsely implying Revive is
    # about to build it.
    set_lane("frontend", "skipped")

    containerized = False
    if not compose:
        set_lane("backend", "generating")
        if provider == "heuristic":
            return finish({"status": "failed", "recon": recon,
                    "reason": "no docker-compose and no LLM to generate one — set a provider in Settings"})
        # ASK before containerizing (run-any-codebase style)
        from . import chat
        stack = ", ".join(recon.get("stack", [])) or "this"
        choice = chat.ask(job, f"No docker-compose found. I can **containerize** this "
                          f"{stack} app (generate a Dockerfile + compose, the run-any-codebase way) "
                          "and boot it. Proceed?",
                          choices=["Generate & boot", "Skip"], timeout=180, default="Generate & boot")
        if choice == "Skip":
            set_lane("backend", "failed")
            return finish({"status": "failed", "reason": "no compose; user skipped containerization", "recon": recon})
        compose = _containerize(job, sandbox, recon, log, usage)
        if not compose:
            set_lane("backend", "failed")
            return finish({"status": "failed", "reason": "could not generate a working container for this app",
                    "recon": recon})
        set_lane("backend", "built")
        set_lane("database", "pending")  # generated alongside the backend; boots in the unified step below
        containerized = True
    cwd = os.path.dirname(compose)

    # Pre-flight: fix every host-port conflict at once, before spending any
    # of the self-heal attempt budget reacting to them one at a time.
    _avoid_port_conflicts(compose, log)

    fixes = []
    augmented = False
    # Phase 3 — PARALLEL LANES: scope already told us if the compose has
    # infra but no app service. Act on that now, upfront, instead of
    # discovering it reactively after a doomed first boot attempt — and run
    # the database lane (bring up what's already defined) concurrently with
    # the backend lane (LLM call to generate what's missing), since neither
    # depends on the other finishing first.
    pairs0 = R._published_ports(compose)
    has_app_service = any(c not in R.NON_HTTP_PORTS and h not in R.NON_HTTP_PORTS for h, c in pairs0)
    if not containerized and not has_app_service and pairs0 and provider != "heuristic":
        augmented = True
        infra = [(h, c) for h, c in pairs0 if c in R.NON_HTTP_PORTS or h in R.NON_HTTP_PORTS]
        infra_desc = ", ".join(f"{_INFRA_LABELS.get(c, f'port {c}')} ({h}→{c})" for h, c in infra) or "none"
        set_lane("database", "starting")
        set_lane("backend", "generating")
        log("info", f"scope shows infra-only ({infra_desc}) — bringing up the database lane and "
                    f"generating the backend lane concurrently…")
        with ThreadPoolExecutor(max_workers=2) as ex:
            db_future = ex.submit(_boot_db_services, compose, cwd, log)
            backend_future = ex.submit(_augment_missing_app_service, job, sandbox, compose, recon,
                                        infra_desc, log, usage)
            db_ok = db_future.result()
            fix = backend_future.result()
        set_lane("database", "healthy" if db_ok else "failed")
        if fix:
            fixes.append(fix)
            set_lane("backend", "built")
        else:
            set_lane("backend", "failed")

    # Phase 4 — unified boot + self-heal loop. Runs regardless of whether the
    # parallel-prep step above ran: it's what actually builds/starts the
    # backend service the prep step just wrote (prep only generates the
    # files), it's the sole path for repos whose compose was already
    # complete, and any build error here — including one in a freshly
    # generated backend — still goes through the same self-heal as always.
    _wire_db_healthchecks(sandbox, compose, log)
    _wire_framework_db_init(sandbox, compose, log)

    provider = providers.active_provider()
    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log("t", f"$ docker compose up -d --build  (attempt {attempt}/{MAX_ATTEMPTS})")
        if not (attempt == 1 and augmented):
            # Skip on the first attempt right after parallel-prep — the
            # database lane deliberately already brought those containers
            # up; tearing them down here would just throw away the head
            # start gained by running it concurrently with the backend lane.
            subprocess.run(["docker", "compose", "-f", compose, "down", "--remove-orphans"],
                           cwd=cwd, capture_output=True, text=True, timeout=120)
        try:
            proc = subprocess.run(["docker", "compose", "-f", compose, "up", "-d", "--build"],
                                  cwd=cwd, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            last_err = f"build timeout ({BUILD_TIMEOUT}s)"
            break
        if proc.returncode == 0:
            # A clean exit code only means the containers STARTED — check
            # they're still up a moment later before calling this a success.
            crash = _check_container_crashed(compose, cwd, log)
            if not crash:
                break
            last_err = crash
        else:
            last_err = (proc.stderr or proc.stdout or "").strip()
            for line in last_err.splitlines()[-4:]:
                log("err", line)
        if attempt >= MAX_ATTEMPTS or provider == "heuristic":
            if provider == "heuristic":
                log("warn", "self-heal needs an LLM provider — set one in Settings.")
            break
        # SELF-HEAL: ask the LLM to fix the offending build file (asks the user to approve)
        fix = _self_heal(job, sandbox, compose, last_err, log, usage)
        if fix:
            fixes.append(fix)
        else:
            log("warn", "self-heal could not produce a safe fix — stopping.")
            break
    else:
        pass

    # did it come up?
    ps = subprocess.run(["docker", "compose", "-f", compose, "ps", "-q"],
                        cwd=cwd, capture_output=True, text=True, timeout=30)
    containers = len([x for x in ps.stdout.splitlines() if x.strip()])
    if containers == 0:
        set_lane("database", "failed")
        set_lane("backend", "failed")
        return finish({"status": "failed", "reason": f"could not boot after {MAX_ATTEMPTS} attempt(s): "
                + (last_err.splitlines()[-1] if last_err else "unknown"),
                "recon": recon, "fixes": fixes, "attempts": attempt, "max_attempts": MAX_ATTEMPTS})

    # Phase 4 — health (web ports only). `augmented` carries over from Phase 3
    # unchanged — NOT reset here — so this only re-triggers the augment path
    # as a fallback when Phase 3's upfront check didn't already handle it.
    post_augment_heals = 0
    MAX_POST_AUGMENT_HEALS = 2
    # True right after a build attempt actually failed (nonzero exit) — the
    # compose file can still claim a web port in this state (it's parsed from
    # the FILE, not from what's actually running), so this has to gate the
    # HTTP-wait explicitly or a broken build gets misread as "slow to start."
    build_failed = False
    while True:
        pairs = R._published_ports(compose)
        web_ports = [h for h, c in pairs if c not in R.NON_HTTP_PORTS and h not in R.NON_HTTP_PORTS]
        base, port = None, None
        if not build_failed:
            for p in web_ports:
                cand = f"http://localhost:{p}/"
                log("m", f"waiting for {cand} …")
                if R._wait_http(cand, log):
                    base, port = cand, p
                    break
        if base:
            break
        infra = [(h, c) for h, c in pairs if c in R.NON_HTTP_PORTS or h in R.NON_HTTP_PORTS]
        infra_desc = ", ".join(f"{_INFRA_LABELS.get(c, f'port {c}')} ({h}→{c})" for h, c in infra) or "none"
        # SELF-HEAL: the compose booted but never had an app service at all — a
        # different shape of gap than a build error, so it needs its own repair
        # path instead of just being reported. Try it once, same guardrail
        # (asks before applying) as the error-driven self-heal above.
        if not web_ports and not augmented and provider != "heuristic":
            augmented = True
            set_lane("database", "starting")
            set_lane("backend", "generating")
            fix = _augment_missing_app_service(job, sandbox, compose, recon, infra_desc, log, usage)
            set_lane("database", "healthy")  # already up (this is the reactive fallback, boot already succeeded)
            if fix:
                fixes.append(fix)
                set_lane("backend", "built")
                log("t", "$ docker compose up -d --build  (retrying after augment)")
                retry_proc = subprocess.run(["docker", "compose", "-f", compose, "up", "-d", "--build"],
                               cwd=cwd, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
                build_failed = retry_proc.returncode != 0
                if build_failed:
                    last_err = (retry_proc.stderr or retry_proc.stdout or "").strip()
                    for line in last_err.splitlines()[-4:]:
                        log("err", line)
                ps2 = subprocess.run(["docker", "compose", "-f", compose, "ps", "-q"],
                                     cwd=cwd, capture_output=True, text=True, timeout=30)
                containers = len([x for x in ps2.stdout.splitlines() if x.strip()])
                continue  # re-check health against the augmented compose
            else:
                set_lane("backend", "failed")
        # SELF-HEAL: the augment step's own build broke (e.g. a stale/removed
        # base image) — feed that error into the SAME error-driven self-heal
        # used for the main boot loop, instead of stopping the moment the
        # first repair attempt has a bug of its own.
        if build_failed and augmented and last_err and post_augment_heals < MAX_POST_AUGMENT_HEALS \
                and provider != "heuristic":
            post_augment_heals += 1
            log("info", f"the augmented build itself failed — self-healing that error "
                        f"(attempt {post_augment_heals}/{MAX_POST_AUGMENT_HEALS})…")
            fix = _self_heal(job, sandbox, compose, last_err, log, usage)
            last_err = ""
            if not fix:
                build_failed = False  # can't heal it further; stop treating it as "just failed"
                log("warn", "self-heal could not produce a safe fix for the augmented build — stopping.")
            else:
                fixes.append(fix)
                log("t", "$ docker compose up -d --build  (retrying after error self-heal)")
                retry_proc = subprocess.run(["docker", "compose", "-f", compose, "up", "-d", "--build"],
                               cwd=cwd, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
                build_failed = retry_proc.returncode != 0
                if build_failed:
                    last_err = (retry_proc.stderr or retry_proc.stdout or "").strip()
                    for line in last_err.splitlines()[-4:]:
                        log("err", line)
                ps3 = subprocess.run(["docker", "compose", "-f", compose, "ps", "-q"],
                                     cwd=cwd, capture_output=True, text=True, timeout=30)
                containers = len([x for x in ps3.stdout.splitlines() if x.strip()])
                continue
        if not web_ports or build_failed:
            reason = (f"no web/app port was published by this compose file — only infra "
                      f"services came up ({infra_desc}). The compose here only starts "
                      f"{'the database' if len(infra) == 1 else 'databases'}; the application "
                      f"container itself is missing from it, not just slow to respond.")
            if build_failed:
                reason = (f"the application service's build failed and never started "
                          f"(only infra came up: {infra_desc})." +
                          (f" Auto-added the service and tried to self-heal its build "
                           f"{post_augment_heals} time(s) — see the log above for the real error."
                           if post_augment_heals else
                           " Auto-added the service, but its build failed — see the log above for the real error."))
            elif augmented:
                reason += " An attempt to auto-add the missing service didn't produce a working app."
        else:
            tried = ", ".join(f"http://localhost:{p}/" for p in web_ports)
            tail = _container_logs_tail(compose, cwd, log)
            reason = (f"checked {tried} — none answered within {R.HEALTH_TIMEOUT}s."
                      + (f" Last container log lines: {tail}" if tail else " No container log output captured."))
        return finish({"status": "running_no_http", "containers": containers,
                "reason": reason,
                "recon": recon, "fixes": fixes, "compose": compose, "cwd": cwd,
                "attempts": attempt, "max_attempts": MAX_ATTEMPTS})
    # Only claim the database lane is "healthy" if a database was actually
    # detected — for a backend-only app like fastapi-shop, there's no DB to
    # be healthy OR unhealthy, and "healthy" would misleadingly imply one's
    # running when the correct read is "not needed."
    set_lane("database", "healthy" if scope["database"]["detected"] else "skipped")
    set_lane("backend", "healthy")
    log("ok", f"app is LIVE at {base} ✓")

    # Phase 5 — gap scan (skill) + OpenAPI verify
    gaps = _gap_scan(base, sandbox, log)
    oa = R._probe_openapi(base, log)

    # Hand the DB connection forward so Legacy Transform can prefill it instead
    # of the user reading ports off `docker ps`. Suppressed in demo mode, where
    # the response is exposed publicly and this carries credentials.
    dbs = [] if getattr(settings, "CODEINTEL_DEMO_MODE", False) else _db_urls_from_compose(compose)
    if dbs:
        log("m", "database connection captured for Legacy Transform: "
                 + ", ".join(f"{d['service']} ({d['engine']} :{d['port']})" for d in dbs))

    log("ok", f"● Revive Agent complete — LIVE {base} · {len(fixes)} self-heal fix(es) · "
              f"{len(gaps.get('broken', []))} broken page(s)")
    return finish({
        "databases": dbs,
        "db_url": dbs[0]["url"] if dbs else "",
        "status": "running", "url": base, "port": port, "containers": containers,
        "pages_working": gaps.get("ok", 0), "pages_total": gaps.get("total", 0),
        "api_routes": (oa or {}).get("routes", []), "api_source": (oa or {}).get("source"),
        "recon": recon, "fixes": fixes, "gaps": gaps, "containerized": containerized,
        "compose": compose, "cwd": cwd, "started": _now().isoformat(),
        "attempts": attempt, "max_attempts": MAX_ATTEMPTS,
        "agent": True,
    })


# --------------------------------------------------- containerization ----

def _pick_port(jid: str) -> int:
    m = re.search(r"(\d+)", jid)
    return 8100 + ((int(m.group(1)) if m else 0) % 800)


_SPRING_PROFILE_FILE_RE = re.compile(r"^application-([A-Za-z0-9_]+)\.(?:properties|ya?ml)$")
_DB_IMAGE_RE = re.compile(r"^\s*image:\s*[\"']?(mysql|mariadb|postgres|postgresql)\b", re.M | re.I)
_JDBC_RE = re.compile(r"jdbc:(\w+)://", re.I)


def _spring_profiles(sandbox: Path) -> set:
    """Profile names the repo actually ships (application-mysql.properties -> mysql)."""
    found, seen = set(), 0
    for p in sandbox.rglob("application-*"):
        seen += 1
        if seen > 4000:
            break
        m = _SPRING_PROFILE_FILE_RE.match(p.name)
        if m:
            found.add(m.group(1).lower())
    return found


def _compose_service_blocks(text: str) -> list:
    """[(name, start_idx, end_idx, key_indent)] over the top-level `services:` map.

    Deliberately not a YAML parser -- pyyaml isn't a dependency of this project
    and adding one for a targeted edit isn't worth it. Indentation is read from
    the file rather than assumed, so 2- and 4-space composes both work."""
    lines = text.splitlines(keepends=True)
    starts, out = [], []
    in_services = False
    svc_indent = None
    for i, line in enumerate(lines):
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if not in_services:
            continue
        if line.strip() and not line[0].isspace():
            break  # a new top-level key ended the services map
        m = re.match(r"^(\s+)([A-Za-z0-9._-]+):\s*$", line)
        if m:
            indent = len(m.group(1))
            if svc_indent is None:
                svc_indent = indent
            if indent == svc_indent:
                starts.append((m.group(2), i))
    for n, (name, i) in enumerate(starts):
        end = starts[n + 1][1] if n + 1 < len(starts) else len(lines)
        out.append((name, i, end, (svc_indent or 2) + 2))
    return out


def _app_service_block(text: str):
    """The service that builds THIS repo -- the one a startup command belongs on."""
    lines = text.splitlines(keepends=True)
    for name, start, end, indent in _compose_service_blocks(text):
        body = "".join(lines[start:end])
        if re.search(r"^\s+build:", body, re.M):
            return name, start, end, indent
    return None


def _inject_startup_command(compose_path: Path, text: str, prefix: str, log) -> bool:
    """Run `prefix` before whatever the app service already starts with.

    Django/Rails create their tables through a CLI step (migrate / db:prepare),
    not a config flag, so the Spring env-var approach doesn't transfer -- the
    command itself has to change."""
    blk = _app_service_block(text)
    if not blk:
        log("warn", "no build: service in the compose — startup command not wired")
        return False
    name, start, end, indent = blk
    lines = text.splitlines(keepends=True)
    body = "".join(lines[start:end])

    m = re.search(r"^(\s+)command:\s*(.+?)\s*$", body, re.M)
    if m:
        existing = m.group(2).strip()
        if existing.startswith(("[", "{", ">", "|")):
            log("warn", f"'{name}' uses a list/block command — not rewriting it; "
                        f"run `{prefix}` manually if tables are missing")
            return False
        if existing.startswith(("'", '"')):
            existing = existing[1:-1] if len(existing) > 1 and existing[-1] == existing[0] else existing
        if prefix.split()[0] in existing and "migrate" in existing or "db:prepare" in existing:
            return False  # already does it
        if '"' in existing:
            log("warn", f"'{name}' command contains quotes — not rewriting automatically")
            return False
        new_cmd = f"""{m.group(1)}command: 'sh -c "{prefix} && exec {existing}"'\n"""
        body = body[: m.start()] + new_cmd + body[m.end() + 1:]
    else:
        # No command: the image's CMD starts it. Read that out of the Dockerfile
        # so the prefix can run ahead of it instead of replacing it.
        dockerfile = compose_path.parent / "Dockerfile"
        cmd = ""
        if dockerfile.exists():
            dm = re.findall(r"^\s*CMD\s+(.+)$", dockerfile.read_text(errors="ignore"), re.M)
            if dm:
                raw = dm[-1].strip()
                if raw.startswith("["):
                    try:
                        cmd = " ".join(json.loads(raw))
                    except Exception:
                        cmd = ""
                else:
                    cmd = raw
        if not cmd or '"' in cmd:
            log("warn", f"could not determine '{name}' start command — "
                        f"`{prefix}` not wired; tables may be missing on first boot")
            return False
        pad = " " * indent
        body = body.rstrip("\n") + f"""\n{pad}command: 'sh -c "{prefix} && exec {cmd}"'\n"""

    new_text = "".join(lines[:start]) + body + "".join(lines[end:])
    try:
        compose_path.write_text(new_text)
    except OSError:
        return False
    log("ok", f"wired `{prefix}` into the '{name}' start command — without it the "
              "tables are never created and DB-backed pages fail ✓")
    return True


def _wire_django(sandbox: Path, compose_path: Path, text: str, log) -> bool:
    if not (sandbox / "manage.py").exists() and not _find_first(sandbox, "manage.py"):
        return False
    if re.search(r"manage\.py\s+migrate", text):
        return False
    return _inject_startup_command(compose_path, text, "python manage.py migrate --noinput", log)


def _wire_rails(sandbox: Path, compose_path: Path, text: str, log) -> bool:
    if not (sandbox / "config" / "application.rb").exists():
        return False
    if re.search(r"db:(prepare|setup|migrate|schema:load)", text):
        return False
    # db:prepare is the idempotent one -- creates the DB if absent, loads the
    # schema, then applies migrations. db:migrate alone fails on an empty DB.
    return _inject_startup_command(compose_path, text, "bin/rails db:prepare", log)


_SERVICE_IMAGE_RE = re.compile(r"^\s+image:\s*[\"']?([\w.\-/]+)", re.M)
# Health probes verified by running them inside the real images before being
# wired in -- a probe that never passes is worse than the race it fixes,
# because `condition: service_healthy` would then block the app forever.
_DB_HEALTHCHECKS = {
    "postgres": 'pg_isready -U "$${POSTGRES_USER:-postgres}" -d "$${POSTGRES_DB:-postgres}"',
    "postgis": 'pg_isready -U "$${POSTGRES_USER:-postgres}" -d "$${POSTGRES_DB:-postgres}"',
    "mysql": 'mysqladmin ping -h 127.0.0.1 -u root --password="$${MYSQL_ROOT_PASSWORD:-}" --silent',
    "mariadb": 'mysqladmin ping -h 127.0.0.1 -u root --password="$${MARIADB_ROOT_PASSWORD:-$${MYSQL_ROOT_PASSWORD:-}}" --silent',
    "redis": "redis-cli ping",
    "mongo": "mongosh --quiet --eval 'db.adminCommand(\"ping\")'",
}


def _db_kind(image: str) -> str | None:
    base = image.split("/")[-1].split(":")[0].lower()
    if base in ("postgresql",):
        base = "postgres"
    return base if base in _DB_HEALTHCHECKS else None


def _db_urls_from_compose(compose: str) -> list:
    """Connection URLs for every database the revived app just booted.

    Revive wrote (or adapted) this compose, so it already knows the engine, the
    published host port and the credentials -- making the user read them off
    `docker ps` and retype them into Legacy Transform is busywork, and typing
    the wrong port is the likeliest way to get a confusing failure.
    """
    try:
        text = Path(compose).read_text(errors="ignore")
    except OSError:
        return []
    if text and not text.endswith("\n"):
        text += "\n"
    lines = text.splitlines(keepends=True)
    out = []
    for name, start, end, _ in _compose_service_blocks(text):
        body = "".join(lines[start:end])
        m = _SERVICE_IMAGE_RE.search(body)
        kind = _db_kind(m.group(1)) if m else None
        if kind not in ("mysql", "mariadb", "postgres"):
            continue
        env = dict(re.findall(r"^\s+-\s*([A-Z_]+)=(.*)$", body, re.M))
        env.update(dict(re.findall(r"^\s+([A-Z_]+):\s*[\"']?([^\"'\n]*)", body, re.M)))
        # "3307:3306" -> host port 3307; that is what reaches the DB from here.
        pm = re.search(r'^\s+-\s*"?(\d+):(\d+)"?', body, re.M)
        host_port = pm.group(1) if pm else ("3306" if kind != "postgres" else "5432")
        if kind == "postgres":
            user = env.get("POSTGRES_USER") or "postgres"
            pwd = env.get("POSTGRES_PASSWORD") or ""
            db = env.get("POSTGRES_DB") or user
            scheme = "postgres"
        else:
            user = env.get("MYSQL_USER") or "root"
            pwd = env.get("MYSQL_PASSWORD") or env.get("MYSQL_ROOT_PASSWORD") or ""
            db = env.get("MYSQL_DATABASE") or ""
            scheme = "mysql"
        if not db:
            continue
        out.append({"service": name, "engine": scheme, "port": host_port,
                    "url": f"{scheme}://{user}:{pwd}@127.0.0.1:{host_port}/{db}"})
    return out


def _wire_db_healthchecks(sandbox: Path, compose: str, log) -> bool:
    """Hold the app back until the database actually answers.

    `depends_on: [db]` only orders STARTUP, it does not wait for readiness, so
    the app routinely opens a connection while the database is still
    initialising and dies with "Connection refused". It's a race, so it passes
    on a fast machine and fails on a slow one -- which makes it look like a bug
    in the ingested app rather than in how it was booted (hit live while
    testing a Django+Postgres boot: the web container crashed on startup and
    only came up after a manual restart).

    Adds a healthcheck to each recognised database service and upgrades the
    app's depends_on to the condition form that honours it."""
    path = Path(compose)
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return False

    # A file whose last line has no newline defeats every `.*\n` line pattern
    # below. An LLM-written compose routinely ends that way, and the symptom is
    # silent: depends_on parsed as EMPTY, so the app was gated on nothing and
    # the race this function exists to fix stayed wide open (seen live on
    # job_003 — "'app' waits for " with no service named).
    if text and not text.endswith("\n"):
        text += "\n"

    blocks = _compose_service_blocks(text)
    if not blocks:
        return False
    lines = text.splitlines(keepends=True)

    dbs = {}
    app = None
    for name, start, end, indent in blocks:
        body = "".join(lines[start:end])
        m = _SERVICE_IMAGE_RE.search(body)
        kind = _db_kind(m.group(1)) if m else None
        if kind:
            dbs[name] = kind
        elif re.search(r"^\s+build:", body, re.M):
            app = (name, start, end, indent)
    if not dbs or not app:
        return False

    out, changed = [], []
    for name, start, end, indent in blocks:
        body = "".join(lines[start:end])
        pad = " " * indent
        if name in dbs and "healthcheck:" not in body:
            probe = _DB_HEALTHCHECKS[dbs[name]]
            body = body.rstrip("\n") + "\n" + "".join([
                f"{pad}healthcheck:\n",
                f'{pad}  test: ["CMD-SHELL", {json.dumps(probe)}]\n',
                f"{pad}  interval: 5s\n",
                f"{pad}  timeout: 5s\n",
                f"{pad}  retries: 24\n",
                f"{pad}  start_period: 20s\n",
            ])
            changed.append(f"healthcheck on '{name}'")
        if name == app[0] and "condition:" not in body:
            dep = re.search(rf"^{pad}depends_on:\s*\n((?:{pad}\s+-.*\n)*)", body, re.M)
            existing = re.findall(r"-\s*([\w.-]+)", dep.group(1)) if dep else list(dbs)
            waits = "".join(
                f"{pad}  {s}:\n{pad}    condition: "
                f"{'service_healthy' if s in dbs else 'service_started'}\n"
                for s in existing)
            new_dep = f"{pad}depends_on:\n{waits}"
            body = (body[: dep.start()] + new_dep + body[dep.end():]) if dep \
                else body.rstrip("\n") + "\n" + new_dep
            changed.append(f"'{app[0]}' waits for {', '.join(s for s in existing if s in dbs)}")
        out.append(body)

    if not changed:
        return False
    new_text = "".join(lines[: blocks[0][1]]) + "".join(out) + "".join(lines[blocks[-1][2]:])
    try:
        path.write_text(new_text)
    except OSError:
        return False
    log("ok", "readiness-gated the database: " + "; ".join(changed)
             + " — prevents the app racing an uninitialised DB ✓")
    return True


def _wire_framework_db_init(sandbox: Path, compose: str, log) -> bool:
    """Make the app create its own tables on boot.

    Every framework has a step that turns an empty database into a usable one,
    and none of them run it by default in a generated compose. The shape of the
    fix differs per stack -- Spring flips a config profile, Django and Rails run
    a CLI command -- but the failure is identical and identically quiet: the
    boot succeeds, the homepage loads, and only DB-backed pages 500."""
    path = Path(compose)
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    if _wire_spring_profile(sandbox, path, text, log):
        return True
    if _wire_django(sandbox, path, text, log):
        return True
    return _wire_rails(sandbox, path, text, log)


def _wire_spring_profile(sandbox: Path, path: Path, text: str, log) -> bool:
    """Activate the framework's DB profile when the compose wires a real database.

    A Spring app pointed at MySQL purely via SPRING_DATASOURCE_URL connects fine
    and then serves 500s, because the schema is never created: PetClinic keeps
    `spring.sql.init.mode=always` (the setting that runs schema.sql + data.sql)
    in application-mysql.properties, and its default profile also sets
    `ddl-auto=none`, so with no profile active NOTHING creates the tables. The
    boot looks completely successful -- containers up, homepage 200 -- and only
    the pages that touch the missing tables fail, which no build-error self-heal
    can catch (confirmed live: job_002 served /vets.html as a 500 with
    "Table 'petclinic.vets' doesn't exist" while Revive reported success).

    Deterministic on purpose: the LLM that writes the app service reliably gets
    the datasource right and the profile wrong, so this is a post-pass rather
    than more prompt text.
    """
    if "SPRING_PROFILES_ACTIVE" in text or "spring.profiles.active" in text:
        return False

    profiles = _spring_profiles(sandbox)
    if not profiles:
        return False

    # Prefer the DB the app is actually pointed at; fall back to the DB image
    # present in the compose.
    profile = None
    m = _JDBC_RE.search(text)
    if m and m.group(1).lower() in profiles:
        profile = m.group(1).lower()
    if not profile:
        for img in _DB_IMAGE_RE.findall(text):
            name = "postgres" if img.lower().startswith("postgres") else img.lower()
            if name in profiles:
                profile = name
                break
    if not profile:
        return False

    anchor = re.search(r"^(\s*)-\s*SPRING_DATASOURCE_URL=", text, re.M)
    if not anchor:
        log("warn", f"repo ships an '{profile}' Spring profile but the compose has no "
                    "SPRING_DATASOURCE_URL to anchor to — profile not wired")
        return False
    indent = anchor.group(1)
    text = (text[: anchor.start()]
            + f"{indent}- SPRING_PROFILES_ACTIVE={profile}\n"
            + text[anchor.start():])
    try:
        path.write_text(text)
    except OSError:
        return False
    log("ok", f"wired SPRING_PROFILES_ACTIVE={profile} — without it the schema never "
              "loads and DB-backed pages 500 on an otherwise 'successful' boot ✓")
    return True


def _containerize(job: dict, sandbox: Path, recon: dict, log, usage: dict) -> str | None:
    """run-any-codebase style: generate a Dockerfile + compose for an app that has none."""
    port = _pick_port(job["id"])
    ctx = []
    for name in ("package.json", "requirements.txt", "pyproject.toml", "go.mod",
                 "pom.xml", "Gemfile", "composer.json", "Procfile", "main.py", "server.js", "index.js", "app.py"):
        f = _find_first(sandbox, name)
        if f:
            ctx.append(f"FILE {name}:\n{_read(f, 1800)}")
    listing = "\n".join(sorted(os.path.basename(str(p)) for p in list(sandbox.iterdir()))[:40])
    prompt = (
        "Generate a minimal, WORKING Dockerfile and docker-compose.yml to run this app. "
        f"Detected stack: {', '.join(recon.get('stack', [])) or 'unknown'}. "
        f"The web service MUST publish host port {port} mapped to the app's port "
        f"(e.g. \"{port}:3000\"). Use a slim base image, install dependencies, and start the "
        "server. If it needs a database, add that service too. Return EXACTLY:\n"
        "FILE: Dockerfile\n<content>\nFILE: docker-compose.yml\n<content>\nNo prose.\n\n"
        f"ROOT:\n{listing}\n\n" + "\n\n".join(ctx)[:6500])
    log("info", f"containerizing (run-any-codebase) — generating Dockerfile + compose, web port {port}…")
    try:
        result = providers.call_llm(prompt, log, json_mode=False)
    except Exception as e:
        log("err", f"containerize LLM error: {type(e).__name__}")
        return None
    _accumulate_usage(usage, result.get("usage"), log)
    files = _all_parsed_files(result["text"])
    wrote = []
    for rel, content in files.items():
        if _EDITABLE.search(rel) and content.strip():
            _write_build_file(sandbox / os.path.basename(rel), content, log)
            wrote.append(os.path.basename(rel))
    compose = R._find_compose(sandbox)
    if compose and any("compose" in w for w in wrote):
        log("ok", f"generated {', '.join(wrote)} ✓ (web port {port})")
        return compose
    log("warn", "containerize did not produce a usable compose")
    return None


def _augment_missing_app_service(job: dict, sandbox: Path, compose: str, recon: dict,
                                  infra_desc: str, log, usage: dict) -> dict | None:
    """The compose booted clean but only started infra (DB/cache/queue) — the
    app service itself was never in it. This is a different failure shape
    than a build error: `docker compose up` succeeded, so the normal
    error-driven self-heal loop never triggers. Ask the LLM to ADD the
    missing service to the EXISTING compose (reusing its already-working db
    service names/credentials) instead of just reporting the gap."""
    port = _pick_port(job["id"])
    try:
        current = Path(compose).read_text(errors="ignore")
    except OSError:
        return None
    # Root only, not a recursive search: `build: .` in the compose resolves
    # relative to the sandbox root, so a Dockerfile anywhere else (e.g. one
    # meant for .devcontainer/ tooling, unrelated to this compose) is not
    # usable here even though it exists in the repo — confirmed via a live
    # repro against job_024, where a recursive match picked up exactly that
    # kind of irrelevant Dockerfile and silently broke the build.
    has_dockerfile = (sandbox / "Dockerfile").exists()
    ctx = []
    for name in ("package.json", "requirements.txt", "pyproject.toml", "go.mod",
                 "pom.xml", "build.gradle", "Gemfile", "composer.json", "Procfile"):
        f = _find_first(sandbox, name)
        if f:
            ctx.append(f"FILE {name}:\n{_read(f, 1800)}")
    dockerfile_ask = (
        "" if has_dockerfile else
        "This repo has NO Dockerfile — also generate one (multi-stage build if needed for "
        "compiled languages). Return it too, as a second FILE block.\n"
    )
    return_shape = (
        "Return the COMPLETE corrected docker-compose.yml only:\nFILE: docker-compose.yml\n<full file content>\nNo prose."
        if has_dockerfile else
        "Return BOTH files:\nFILE: Dockerfile\n<full file content>\nFILE: docker-compose.yml\n<full file content>\nNo prose."
    )
    prompt = (
        "This docker-compose.yml boots successfully but only starts infrastructure "
        f"({infra_desc}) — the application service itself is missing from it entirely. "
        f"Detected stack: {', '.join(recon.get('stack', [])) or 'unknown'}. "
        "Add ONE new service that builds and runs the actual application from this repo. "
        "Reuse the EXISTING db service name(s), credentials, and database name already in "
        "this compose as the app's connection config (e.g. via environment variables) — do "
        "not change the existing services. If a Dockerfile is needed for a JVM stack, the "
        "`openjdk` and `maven:*-openjdk-*` Docker Hub images are RETIRED and will fail to "
        "pull — use `eclipse-temurin` (e.g. eclipse-temurin:17-jdk / eclipse-temurin:17-jre) "
        "or `maven:3-eclipse-temurin-17` instead. " + dockerfile_ask +
        f"The new service MUST publish host port {port} mapped to the app's real port "
        f"(e.g. \"{port}:8080\"). {return_shape}\n\n"
        f"CURRENT docker-compose.yml:\n{current}\n\n" + "\n\n".join(ctx)[:5000])
    log("info", f"compose is infra-only — asking the LLM to add the missing app service"
                f"{' + Dockerfile' if not has_dockerfile else ''} (web port {port})…")
    try:
        result = providers.call_llm(prompt, log, json_mode=False)
    except Exception as e:
        log("err", f"augment LLM error: {type(e).__name__}")
        return None
    _accumulate_usage(usage, result.get("usage"), log)
    compose_content = _extract_single_file(result["text"], "docker-compose.yml")
    if not compose_content:
        log("warn", "augment did not produce a usable compose")
        return None
    dockerfile_content = ""
    if not has_dockerfile:
        dockerfile_content = _all_parsed_files(result["text"]).get("Dockerfile", "").strip()
        if not dockerfile_content:
            log("warn", "augment needed a Dockerfile but didn't produce one — skipping, would fail to build")
            return None
    # HUMAN-IN-THE-LOOP: same guardrail as the error-driven self-heal — ask before applying.
    from . import chat
    what = ("docker-compose.yml + Dockerfile" if dockerfile_content else "docker-compose.yml")
    choice = chat.ask(job, f"The compose only starts {infra_desc} — I can add the missing "
                      f"application service (rewriting {what}) and retry. Apply it?",
                      choices=["Apply fix", "Skip"], timeout=180, default="Apply fix")
    if choice == "Skip":
        log("warn", "user chose to skip adding the missing app service.")
        return None
    _write_build_file(Path(compose), compose_content, log)
    if dockerfile_content:
        _write_build_file(sandbox / "Dockerfile", dockerfile_content, log)
    log("ok", f"added the missing app service ✓ ({what}, web port {port}) — retrying…")
    return {"file": what, "bytes": len(compose_content) + len(dockerfile_content),
            "type": "added_missing_app_service"}


_FENCE_LINE_RE = re.compile(r"^\s*`{3,}[\w+.-]*\s*$")


def _strip_code_fences(content: str) -> str:
    """Drop markdown fences wrapping an LLM-returned file body.

    Loops instead of one $-anchored substitution: models sometimes close a
    block TWICE, and an anchored strip removes only the final fence. The
    survivor is written into the file verbatim and Docker dies with
    `dockerfile parse error: unknown instruction: ``` ` -- confirmed live on
    job_003, whose generated Dockerfile ended with a stray fence on line 14.
    Only leading/trailing fence-only lines go; anything mid-file is content.
    """
    lines = content.strip().split("\n")
    while lines and _FENCE_LINE_RE.match(lines[0]):
        lines.pop(0)
    while lines and _FENCE_LINE_RE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines).strip()


def _write_build_file(path: Path, content: str, log=None) -> None:
    """Single choke point for every LLM-authored build file.

    Parser-level fence stripping kept missing shapes -- a stray ``` reached the
    Dockerfile twice in a row on job_003 and Docker refused it with "unknown
    instruction: ```", burning a self-heal attempt each time. Rather than keep
    chasing response shapes, sanitise here, where the question is decidable: in
    a Dockerfile or a compose file a line of nothing but backticks is NEVER
    valid content, wherever it appears. Trailing newline included so the
    line-oriented compose rewrites downstream can match the last line.
    """
    cleaned = [ln for ln in _strip_code_fences(content).split("\n")
               if not _FENCE_LINE_RE.match(ln)]
    dropped = len(_strip_code_fences(content).split("\n")) - len(cleaned)
    text = "\n".join(cleaned).rstrip("\n") + "\n"
    path.write_text(text)
    if dropped and log:
        log("warn", f"stripped {dropped} stray markdown fence line(s) from "
                    f"{path.name} — the model left them in its output")


def _parse_files(raw: str) -> dict:
    files = {}
    for part in re.split(r"(?m)^FILE:\s*", raw)[1:]:
        lines = part.split("\n")
        name = lines[0].strip().strip("`")
        content = _strip_code_fences("\n".join(lines[1:]))
        if name:
            files[name] = content
    return files


def _parse_files_loose(raw: str) -> dict:
    """Second-tier fallback for multi-file responses: models often label each
    fenced block with a `# FILE: <name>` COMMENT line inside the fence
    (confirmed live — gpt-4o did exactly this for a Dockerfile+compose
    response) rather than the strict "FILE: <name>" delimiter _parse_files
    expects outside the fence. Read the comment hint, or fall back to the
    fence's language tag + a content sniff."""
    files = {}
    for m in re.finditer(r"```(\w*)\n(.*?)\n```", raw, re.S):
        lang, body = m.group(1).lower(), m.group(2)
        lines = body.split("\n")
        head = lines[0].strip()
        hm = re.match(r"^(?:#|//)\s*(?:FILE:\s*)?([\w.\-]+\.[\w]+|Dockerfile)\b", head, re.I)
        if hm:
            files[hm.group(1)] = _strip_code_fences("\n".join(lines[1:]))
        elif lang == "dockerfile":
            files["Dockerfile"] = _strip_code_fences(body)
        elif lang in ("yaml", "yml") and body.strip().startswith(("services:", "version:")):
            files["docker-compose.yml"] = _strip_code_fences(body)
    return files


def _all_parsed_files(raw: str) -> dict:
    """Strict FILE: delimiter parsing, topped up by the loose fallback for
    names the strict pass missed."""
    strict = _parse_files(raw)
    loose = _parse_files_loose(raw)
    return {**loose, **strict}


def _extract_single_file(raw: str, expected_name: str) -> str:
    """Same job as _parse_files but for prompts that ask for exactly ONE file:
    models frequently drop the "FILE: <name>" prefix when there's only one
    file to return and just answer with a fenced code block (confirmed via a
    live repro against job_024 — gpt-4o did exactly this for a compose-only
    prompt). Try both parsers first, then a bare fenced block, then the raw
    text itself if it plausibly looks like the right kind of content."""
    files = _all_parsed_files(raw)
    if files.get(expected_name, "").strip():
        return _strip_code_fences(files[expected_name])
    m = re.search(r"```(?:ya?ml|dockerfile)?\s*\n(.*?)\n```", raw, re.S | re.I)
    if m and m.group(1).strip():
        return _strip_code_fences(m.group(1))
    stripped = raw.strip()
    if stripped.startswith(("services:", "version:")):
        return _strip_code_fences(stripped)
    return ""


def _find_first(root: Path, name: str):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in ("node_modules", ".git", ".venv", "__pycache__", "dist", "build", "target")]
        if name in fn:
            return os.path.join(dp, name)
    return None


def _read(path, limit=2000):
    try:
        return Path(path).read_text(errors="ignore")[:limit]
    except OSError:
        return ""


# ------------------------------------------------------------- phases ----

def _recon(root: Path, log) -> dict:
    if not RECON.exists():
        return {}
    try:
        p = subprocess.run(["bash", str(RECON), str(root)], capture_output=True, text=True, timeout=60)
        out = p.stdout or ""
    except Exception:
        return {}
    stack = re.findall(r"^\s*([\w./+ ]+?)\s+->\s*present", out, re.M)
    # simpler: grab the "Detected stack" section lines that look like a label
    detected = re.findall(r"\b(Node\.js|Django|Python|JVM|Go|Rust|\.NET|PHP|Ruby)\b", out)
    log("m", "recon: stack " + (", ".join(sorted(set(detected))) or "unidentified"))
    return {"stack": sorted(set(detected)), "report_tail": out.strip().splitlines()[-8:]}


def _self_heal(job: dict, sandbox: Path, compose: str, err: str, log, usage: dict) -> dict | None:
    # collect the build files the LLM may see/fix
    files = {}
    for pat in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml"):
        for f in sandbox.rglob(pat):
            try:
                files[str(f.relative_to(sandbox))] = f.read_text(errors="ignore")[:4000]
            except OSError:
                pass
        if len(files) > 8:
            break
    filelist = "\n\n".join(f"FILE: {k}\n{v}" for k, v in list(files.items())[:8])
    prompt = (
        "A `docker compose up --build` failed. Diagnose the error and fix ONE build "
        "file. Return ONLY:\nFIX: <relative/path>\n<full corrected file content>\n\n"
        "Do not add prose. Only fix Dockerfiles / compose files. Keep the change minimal. "
        "If the error is a missing/unresolvable base image and it's a `openjdk` or "
        "`maven:*-openjdk-*` image, those are RETIRED on Docker Hub — switch to "
        "`eclipse-temurin` (or `maven:3-eclipse-temurin-<version>`), don't just try another "
        "openjdk tag.\n\n"
        f"ERROR (tail):\n{err[-2000:]}\n\nBUILD FILES:\n{filelist}")
    try:
        result = providers.call_llm(prompt, log, json_mode=False)
    except Exception as e:
        log("warn", f"self-heal LLM error: {type(e).__name__}")
        return None
    _accumulate_usage(usage, result.get("usage"), log)
    raw = result["text"]
    m = re.search(r"FIX:\s*(\S+)\s*\n(.*)", raw, re.S)
    if not m:
        return None
    rel, content = m.group(1).strip(), m.group(2)
    # Was a second copy of the same $-anchored strip that let a doubled closing
    # fence through; _write_build_file below is now the authority.
    content = _strip_code_fences(content)
    # GUARDRAIL: only build/config files, inside the sandbox
    if not _EDITABLE.search(rel):
        log("warn", f"self-heal refused to edit non-build file: {rel}")
        return None
    target = (sandbox / rel).resolve()
    if sandbox.resolve() not in target.parents and target != sandbox:
        log("warn", "self-heal refused a path outside the sandbox")
        return None
    # HUMAN-IN-THE-LOOP: ask the user to approve the fix before applying it.
    from . import chat
    err_line = (err.strip().splitlines() or ["build failed"])[-1][:180]
    q = (f"The build failed:\n`{err_line}`\n\nI can fix **{rel}** (a {len(content)}-byte "
         "rewrite) and retry. Apply it?")
    choice = chat.ask(job, q, choices=["Apply fix", "Skip"], timeout=180, default="Apply fix")
    if choice == "Skip":
        log("warn", "user chose to skip the self-heal fix.")
        return None
    try:
        _write_build_file(target, content, log)
    except OSError:
        return None
    log("ok", f"self-heal applied a fix to {rel} — retrying build…")
    return {"file": rel, "bytes": len(content)}


def _gap_scan(base: str, sandbox: Path, log) -> dict:
    if not GAPSCAN.exists():
        return {}
    out = WORK / (sandbox.name + "_gaps.json")
    try:
        subprocess.run([sys.executable, str(GAPSCAN), "--base", base, "--max-pages", "40",
                        "--out", str(out)], capture_output=True, text=True, timeout=120)
        data = json.loads(out.read_text()) if out.exists() else {}
    except Exception:
        return {}
    pages = data.get("pages") or data.get("results") or []
    ok = sum(1 for p in pages if (p.get("status", 200) or 200) < 400)
    broken = [p for p in pages if (p.get("status", 0) or 0) >= 400][:20]
    log("m", f"gap-scan: {ok}/{len(pages)} pages OK, {len(broken)} broken")
    return {"ok": ok, "total": len(pages), "broken": broken}
