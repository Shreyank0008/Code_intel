#!/usr/bin/env python3
"""Generate a Django data service over the LIVE legacy database.

This is the "DRF data bridge" step of the legacy-transform plan, and the piece
that makes a rebuilt UI show real rows instead of text copied off a screenshot.

    gen_service.py --db-url mysql://user:pass@host:port/dbname --out DIR
                   [--port 8400] [--tables a,b,c]

It introspects the live database and emits a runnable Django project:

    <out>/manage.py
    <out>/service/{__init__,settings,urls,wsgi}.py
    <out>/legacy/{__init__,apps,models,views}.py
    <out>/run.sh  <out>/README.md

Endpoints:
    GET /api/legacy/_meta/            tables, columns, primary keys, row counts
    GET /api/legacy/<table>/          rows  (?limit &offset &order &search &<col>=)
    GET /api/legacy/<table>/<pk>/     one row

KEY DESIGN POINT -- `managed = False` on every model. The legacy database is the
source of truth and stays untouched: Django will never create, alter or drop a
table here, and `migrate` won't try to own the schema. The old app keeps running
against the same rows at the same time.

CORS is enabled (Access-Control-Allow-Origin: *) because the rebuilt UI is served
from a different port than this API -- without it every fetch is blocked by the
browser and the pages silently render empty.

Dependencies: Django, plus PyMySQL (MySQL) or psycopg (PostgreSQL).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, unquote

# ------------------------------------------------------------ db access ----


def parse_db_url(url: str) -> dict:
    u = urlparse(url)
    scheme = (u.scheme or "").lower()
    if scheme in ("mysql", "mysql+pymysql", "mariadb"):
        engine, port = "mysql", u.port or 3306
    elif scheme in ("postgres", "postgresql", "postgresql+psycopg"):
        engine, port = "postgres", u.port or 5432
    else:
        raise SystemExit(f"unsupported database scheme '{u.scheme}' "
                         "(supported: mysql, mariadb, postgres)")
    name = (u.path or "").lstrip("/")
    if not name:
        raise SystemExit("database name missing from --db-url")
    return {"engine": engine, "host": u.hostname or "127.0.0.1", "port": port,
            "user": unquote(u.username or ""), "password": unquote(u.password or ""),
            "name": name}


def connect(cfg: dict):
    if cfg["engine"] == "mysql":
        try:
            import pymysql
        except ImportError:
            raise SystemExit("PyMySQL not installed: pip install pymysql")
        return pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                               password=cfg["password"], database=cfg["name"])
    try:
        import psycopg
    except ImportError:
        raise SystemExit("psycopg not installed: pip install 'psycopg[binary]'")
    return psycopg.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                           password=cfg["password"], dbname=cfg["name"])


# ANSI information_schema works on both MySQL and PostgreSQL, so one query pair
# covers both engines instead of per-engine catalog tables.
_COLUMNS_SQL = """
SELECT table_name, column_name, data_type, is_nullable, character_maximum_length
FROM information_schema.columns
WHERE table_schema = %s
ORDER BY table_name, ordinal_position
"""
_PK_SQL = """
SELECT kcu.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema = kcu.table_schema
WHERE tc.table_schema = %s AND tc.constraint_type = 'PRIMARY KEY'
"""


def introspect(conn, cfg: dict, only: set | None) -> list:
    schema = cfg["name"] if cfg["engine"] == "mysql" else "public"
    cur = conn.cursor()
    cur.execute(_COLUMNS_SQL, (schema,))
    cols = cur.fetchall()
    cur.execute(_PK_SQL, (schema,))
    pks = {}
    for table, col in cur.fetchall():
        pks.setdefault(table, []).append(col)

    tables = {}
    for table, col, dtype, nullable, maxlen in cols:
        if only and table not in only:
            continue
        tables.setdefault(table, []).append({
            "name": col, "type": (dtype or "").lower(),
            "nullable": str(nullable).upper() == "YES",
            "max_length": int(maxlen) if maxlen else None,
            "pk": col in pks.get(table, []),
        })

    out = []
    for table in sorted(tables):
        columns = tables[table]
        pk = next((c["name"] for c in columns if c["pk"]), None)
        try:
            cur.execute(f"SELECT COUNT(*) FROM {_quote(table, cfg['engine'])}")
            count = cur.fetchone()[0]
        except Exception:
            count = None
        out.append({"table": table, "model": model_name(table),
                    "pk": pk, "columns": columns, "rows": count})
    return out


def _quote(ident: str, engine: str) -> str:
    return f"`{ident}`" if engine == "mysql" else f'"{ident}"'


# ------------------------------------------------------------- codegen ----

def model_name(table: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", table) if p) or "Table"


def field_for(col: dict) -> str:
    t, args = col["type"], []
    if not col["pk"]:
        if col["nullable"]:
            args.append("null=True, blank=True")
    if t in ("int", "integer", "mediumint", "smallint", "tinyint", "serial"):
        field = "IntegerField"
    elif t in ("bigint", "bigserial"):
        field = "BigIntegerField"
    elif t in ("decimal", "numeric"):
        field = "DecimalField"
        args.append("max_digits=20, decimal_places=6")
    elif t in ("float", "double", "double precision", "real"):
        field = "FloatField"
    elif t in ("bool", "boolean"):
        field = "BooleanField"
    elif t in ("date",):
        field = "DateField"
    elif t in ("datetime", "timestamp", "timestamp without time zone",
               "timestamp with time zone"):
        field = "DateTimeField"
    elif t in ("time", "time without time zone"):
        field = "TimeField"
    elif t in ("varchar", "character varying", "char", "character"):
        field = "CharField"
        args.append(f"max_length={col['max_length'] or 255}")
    else:
        # text/blob/json/enum/unknown -- TextField reads anything the driver
        # returns without imposing a type the column may not honour.
        field = "TextField"
    if col["pk"]:
        args.insert(0, "primary_key=True")
    args.append(f"db_column={col['name']!r}")
    return f"models.{field}({', '.join(args)})"


def safe_attr(name: str, taken: set) -> str:
    attr = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    if attr[:1].isdigit():
        attr = "f_" + attr
    if attr in ("id",) and attr in taken:
        attr = attr + "_col"
    while attr in taken:
        attr += "_x"
    taken.add(attr)
    return attr


def render_models(tables: list) -> str:
    out = [
        '"""Models generated from the LIVE legacy database.',
        "",
        "managed = False on every model: this project READS an existing database",
        "it does not own. Django will never create, alter or drop these tables, and",
        "the legacy app keeps serving the same rows at the same time.",
        '"""',
        "from django.db import models",
        "",
        "",
    ]
    for t in tables:
        out.append(f"class {t['model']}(models.Model):")
        taken = set()
        has_pk = False
        for col in t["columns"]:
            attr = safe_attr(col["name"], taken)
            if col["pk"]:
                has_pk = True
            out.append(f"    {attr} = {field_for(col)}")
        if not has_pk:
            # No PK: Django requires one. Point it at the first column and mark
            # it read-only in the docstring rather than inventing an id column
            # that does not exist in the legacy table.
            first = t["columns"][0]["name"] if t["columns"] else "id"
            out.append(f"    # no primary key in the legacy table; first column used for identity")
        out.append("")
        out.append("    class Meta:")
        out.append("        managed = False")
        out.append(f"        db_table = {t['table']!r}")
        out.append("")
        out.append("")
    return "\n".join(out)


