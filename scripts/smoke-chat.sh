#!/bin/sh
# Log in with TEACHER_USER / TEACHER_PASSWORD and ask one question, the way the browser does.
set -eu
base_url="${1:-http://127.0.0.1:8080}"
: "${TEACHER_USER:?set TEACHER_USER}"
: "${TEACHER_PASSWORD:?set TEACHER_PASSWORD}"

jar="$(mktemp)"
trap 'rm -f "$jar"' EXIT

login_csrf="$(curl -fsS -c "$jar" "$base_url/login" | sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p')"
printf '%s' "$TEACHER_PASSWORD" | curl -sS -o /dev/null -b "$jar" -c "$jar" -X POST "$base_url/login" \
  --data-urlencode "username=$TEACHER_USER" --data-urlencode "password@-" --data-urlencode "csrf_token=$login_csrf"
csrf="$(curl -fsS -b "$jar" "$base_url/session" | sed -n 's/.*"csrf_token":"\([^"]*\)".*/\1/p')"

curl -fsS -b "$jar" -X POST "$base_url/chat" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
  -d '{"question":"Foglald össze röviden, hogyan működik a RAG.", "level":"beginner"}'
