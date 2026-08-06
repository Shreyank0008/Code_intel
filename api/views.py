import json
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from pathlib import Path
from . import jobs, secrets_store, providers, agents_registry, stage2, stage2_run, chat


def index(request):
    html = (Path(settings.BASE_DIR) / "frontend" / "index.html").read_text()
    return HttpResponse(html)


# Demo-mode ingest allowlist -- kept in exact sync with the frontend's EXAMPLES
# git-repo list. Enforced server-side because hiding the picker in the UI
# alone doesn't stop a direct POST to /api/ingest.
DEMO_ALLOWED_REPOS = {
    "https://github.com/gothinkster/django-realworld-example-app.git",
    "https://github.com/gothinkster/react-redux-realworld-example-app.git",
    "https://github.com/spring-projects/spring-petclinic.git",
    "https://github.com/gothinkster/flask-realworld-example-app.git",
    "https://github.com/gothinkster/node-express-realworld-example-app.git",
    "https://github.com/fastapi/full-stack-fastapi-template.git",
}


_EXAMPLES = [
    {"id": "fastapi-shop", "name": "FastAPI Shop", "dir": "fastapi-shop",
     "stack": ["FastAPI", "REST", "OpenAPI", "UI"], "port": 8801,
     "note": "Best for the full flow — REST API + OpenAPI + a styled storefront. Boots in ~1 min."},
    {"id": "flask-notes", "name": "Flask Notes", "dir": "flask-notes",
     "stack": ["Flask", "Jinja templates"], "port": 8802,
     "note": "Server-rendered templates — shows template detection & template UI."},
    {"id": "express-todo", "name": "Express Todo", "dir": "express-todo",
     "stack": ["Express", "Node", "REST"], "port": 8803,
     "note": "Node/Express API + a static page — JS route detection."},
]


def examples(request):
    base = Path(settings.BASE_DIR) / "examples"
    out = [{**e, "path": str(base / e["dir"])}
           for e in _EXAMPLES if (base / e["dir"]).exists()]
    return JsonResponse({"examples": out})


def config(request):
    prov = providers.active_provider()
    return JsonResponse({
        "allowed_roots": settings.CODEINTEL_ALLOWED_ROOTS,
        "boot_enabled": settings.CODEINTEL_ALLOW_BOOT,
        "provider": prov,
        "ai_available": prov != "heuristic",
        "demo_mode": settings.CODEINTEL_DEMO_MODE,
    })


@csrf_exempt
@require_http_methods(["GET", "POST"])
def settings_api(request):
    if settings.CODEINTEL_DEMO_MODE:
        return JsonResponse({"error": "Settings are not available in this Demo version."}, status=403)
    if request.method == "GET":
        return JsonResponse(secrets_store.masked())
    try:
        patch = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    return JsonResponse(secrets_store.save(patch))


@csrf_exempt
@require_http_methods(["POST"])
def settings_test(request):
    """Ping the configured provider with a tiny prompt to verify the key works."""
    if settings.CODEINTEL_DEMO_MODE:
        return JsonResponse({"error": "Settings are not available in this Demo version."}, status=403)
    logs = []
    prov = providers.active_provider()
    if prov == "heuristic":
        return JsonResponse({"ok": False, "provider": prov,
                             "message": "No provider selected — nothing to test."})
    try:
        txt = providers.call_llm(
            'Reply with only this JSON: {"ok": true}', lambda l, m: logs.append(m))["text"]
        good = "ok" in txt.lower()
        return JsonResponse({"ok": good, "provider": prov,
                             "message": "Provider responded ✓" if good else "Unexpected reply",
                             "sample": txt[:160]})
    except Exception as e:
        return JsonResponse({"ok": False, "provider": prov, "message": str(e)[:300]})