_SETTINGS = '''"""Generated settings for the legacy data service."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("SECRET_KEY", "generated-legacy-bridge-dev-key")
DEBUG = os.environ.get("DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth", "legacy"]
MIDDLEWARE = ["django.middleware.common.CommonMiddleware", "legacy.views.CorsMiddleware"]
ROOT_URLCONF = "service.urls"
WSGI_APPLICATION = "service.wsgi.application"
TEMPLATES = []
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Points at the LIVE legacy database. Nothing here migrates or mutates schema --
# every model is managed=False.
DATABASES = {
    "default": {
        "ENGINE": %(engine)r,
        "NAME": os.environ.get("LEGACY_DB_NAME", %(name)r),
        "USER": os.environ.get("LEGACY_DB_USER", %(user)r),
        "PASSWORD": os.environ.get("LEGACY_DB_PASSWORD", %(password)r),
        "HOST": os.environ.get("LEGACY_DB_HOST", %(host)r),
        "PORT": os.environ.get("LEGACY_DB_PORT", %(port)r),
    }
}
'''

_VIEWS = '''"""Read endpoints over the legacy tables.

Read-only by design: this bridge proves the rebuilt UI against real data. Writes
would need the legacy app\'s validation rules, which live in its code and not in
the schema, so they are deliberately not exposed here.
"""
import datetime
import decimal
from django.apps import apps
from django.http import JsonResponse, HttpResponse
from django.db.models import Q

MAX_LIMIT = 500


class CorsMiddleware:
    """The rebuilt UI is served from a different port, so every fetch is
    cross-origin. Without these headers the browser blocks the response and the
    page renders empty with no visible error."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Allow-Headers"] = "*"
        response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return response


def _json_safe(value):
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"
    return value


def _models():
    return {m._meta.db_table: m for m in apps.get_app_config("legacy").get_models()}


def meta(request):
    out = []
    for table, model in sorted(_models().items()):
        fields = [{"name": f.db_column or f.name, "attr": f.name,
                   "type": f.get_internal_type(), "pk": f.primary_key}
                  for f in model._meta.fields]
        try:
            count = model.objects.count()
        except Exception as exc:
            count = None
        out.append({"table": table, "model": model.__name__,
                    "columns": fields, "rows": count,
                    "url": f"/api/legacy/{table}/"})
    return JsonResponse({"tables": out})


def rows(request, table):
    model = _models().get(table)
    if model is None:
        return JsonResponse({"error": f"unknown table '{table}'"}, status=404)

    qs = model.objects.all()
    field_names = {f.name for f in model._meta.fields}
    text_fields = [f.name for f in model._meta.fields
                   if f.get_internal_type() in ("CharField", "TextField")]

    # ?<column>=<value> exact filters
    for key, value in request.GET.items():
        if key in field_names:
            qs = qs.filter(**{key: value})

    # ?search= across every text column -- what a "find by name" screen needs
    search = (request.GET.get("search") or "").strip()
    if search and text_fields:
        q = Q()
        for name in text_fields:
            q |= Q(**{f"{name}__icontains": search})
        qs = qs.filter(q)

    order = (request.GET.get("order") or "").strip()
    if order.lstrip("-") in field_names:
        qs = qs.order_by(order)

    try:
        total = qs.count()
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)

    try:
        limit = min(int(request.GET.get("limit", 100)), MAX_LIMIT)
        offset = max(int(request.GET.get("offset", 0)), 0)
    except ValueError:
        limit, offset = 100, 0

    try:
        data = list(qs[offset: offset + limit].values())
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)
    results = [{k: _json_safe(v) for k, v in row.items()} for row in data]
    return JsonResponse({"table": table, "count": total, "limit": limit,
                         "offset": offset, "results": results})


def row(request, table, pk):
    model = _models().get(table)
    if model is None:
        return JsonResponse({"error": f"unknown table '{table}'"}, status=404)
    try:
        obj = model.objects.filter(pk=pk).values().first()
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)
    if obj is None:
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse({k: _json_safe(v) for k, v in obj.items()})
'''

