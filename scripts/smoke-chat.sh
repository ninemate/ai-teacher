#!/bin/sh
# Log in with TEACHER_USER / TEACHER_PASSWORD and ask one question, the way the browser does.
# Goes to the HTTPS reverse proxy by default (the private services publish no host ports);
# CURL_INSECURE=1 trusts the proxy's internal-CA certificate with curl -k.
set -eu
base_url="${1:-https://localhost:8443}"
: "${TEACHER_USER:?set TEACHER_USER}"
: "${TEACHER_PASSWORD:?set TEACHER_PASSWORD}"
curl_opts=""
if [ "${CURL_INSECURE:-0}" = "1" ]; then curl_opts="-k"; fi

jar="$(mktemp)"
trap 'rm -f "$jar"' EXIT

login_csrf="$(curl -fsS $curl_opts -c "$jar" "$base_url/login" | sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p')"
printf '%s' "$TEACHER_PASSWORD" | curl $curl_opts -sS -o /dev/null -b "$jar" -c "$jar" -X POST "$base_url/login" \
  --data-urlencode "username=$TEACHER_USER" --data-urlencode "password@-" --data-urlencode "csrf_token=$login_csrf"
csrf="$(curl -fsS $curl_opts -b "$jar" "$base_url/session" | sed -n 's/.*"csrf_token":"\([^"]*\)".*/\1/p')"

curl -fsS $curl_opts -b "$jar" -X POST "$base_url/chat" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
  -d '{"question":"Foglald össze röviden, hogyan működik a RAG.", "level":"beginner"}'
