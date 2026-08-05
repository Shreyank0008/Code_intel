#!/usr/bin/env python3
"""Schema drift: tables the CODE uses vs tables the SCHEMA defines.

The classic legacy-migration trap is a table that every query references but no
committed DDL creates -- it exists only in a production database nobody has the
migration for. Port that codebase to a fresh schema and it dies on first query.
This finds those before the port, not after.

    schema_diff.py --source DIR --json FILE --md FILE

Two inventories, harvested statically:
  DEFINED   CREATE TABLE (.sql), Django/SQLAlchemy models, Rails schema.rb,
            JPA @Table/@Entity, migration files
  REFERENCED  FROM / JOIN / INSERT INTO / UPDATE / DELETE FROM in any source file

EXIT CODE = the number of confidently-missing tables (referenced, never
defined), capped at 200. api/stage2_run.py reads the return code straight into
``confident_missing``, so this deliberately exits non-zero on a real finding --
that is a result, not an error. A genuine crash exits 250+.

"Confident" excludes SQL keywords, CTE names bound by a WITH in the same file,
and derived-table aliases -- the things that look like unknown tables but aren't.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

MAX_FILES = 6000
MAX_BYTES = 800_000
EXIT_CAP = 200
CRASH_EXIT = 250

SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build",
    "target", "vendor", ".next", ".nuxt", "coverage", ".tox", "site-packages",
}
SCAN_EXT = {
    ".sql", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".java", ".rb", ".php",
    ".go", ".cs", ".xml", ".yml", ".yaml", ".erb", ".jsp",
}

# -------------------------------------------------------------- defined ----

CREATE_TABLE_RE = re.compile(
    r"""\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[]?([\w.]+)[`"\]]?""", re.I)
DJANGO_META_TABLE_RE = re.compile(r"""\bdb_table\s*=\s*["']([\w.]+)["']""")
SQLALCHEMY_TABLE_RE = re.compile(r"""\b__tablename__\s*=\s*["']([\w.]+)["']""")
SQLALCHEMY_OBJ_RE = re.compile(r"""\bTable\(\s*["']([\w.]+)["']""")
RAILS_SCHEMA_RE = re.compile(r"""\bcreate_table\s+["':]([\w.]+)["']?""")
JPA_TABLE_RE = re.compile(r"""@Table\s*\(\s*name\s*=\s*["']([\w.]+)["']""")
JPA_ENTITY_RE = re.compile(r"""@Entity\b[\s\S]{0,200}?\bclass\s+(\w+)""")
DJANGO_MODEL_RE = re.compile(r"""^\s*class\s+(\w+)\s*\(\s*(?:models\.)?Model\b""", re.M)

# ----------------------------------------------------------- referenced ----

REF_RE = re.compile(
    r"""\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+[`"\[]?([\w.]+)[`"\]]?""", re.I)
CTE_RE = re.compile(r"""\bWITH\s+(\w+)\s+AS\s*\(|\)\s*,\s*(\w+)\s+AS\s*\(""", re.I)
ALIAS_RE = re.compile(r"""\)\s*(?:AS\s+)?(\w+)\b""", re.I)

# CRITICAL: REF_RE must only ever see genuine SQL.
#
# Run it over whole source files and English prose starts naming tables: PetClinic's
# javadoc "Retrieve all Vets from the data store." yields tables `the` and `data`,
# and a comment reading "...from your database" yields `your`. That produced 9
# bogus "confidently missing" tables on a codebase whose real answer is zero.
# So references are harvested ONLY from:
#   * .sql files (comments stripped)
#   * string literals in code that actually parse as SQL/JPQL
#   * XML text nodes that parse as SQL (MyBatis/iBatis mappers)
# Comments in code files are excluded for free -- they are never string literals.

# Quoted strings across the languages we scan, including Python/Java text blocks.
STRING_LIT_RE = re.compile(
    r'"""(?P<a>.*?)"""'      # python / java text block
    r"|'''(?P<b>.*?)'''"
    r'|"(?P<c>(?:\\.|[^"\\])*)"'
    r"|'(?P<d>(?:\\.|[^'\\])*)'"
    r"|`(?P<e>(?:\\.|[^`\\])*)`",
    re.S)
# A SQL verb AND its mandatory clause -- "select a shipping option from the list"
# has no FROM-clause structure that survives both halves of this test.
SQL_SHAPE_RE = re.compile(
    r"""\bSELECT\b[\s\S]{1,4000}?\bFROM\b"""
    r"""|\bINSERT\s+INTO\b"""
    r"""|\bUPDATE\b[\s\S]{1,200}?\bSET\b"""
    r"""|\bDELETE\s+FROM\b""", re.I)
SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
SQL_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
XML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
XML_TEXT_RE = re.compile(r">([^<>]{12,4000})<")

# ORM references that never spell out SQL: the entity IS the table reference.
ORM_REF_RES = (
    # Spring Data: interface VetRepository extends JpaRepository<Vet, Integer>
    re.compile(r"""\b(?:Jpa|Crud|PagingAndSorting|Mongo|R2dbc)Repository\s*<\s*(\w+)"""),
    re.compile(r"""\bRepository\s*<\s*(\w+)\s*,"""),
    # Django:  Owner.objects.filter(...)
    re.compile(r"""\b([A-Z]\w+)\.objects\b"""),
    # SQLAlchemy:  session.query(Owner)
    re.compile(r"""\bquery\(\s*([A-Z]\w+)\s*\)"""),
    # JPA criteria:  from(Owner.class)
    re.compile(r"""\bfrom\(\s*(\w+)\.class\s*\)"""),
)

# Words that follow FROM/JOIN in real SQL but never name a table.
SQL_NOISE = {
    "select", "where", "group", "order", "having", "limit", "offset", "union",
    "left", "right", "inner", "outer", "cross", "full", "on", "as", "set",
    "values", "dual", "lateral", "unnest", "generate_series", "table", "only",
    "distinct", "all", "case", "when", "then", "else", "end", "using", "natural",
}


def camel_to_snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def norm(table: str) -> str:
    """Strip schema qualifier and quoting so public.users == users."""
    t = table.strip().strip('`"[]').lower()
    return t.rsplit(".", 1)[-1]


def walk(root: Path):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if Path(fn).suffix.lower() in SCAN_EXT:
                yield Path(dirpath) / fn
                n += 1
                if n >= MAX_FILES:
                    return


def read(p: Path) -> str:
    try:
        if p.stat().st_size > MAX_BYTES:
            return ""
        return p.read_text(errors="ignore")
    except OSError:
        return ""


def sql_regions(path: Path, text: str):
    """Yield only the parts of a file that are genuinely SQL/JPQL."""
    ext = path.suffix.lower()
    if ext == ".sql":
        body = SQL_BLOCK_COMMENT_RE.sub(" ", text)
        yield SQL_LINE_COMMENT_RE.sub(" ", body)
        return
    if ext in (".xml", ".yml", ".yaml"):
        body = XML_COMMENT_RE.sub(" ", text)
        for m in XML_TEXT_RE.finditer(body):
            chunk = m.group(1)
            if SQL_SHAPE_RE.search(chunk):
                yield chunk
        return
    for m in STRING_LIT_RE.finditer(text):
        lit = next((g for g in m.groups() if g), "")
        if lit and SQL_SHAPE_RE.search(lit):
            yield lit


def harvest(root: Path):
    defined: dict = {}       # table -> [where defined]
    entities: dict = {}      # lowercased entity/model name -> table name
    raw_refs: list = []      # (raw name, rel path) -- resolved after the walk
    files = 0

    def add(bag, name, rel, extra=""):
        key = norm(name)
        if not key or key in SQL_NOISE or key.isdigit() or len(key) < 2:
            return
        bag.setdefault(key, [])
        tag = rel + (f" ({extra})" if extra else "")
        if tag not in bag[key]:
            bag[key].append(tag)

    for path in walk(root):
        text = read(path)
        if not text:
            continue
        files += 1
        rel = str(path.relative_to(root))
        is_migration = "migration" in rel.lower() or "migrate" in rel.lower()

        # ---- definitions ----
        for m in CREATE_TABLE_RE.finditer(text):
            add(defined, m.group(1), rel,
                "CREATE TABLE" + (" · migration" if is_migration else ""))
        for rx, label in ((DJANGO_META_TABLE_RE, "db_table"),
                          (SQLALCHEMY_TABLE_RE, "__tablename__"),
                          (SQLALCHEMY_OBJ_RE, "Table()"),
                          (RAILS_SCHEMA_RE, "create_table"),
                          (JPA_TABLE_RE, "@Table")):
            for m in rx.finditer(text):
                add(defined, m.group(1), rel, label)

        # Entity/model classes: the framework derives a table name from the class.
        # Record the class->table mapping so a JPQL "FROM PetType" later resolves
        # to the pet_type table instead of being reported as an unknown one.
        # Scope the @Table/db_table lookup to the matched annotation block, not
        # the whole file -- two entities in one file would otherwise both claim
        # the first @Table name and silently collapse into one table.
        for m in JPA_ENTITY_RE.finditer(text):
            cls = m.group(1)
            explicit = JPA_TABLE_RE.search(m.group(0))
            table = norm(explicit.group(1)) if explicit else camel_to_snake(cls)
            entities[cls.lower()] = table
            add(defined, table, rel, "@Entity")
        for m in DJANGO_MODEL_RE.finditer(text):
            cls = m.group(1)
            # class Meta follows the model body, so look ahead from the match to
            # the next top-level class rather than back over the whole file.
            body = text[m.end(): m.end() + 4000].split("\nclass ", 1)[0]
            explicit = DJANGO_META_TABLE_RE.search(body)
            table = norm(explicit.group(1)) if explicit else camel_to_snake(cls)
            entities[cls.lower()] = table
            add(defined, table, rel, "Django model")

        # ---- references (SQL contexts only) ----
        for region in sql_regions(path, text):
            local_noise = set()
            for m in CTE_RE.finditer(region):
                local_noise.add(norm(m.group(1) or m.group(2) or ""))
            for m in ALIAS_RE.finditer(region):
                local_noise.add(norm(m.group(1)))
            for m in REF_RE.finditer(region):
                t = norm(m.group(1))
                if t and t not in local_noise and t not in SQL_NOISE:
                    raw_refs.append((t, rel))

        # ---- references with no SQL at all (repository/ORM handles) ----
        for rx in ORM_REF_RES:
            for m in rx.finditer(text):
                raw_refs.append((m.group(1), rel))

    # Resolve entity names to their tables now that every entity is known.
    referenced: dict = {}
    for raw, rel in raw_refs:
        key = norm(raw)
        key = entities.get(key, key)
        add(referenced, key, rel)

    return defined, referenced, entities, files


def write_markdown(dest: Path, data: dict) -> None:
    d = data
    lines = [
        "# Schema diff",
        "",
        f"Source: `{d['source']}`  ",
        f"Scanned {d['files_scanned']} file(s).",
        "",
        "| | count |",
        "|---|---|",
        f"| Tables defined in schema | {len(d['defined'])} |",
        f"| Tables referenced in code | {len(d['referenced'])} |",
        f"| **Referenced but never defined** | **{len(d['confident_missing'])}** |",
        f"| Defined but never referenced | {len(d['unused'])} |",
        "",
    ]
    if d["confident_missing"]:
        lines += ["## Referenced but never defined", "",
                  "These are queried by the code with no committed DDL creating them. "
                  "A port to a fresh schema will fail on first use.", "",
                  "| table | used in |", "|---|---|"]
        for t in d["confident_missing"]:
            where = ", ".join(f"`{w}`" for w in d["referenced"][t][:4])
            lines.append(f"| `{t}` | {where} |")
        lines.append("")
    else:
        lines += ["## Referenced but never defined", "",
                  "None — every referenced table has a definition in the source. ✓", ""]

    if d["unused"]:
        lines += ["## Defined but never referenced", "",
                  "Candidates for dead schema — verify before dropping; they may be "
                  "reached by raw SQL this scan can't see (string-built queries, "
                  "stored procedures, external jobs).", ""]
        lines += [f"- `{t}`" for t in d["unused"][:60]]
        lines.append("")
    dest.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Diff referenced vs defined DB tables.")
    ap.add_argument("--source", required=True)
    ap.add_argument("--json", dest="json_out", required=True)
    ap.add_argument("--md", dest="md_out", required=True)
    args = ap.parse_args()

    root = Path(args.source).expanduser().resolve()
    if not root.is_dir():
        print(f"source dir not found: {root}", file=sys.stderr)
        return CRASH_EXIT

    defined, referenced, entities, files = harvest(root)
    missing = sorted(t for t in referenced if t not in defined)
    unused = sorted(t for t in defined if t not in referenced)

    data = {
        "source": str(root),
        "files_scanned": files,
        "defined": {k: v[:6] for k, v in sorted(defined.items())},
        "referenced": {k: v[:6] for k, v in sorted(referenced.items())},
        "confident_missing": missing,
        "unused": unused,
        "entities": entities,
        "counts": {
            "defined": len(defined),
            "referenced": len(referenced),
            "confident_missing": len(missing),
            "unused": len(unused),
        },
    }

    jd, md = Path(args.json_out), Path(args.md_out)
    jd.parent.mkdir(parents=True, exist_ok=True)
    md.parent.mkdir(parents=True, exist_ok=True)
    jd.write_text(json.dumps(data, indent=2), encoding="utf-8")
    write_markdown(md, data)

    print(f"scanned {files} file(s)")
    print(f"defined:    {len(defined)} table(s)")
    print(f"referenced: {len(referenced)} table(s)")
    print(f"confident missing: {len(missing)}")
    for t in missing[:15]:
        print(f"  ! {t}  (used in {', '.join(referenced[t][:2])})")
    if unused:
        print(f"defined but unused: {len(unused)}")
        for t in unused[:10]:
            print(f"  · {t}")
    print(f"report → {md}")
    return min(len(missing), EXIT_CAP)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # a crash must not be mistaken for a missing-table count
        print(f"schema_diff failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(CRASH_EXIT)