@csrf_exempt
@require_http_methods(["POST"])
def ingest(request):
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    source = (body.get("source") or "").strip()
    stack = body.get("stack") or "Django (DRF)"
    use_llm = bool(body.get("use_llm", True))
    source_type = body.get("source_type") or ("git" if source.startswith("http") else "local")
    revive = bool(body.get("revive", False))
    if not source:
        return JsonResponse({"error": "source path or URL is required"}, status=400)
    if settings.CODEINTEL_DEMO_MODE:
        normalized = source.strip().rstrip("/")
        if normalized not in DEMO_ALLOWED_REPOS:
            return JsonResponse({
                "error": "This Demo version only accepts one of the pre-established public "
                         "example repos — local folders, private repos, archive uploads and "
                         "arbitrary git URLs are disabled.",
            }, status=403)
        source_type = "git"
    job = jobs.create_job(source, stack, use_llm, source_type=source_type, revive=revive)
    return JsonResponse(_public(job), status=201)


def agents_list(request):
    if settings.CODEINTEL_DEMO_MODE:
        return JsonResponse({"error": "LLM Agents are not available in this Demo version."}, status=403)
    return JsonResponse({"agents": agents_registry.get_all(),
                         "provider": providers.active_provider()})


@csrf_exempt
@require_http_methods(["POST"])
def agent_update(request, aid):
    if settings.CODEINTEL_DEMO_MODE:
        return JsonResponse({"error": "LLM Agents are not available in this Demo version."}, status=403)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    try:
        if body.get("reset"):
            return JsonResponse(agents_registry.reset_prompt(aid))
        return JsonResponse(agents_registry.save_prompt(aid, body.get("system_prompt", "")))
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)


def chat_messages(request, jid):
    if settings.CODEINTEL_DEMO_MODE:
        return JsonResponse({"messages": [
            {"role": "assistant", "text": chat.DEMO_NOTICE, "t": "", "actions": []},
        ], "actions": {}})
    return JsonResponse({"messages": chat.history(jid), "actions": chat.ACTIONS})


@csrf_exempt
@require_http_methods(["POST"])
def chat_send(request, jid):
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return JsonResponse({"error": "empty message"}, status=400)
    return JsonResponse(chat.send(jid, text))


@csrf_exempt
@require_http_methods(["POST"])
def chat_answer(request, jid):
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    return JsonResponse(chat.answer(jid, (body.get("answer") or "").strip()))


@csrf_exempt
@require_http_methods(["POST"])
def chat_action(request, jid):
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    return JsonResponse(chat.run_action(jid, body.get("action", "")))


def job_list(request):
    return JsonResponse({"jobs": [_public(j) for j in jobs.list_jobs()]})


def job_detail(request, jid):
    job = jobs.get_job(jid)
    if not job:
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse(_public(job, full=True))


def job_logs(request, jid):
    cursor = int(request.GET.get("cursor", 0))
    lines, new_cursor = jobs.logs_since(jid, cursor)
    job = jobs.get_job(jid)
    return JsonResponse({
        "lines": lines, "cursor": new_cursor,
        "status": job["status"] if job else "unknown",
        "progress": job["progress"] if job else 0,
        "stage": job["stage"] if job else None,
    })


@csrf_exempt
@require_http_methods(["POST"])
def revive_start(request, jid):
    from . import revive_run
    if not settings.CODEINTEL_ALLOW_BOOT:
        return JsonResponse({"error": "boot disabled — start the server with CODEINTEL_ALLOW_BOOT=1"}, status=400)
    return JsonResponse(revive_run.start(jid))


def revive_status(request, jid):
    from . import revive_run
    return JsonResponse(revive_run.status(jid))


@csrf_exempt
@require_http_methods(["POST"])
def revive_stop(request, jid):
    from . import revive as revive_mod
    job = jobs.get_job(jid)
    if not job:
        return JsonResponse({"error": "not found"}, status=404)
    rr = job.get("revive_result") or {}
    if rr.get("compose") and rr.get("cwd"):
        ok = revive_mod.stop(rr["compose"], rr["cwd"])
        rr["status"] = "stopped"
        job["revive_result"] = rr
        return JsonResponse({"ok": ok, "status": "stopped"})
    return JsonResponse({"ok": False, "reason": "nothing running for this job"})


