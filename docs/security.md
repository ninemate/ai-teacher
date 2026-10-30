# Security

This system handles private library content and private user questions. Treat it as sensitive.

## Required Baseline

- no secrets in git
- `.env.example` only
- Vault or external secret handling for real passwords
- read-only network-share mount by default
- private access path for the UI

## Access Model

Three layers, each useful on its own:

1. **VPN** (WireGuard or Tailscale) is the network path from the phone to the host. See [mobile-access.md](mobile-access.md).
2. **HTTPS** in front of the app: the Caddy container in the compose stack (internal-CA certificate by default, real certs optional; see [https.md](https.md)), or the VPN's own TLS, e.g. Tailscale Serve. This is the baseline, not an option.
3. **App login** built into the API: every page and API route needs a session, except the ones listed below.

### Routes Without Login

| Route | Why |
|---|---|
| `GET /health` | container and Ansible health checks |
| `GET /login`, `POST /login` | the login page itself |
| `/static/*` | CSS and JS for the login page; no private data |

Everything else, including `/`, `/chat`, `/quiz`, `/summary`, `/flashcards`, `/study/*`, `/sources`, `/ingest/*`, `/voice/*`, `/session`, `/logout`, `/docs` and `/openapi.json`, answers `401` without a session (a browser asking for a page is redirected to `/login`). The check is a default-deny middleware, so a new route is protected without any extra code. `tests/test_api_auth.py` walks every registered route to keep it that way.

## App Login

### Users

Users come from configuration, not from a database. Each user is `username:argon2-hash`; several are separated by `;`:

```bash
# .env — single quotes, because the hash contains `$`
TEACHER_AUTH_USERS='alice:$argon2id$v=19$m=65536,t=3,p=4$...;bob:$argon2id$v=19$...'
```

Alternatively, point `TEACHER_AUTH_USERS_FILE` at a file inside the api container (for example a mounted Docker secret) with one `username:hash` per line; `#` starts a comment. Both sources are merged.

Usernames may contain letters, digits, `.`, `_` and `-`. A malformed entry, or a plaintext password in place of a hash, stops the API from starting.

Make a hash:

```bash
make hash-password            # runs `python auth.py` in the api image, asks for the password twice
```

or, with `argon2-cffi` installed locally:

```bash
python -c "import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass.getpass()))"
```

To change a password, replace the hash and restart the api container. Sessions created with the old password stop working. Removing a user ends their sessions too.

If `TEACHER_AUTH_USERS` is empty the app stays closed: nobody can log in, and the API logs a warning at startup. `TEACHER_AUTH_DISABLED=true` turns the login off entirely; use it only for a throwaway local experiment, never on a host reachable from another device.

### Sessions

- After login the browser gets a random `teacher_session` cookie. Only its SHA-256 is stored, in the `auth_sessions` table of the SQLite metadata database, so a copy of the database does not hand out live sessions.
- The cookie is `HttpOnly`, `SameSite=Lax` and `Path=/`. It lasts `TEACHER_SESSION_TTL_HOURS` (default 720, 30 days, so the phone does not ask every day).
- `Secure`: with `TEACHER_COOKIE_SECURE=auto` (default) the cookie is marked Secure when the request came over HTTPS, either directly or through a proxy that sets `X-Forwarded-Proto: https` (the compose proxy does, and replaces any value a client sent). Set `TEACHER_COOKIE_SECURE=true` once the app is only reached through HTTPS; `prod` does.
- Logout (`POST /logout`, the "Kijelentkezés" button) deletes the session on the server, not only in the browser.
- Sessions survive an API restart. To log everyone out, delete the rows from `auth_sessions` or change the passwords.

### CSRF

- Each session has its own CSRF token. The UI reads it from the page (`<meta name="csrf-token">`) or from `GET /session`, and sends it as `X-CSRF-Token` on every `POST`, `PUT`, `PATCH` and `DELETE`. Requests without a matching token get `403`.
- The login form uses a double-submit token: a short-lived `teacher_login_csrf` cookie that must match the hidden form field.
- Any state-changing request whose `Origin` header names a different host than the request's `Host` is rejected with `403`.
- `SameSite=Lax` keeps the session cookie off cross-site `POST`s as a further layer.

### Login Rate Limiting

Failed logins are counted per client address and per (existing) username in a sliding window: `TEACHER_LOGIN_MAX_ATTEMPTS` (default 10) within `TEACHER_LOGIN_WINDOW_SECONDS` (default 900). Once either limit is hit, the login page answers `429` with `Retry-After` and a Hungarian message, even for the right password, until the window passes. A successful login clears that user's counter.

