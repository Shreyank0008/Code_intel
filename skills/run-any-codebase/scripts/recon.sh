#!/usr/bin/env bash
# Stack recon for an ingested codebase -- "what is this, and how would it boot?"
#
#   recon.sh <root>
#
# Emits a plain-text report on stdout. api/revive_agent.py::_recon parses two
# things out of it, so the format is load-bearing:
#   * marker lines matching   ^\s*<name>\s+->\s*present
#   * stack names matching    \b(Node\.js|Django|Python|JVM|Go|Rust|\.NET|PHP|Ruby)\b
# Keep prose free of those bare words so the detector doesn't pick up narration.
#
# Read-only: this only stats and greps files. It never builds, installs or runs
# anything in the target tree.
set -uo pipefail

ROOT="${1:-.}"
if [ ! -d "$ROOT" ]; then
  echo "recon: not a directory: $ROOT" >&2
  exit 1
fi
cd "$ROOT" || exit 1

# Bounded find: skip dependency/build trees, cap depth so a huge monorepo can't
# stall the 60s budget the caller allows.
PRUNE='-name node_modules -o -name .git -o -name venv -o -name .venv -o -name target -o -name dist -o -name build -o -name vendor -o -name __pycache__'

has() {  # has <glob-name> -> prints the first match path, empty if none
  find . -maxdepth 4 \( $PRUNE \) -prune -o -name "$1" -type f -print 2>/dev/null | head -1
}

marker() {  # marker <label> <glob>
  local hit; hit="$(has "$2")"
  if [ -n "$hit" ]; then
    printf '  %-28s -> present   (%s)\n' "$1" "${hit#./}"
    return 0
  fi
  printf '  %-28s -> absent\n' "$1"
  return 1
}

echo "=============================================="
echo " recon: $(pwd)"
echo "=============================================="
echo
echo "-- build / dependency markers --"

STACKS=()
add() { STACKS+=("$1"); }

marker "package.json"        "package.json"        && add "Node.js"
marker "requirements.txt"    "requirements.txt"    && add "Python"
marker "pyproject.toml"      "pyproject.toml"      && add "Python"
marker "Pipfile"             "Pipfile"             && add "Python"
marker "manage.py"           "manage.py"           && add "Django"
marker "pom.xml"             "pom.xml"             && add "JVM"
marker "build.gradle"        "build.gradle"        && add "JVM"
marker "go.mod"              "go.mod"              && add "Go"
marker "Cargo.toml"          "Cargo.toml"          && add "Rust"
marker "composer.json"       "composer.json"       && add "PHP"
marker "Gemfile"             "Gemfile"             && add "Ruby"
marker "*.csproj"            "*.csproj"            && add ".NET"
marker "Dockerfile"          "Dockerfile"
marker "docker-compose.yml"  "docker-compose.yml"
marker "docker-compose.yaml" "docker-compose.yaml"
marker "Makefile"            "Makefile"
marker ".env.example"        ".env.example"

# Django can hide behind a bare requirements.txt with no manage.py at the root.
if [ -z "$(has manage.py)" ] && grep -rqiE '^[[:space:]]*django([=<>~[]|$)' \
     requirements.txt pyproject.toml Pipfile 2>/dev/null; then
  add "Django"
fi

echo
echo "-- detected stack --"
if [ ${#STACKS[@]} -eq 0 ]; then
  echo "  unidentified (no recognised build manifest at depth <= 4)"
else
  printf '%s\n' "${STACKS[@]}" | sort -u | sed 's/^/  /'
fi

echo
echo "-- entrypoint candidates --"
find . -maxdepth 3 \( $PRUNE \) -prune -o -type f \
     \( -name "main.*" -o -name "app.py" -o -name "server.js" -o -name "index.js" \
        -o -name "wsgi.py" -o -name "asgi.py" -o -name "manage.py" \) -print 2>/dev/null \
  | head -12 | sed 's|^\./|  |'

echo
echo "-- declared ports --"
{
  grep -rhoE '^[[:space:]-]*"?[0-9]{2,5}:[0-9]{2,5}"?' docker-compose.y*ml 2>/dev/null | tr -d ' "-'
  grep -rhoE 'EXPOSE[[:space:]]+[0-9]{2,5}' Dockerfile 2>/dev/null | awk '{print $2}'
} | sort -u | head -10 | sed 's/^/  /'

echo
echo "-- size --"
FILES=$(find . \( $PRUNE \) -prune -o -type f -print 2>/dev/null | wc -l | tr -d ' ')
echo "  tracked-ish files: $FILES"

echo
echo "recon complete"