@csrf_exempt
@require_http_methods(["POST"])
def mark_continued(request, jid):
    job = jobs.get_job(jid)
    if not job:
        return JsonResponse({"error": "not found"}, status=404)
    job["continued"] = True
    jobs._persist()
    return JsonResponse({"ok": True, "continued": True})


@csrf_exempt
@require_http_methods(["POST"])
def finalise(request, jid):
    job = jobs.get_job(jid)
    if not job:
        return JsonResponse({"error": "not found"}, status=404)
    if not job.get("result"):
        return JsonResponse({"error": "job has no UCP result to finalise"}, status=400)
    job["continued"] = True
    job["finalised"] = True
    jobs._persist()
    return JsonResponse({"ok": True, "finalised": True})


@csrf_exempt
@require_http_methods(["POST"])
def stage2_build(request, jid, kind):
    job = jobs.get_job(jid)
    if not job:
        return JsonResponse({"error": "not found"}, status=404)
    if not job.get("finalised"):
        return JsonResponse({"error": "finalise UCP & Pricing before building"}, status=400)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    try:
        spec = stage2.build(kind, body.get("answers") or {})
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    job.setdefault("stage2", {})[kind] = spec
    return JsonResponse(spec)


@csrf_exempt
@require_http_methods(["POST"])
def stage2_execute(request, jid, kind):
    job = jobs.get_job(jid)
    if not job:
        return JsonResponse({"error": "not found"}, status=404)
    if not job.get("finalised"):
        return JsonResponse({"error": "finalise UCP & Pricing before building"}, status=400)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    return JsonResponse(stage2_run.start(jid, kind, body.get("answers") or {}))


def stage2_status(request, jid, kind):
    job = jobs.get_job(jid)
    if not job:
        return JsonResponse({"error": "not found"}, status=404)
    slot = (job.get("stage2") or {}).get(kind) or {}
    result = slot.get("result")
    if kind == "pixel" and slot.get("status") == "done":
        # Revive the preview server if a restart killed it, so a finished
        # clone's link keeps working instead of 404ing at the browser.
        from . import pixel_agent
        result = pixel_agent.ensure_clone_serving(jid, result)
        if result is not slot.get("result"):
            slot["result"] = result
    return JsonResponse({
        "status": slot.get("status", "idle"),
        "logs": slot.get("logs", []),
        "result": result,
    })


def _revive_result(j):
    """Revive's result, with the DB connection derived on read if it predates
    that field. Jobs revived before db_url existed still have their compose on
    disk, and re-running a multi-minute boot just to populate one prefill would
    be a poor trade -- read it back out of the compose instead."""
    rr = j.get("revive_result")
    if not isinstance(rr, dict) or rr.get("db_url") or settings.CODEINTEL_DEMO_MODE:
        return rr
    compose = rr.get("compose")
    if not compose or not Path(compose).exists():
        return rr
    try:
        from .revive_agent import _db_urls_from_compose
        dbs = _db_urls_from_compose(compose)
    except Exception:
        return rr
    if not dbs:
        return rr
    return {**rr, "databases": dbs, "db_url": dbs[0]["url"]}


def _public(j, full=False):
    out = {
        "id": j["id"], "source": j["source"], "stack": j["stack"],
        "use_llm": j["use_llm"], "status": j["status"], "progress": j["progress"],
        "stage": j["stage"], "started_h": j["started_h"], "error": j.get("error"),
        "result": j.get("result"), "revive": j.get("revive", False),
        "revive_result": _revive_result(j),
        "continued": j.get("continued", False),
        "finalised": j.get("finalised", False), "stage2": j.get("stage2"),
    }
    if full:
        out["metrics"] = j.get("metrics")
        out["mapping"] = j.get("mapping")
        out["stages"] = j.get("stages", jobs.BASE_STAGES)
    return out