An attempt is counted before its password is checked, in the same step as the limit check, so a burst of parallel guesses gets exactly `TEACHER_LOGIN_MAX_ATTEMPTS` password checks, not one per request. The limits must be at least 1; `0` stops the API from starting.

Notes:

- The counter lives in the API process's memory. The API runs as a single process; a restart clears the counters.
- Behind Caddy all requests arrive from the proxy's address, so the per-address limit is shared by everyone using that proxy. With a small user list that is acceptable; the per-user limit still applies.
- An attacker who can reach the login page can lock a user out for a window. The VPN keeps that population small.
- Unknown usernames cost the same password-hash check as wrong passwords, so response time does not reveal which usernames exist.

### Response Headers

Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin` and a `Content-Security-Policy` that allows only same-origin scripts, styles and connections (`/docs` and `/redoc` are exempt from the CSP because Swagger loads from a CDN; they still need login). Everything except static files is `Cache-Control: no-store`, so answers do not linger in the phone's browser cache. HSTS belongs on the HTTPS terminator; the compose proxy sends it (off by default until real certs are used, see [https.md](https.md)) and so does the Ansible Caddy template.

## Ingestion Service

The ingestion service is published on the host only on `127.0.0.1:${TEACHER_INGEST_PORT}` and inside the compose network. Its `/ingest/status`, `/ingest/schedule` and `/ingest/reindex` endpoints require `Authorization: Bearer ${TEACHER_INGEST_TOKEN}`; `/health` stays open for health checks.

- Logged-in users reach `GET /ingest/schedule` and `POST /ingest/reindex` through the API, which adds the token, so users never handle it. The API's `GET /ingest/status` reads the last run from the shared SQLite file. A reindex during a running (for example scheduled) run answers `409`; an unreachable ingestion service answers `502`.
- If `TEACHER_INGEST_TOKEN` is empty the endpoints refuse every call (`503`). Generate one with `openssl rand -hex 32`.
- The Ansible deploy keeps the token already in the host's `.env`, or generates one on first deploy, unless `teacher_ingest_token` is set. `reindex.yml` and `make test-ingest` read it from `.env`.

```bash
curl -X POST http://127.0.0.1:8081/ingest/reindex \
  -H "Authorization: Bearer $TEACHER_INGEST_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"full_reindex": false, "remove_missing": true}'
```

## Voice Service

The optional `voice` container has no published port and no authentication of its own, like Qdrant and Ollama: only containers on the Compose network reach it. Browsers reach it only through the API's `/voice/status`, `/voice/transcribe` and `/voice/speak`, which need a session, and the POSTs also need the CSRF token.

- Uploads are capped at `VOICE_MAX_UPLOAD_MB` in both the API and the voice service, and recordings at `VOICE_STT_MAX_SECONDS`. Text to read is capped at `VOICE_TTS_MAX_CHARS`.
- The client's file name is never used as a path: only its extension is passed on, as a decoder hint, and uploads go to a private temporary directory that is deleted after transcription.
- A voice can only be chosen from `VOICE_TTS_VOICES`, so a request cannot point the loader at another file.
- Recordings and transcripts are not stored or logged; the log has only durations and timings.
- The UI plays the generated audio from `blob:` URLs, which the CSP allows with `media-src 'self' blob:`.

## Exposure Rules

- Keep Qdrant internal.
- Keep Ollama internal.
- Keep the voice service internal.
- Keep the ingestion port on `127.0.0.1`.
- With the compose proxy, set `TEACHER_HTTP_BIND=127.0.0.1` so only the HTTPS port is reachable from other devices.
- Prefer VPN for remote access, with HTTPS and app login on top.
- Never run with `TEACHER_AUTH_DISABLED=true` on a reachable host.

## Secrets

| Secret | Where |
|---|---|
| `TEACHER_AUTH_USERS` (argon2 hashes) | `.env` on the host; Ansible var `teacher_auth_users` from Vault; GitHub secret `TEACHER_AUTH_USERS` for the self-hosted deploy |
| `TEACHER_INGEST_TOKEN` | `.env` on the host, generated by the deploy |
| network-share credentials | Ansible Vault |

Hashes are not passwords, but treat them as secrets anyway: an offline guess against a weak password is cheap.

## Logging

- User prompts and retrieved snippets may be sensitive.
- Avoid shipping logs to third-party services without explicit consent.
- Rotate logs and keep retention short unless needed.
- Passwords, session tokens and the ingest token are never logged by the app.

## Local Storage Privacy

The local SSD stores:

- embeddings
- chunk text payloads
- SQLite metadata, including session hashes
- model cache
- logs

That means the host itself becomes part of the trust boundary.