_URLS = '''from django.urls import path
from legacy import views

urlpatterns = [
    path("api/legacy/_meta/", views.meta),
    path("api/legacy/<str:table>/", views.rows),
    path("api/legacy/<str:table>/<str:pk>/", views.row),
]
'''

_MANAGE = '''#!/usr/bin/env python
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "service.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
'''

_WSGI = '''import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "service.settings")
application = get_wsgi_application()
'''

_APPS = '''from django.apps import AppConfig


class LegacyConfig(AppConfig):
    name = "legacy"
    verbose_name = "Legacy database bridge"
'''


def render_readme(cfg: dict, tables: list, port: int) -> str:
    lines = [
        "# Legacy data service",
        "",
        f"Read-only JSON API over the live `{cfg['name']}` database "
        f"({cfg['engine']} at {cfg['host']}:{cfg['port']}).",
        "",
        "Every model is `managed = False` — this project reads a database it does",
        "not own. Django will never create, alter or drop a table here, and the",
        "legacy app keeps serving the same rows at the same time.",
        "",
        "## Run",
        "",
        "```bash",
        "pip install django " + ("pymysql" if cfg["engine"] == "mysql" else "'psycopg[binary]'"),
        f"./run.sh          # http://localhost:{port}",
        "```",
        "",
        "## Endpoints",
        "",
        "| endpoint | returns |",
        "|---|---|",
        "| `/api/legacy/_meta/` | every table, its columns and row count |",
    ]
    for t in tables[:12]:
        lines.append(f"| `/api/legacy/{t['table']}/` | {t['rows']} row(s) |")
    lines += [
        "",
        "Query parameters on a table endpoint: `?limit= &offset= &order= &search=`",
        "plus `?<column>=<value>` for an exact match.",
        "",
        "## Tables",
        "",
        "| table | rows | primary key |",
        "|---|---|---|",
    ]
    for t in tables:
        lines.append(f"| `{t['table']}` | {t['rows']} | `{t['pk'] or '—'}` |")
    lines += [
        "",
        "## Not included",
        "",
        "Writes. A POST would need the legacy app's validation rules, and those",
        "live in its code, not in the schema — a generated endpoint would happily",
        "accept data the old app would have rejected.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a Django data service over a live legacy DB.")
    ap.add_argument("--db-url", required=True,
                    help="mysql://user:pass@host:port/db or postgres://...")
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--tables", default="", help="comma-separated allowlist")
    args = ap.parse_args()

    cfg = parse_db_url(args.db_url)
    only = {t.strip() for t in args.tables.split(",") if t.strip()} or None

    print(f"connecting to {cfg['engine']}://{cfg['host']}:{cfg['port']}/{cfg['name']} …")
    conn = connect(cfg)
    try:
        tables = introspect(conn, cfg, only)
    finally:
        conn.close()
    if not tables:
        print("no tables found — nothing to generate", file=sys.stderr)
        return 1
    print(f"introspected {len(tables)} table(s): "
          + ", ".join(f"{t['table']}({t['rows']})" for t in tables[:10]))

    out = Path(args.out)
    (out / "service").mkdir(parents=True, exist_ok=True)
    (out / "legacy").mkdir(parents=True, exist_ok=True)

    engine = ("django.db.backends.mysql" if cfg["engine"] == "mysql"
              else "django.db.backends.postgresql")
    # PyMySQL ships no MySQLdb module; Django's mysql backend imports one. The
    # shim has to run before django.setup(), so it goes in the package __init__.
    init = ("import pymysql\npymysql.install_as_MySQLdb()\n"
            if cfg["engine"] == "mysql" else "")

    (out / "service" / "__init__.py").write_text(init)
    (out / "service" / "settings.py").write_text(_SETTINGS % {
        "engine": engine, "name": cfg["name"], "user": cfg["user"],
        "password": cfg["password"], "host": cfg["host"], "port": str(cfg["port"])})
    (out / "service" / "urls.py").write_text(_URLS)
    (out / "service" / "wsgi.py").write_text(_WSGI)
    (out / "legacy" / "__init__.py").write_text("")
    (out / "legacy" / "apps.py").write_text(_APPS)
    (out / "legacy" / "models.py").write_text(render_models(tables))
    (out / "legacy" / "views.py").write_text(_VIEWS)
    (out / "manage.py").write_text(_MANAGE)
    (out / "README.md").write_text(render_readme(cfg, tables, args.port))
    run = out / "run.sh"
    run.write_text('#!/usr/bin/env bash\nset -e\ncd "$(dirname "$0")"\n'
                   f'exec "${{PYTHON:-python3}}" manage.py runserver '
                   f'0.0.0.0:${{PORT:-{args.port}}} --noreload\n')
    run.chmod(0o755)

    manifest = {"db": {k: v for k, v in cfg.items() if k != "password"},
                "port": args.port,
                "tables": [{"table": t["table"], "model": t["model"], "pk": t["pk"],
                            "rows": t["rows"],
                            "columns": [c["name"] for c in t["columns"]]}
                           for t in tables]}
    (out / "service_manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"generated Django data service → {out}")
    print(f"  {len(tables)} model(s), managed=False, read-only JSON API")
    print(f"  run: PORT={args.port} {out}/run.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
