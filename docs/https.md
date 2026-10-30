# HTTPS Reverse Proxy

`make dev-up` starts a Caddy container (`proxy`, from `compose/docker-compose.proxy.yml`) in front of the API and web UI. It terminates TLS, redirects plain HTTP to HTTPS, adds security headers and forwards to `api:8000`. Config: `compose/proxy/Caddyfile`, driven by the env vars below.

| Address | What |
|---|---|
| `https://${TEACHER_PROXY_HOST}:${TEACHER_HTTPS_PORT}` | the UI and API (default `https://localhost:8443`) |
| `http://${TEACHER_PROXY_HOST}:${TEACHER_PROXY_HTTP_PORT}` | redirects (`308`) to the HTTPS address, keeping path and query (default port `8088`) |
| `http://${TEACHER_PROXY_HOST}:${TEACHER_PROXY_HTTP_PORT}/ca.crt` | Caddy's root certificate, for installing on a phone (only with `TEACHER_TLS=internal`) |

Caddy was chosen over nginx because it needs no certificate tooling for the default self-signed setup and its `reverse_proxy` sets the forwarded headers for us.

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `TEACHER_PROXY_HOST` | `localhost` | Name or IP clients type in the address bar. The certificate is issued for exactly this. For a phone use the host's VPN or LAN name/IP. |
| `TEACHER_PROXY_BIND` | `127.0.0.1` | Interface the proxy listens on. `0.0.0.0` (or the VPN interface address) to reach it from other devices. |
| `TEACHER_HTTPS_PORT` / `TEACHER_PROXY_HTTP_PORT` | `8443` / `8088` | Host ports (unprivileged, so rootless Docker works). Use `443` / `80` if you want bare URLs. |
| `TEACHER_TLS` | `internal` | `internal` = certificate from Caddy's local CA. Otherwise `<cert> <key>` paths inside `/certs`. |
| `TEACHER_TLS_DIR` | `../certs` | Host directory mounted read-only at `/certs` (relative to `compose/`, so `certs/` in the repo root; git-ignored). |
| `TEACHER_HSTS` | `max-age=0` | `Strict-Transport-Security` value. See below. |

With the proxy running, set `TEACHER_HTTP_BIND=127.0.0.1` so the API's own plain-HTTP port (8080) is not reachable from other devices. Set `TEACHER_COOKIE_SECURE=true` once you only use the HTTPS address.

## Default: internal CA (self-signed)

Caddy creates its own root and intermediate CA on first start and issues the leaf certificate for `TEACHER_PROXY_HOST` from it (works for names and IP addresses; renewed automatically). The CA lives in the `caddy_data` volume, so the certificate stays stable across restarts. If you recreate that volume the CA changes and every device must trust the new root.

Changing `TEACHER_PROXY_HOST` re-issues the leaf; devices that already trust the root need nothing new.

### Trust the certificate on this computer

Browsers show a warning until the root is trusted. Download it and import it into your OS/browser trust store:

```bash
curl -o teacher-ca.crt http://localhost:8088/ca.crt
# or: docker compose --env-file .env -f compose/docker-compose.yml -f compose/docker-compose.proxy.yml \
#       cp proxy:/data/caddy/pki/authorities/local/root.crt teacher-ca.crt
```

Check it is the right file before trusting it: `openssl x509 -in teacher-ca.crt -noout -subject -fingerprint -sha256` (subject `Caddy Local Authority - <year> ECC Root`). Compare the fingerprint with the one on the host if you fetched it over the network.

### Trust it on a phone

Trust only the root, and only on devices that you own. Anyone holding this root's private key can impersonate sites to that device, and the key stays inside the `caddy_data` volume on the host, so protect that volume.

1. On the host set `.env`: `TEACHER_PROXY_HOST=<VPN or LAN IP/name of the host>`, `TEACHER_PROXY_BIND=0.0.0.0` (or the VPN interface address) and restart: `make dev-up`.
2. On the phone, connected to the VPN, open `http://<host>:8088/ca.crt` in the browser (plain HTTP is fine here: the file is public, and you verify its fingerprint). It downloads `teacher-ca.crt`.
3. Install it:
   - **iPhone/iPad**: Settings → "Profile Downloaded" → Install (enter passcode). Then Settings → General → About → Certificate Trust Settings → switch on "Caddy Local Authority". Both steps are required.
   - **Android**: Settings → Security (or "Security & privacy") → More security settings → Encryption & credentials → Install a certificate → **CA certificate** → "Install anyway" → pick the file. Wording differs by vendor; searching Settings for "CA certificate" finds it.
4. Open `https://<host>:8443` in the phone browser. The padlock should be clean; log in as usual.

Notes for Android: Chrome and the system browser use the user CA store, but some apps and Firefox for Android do not (Firefox: Settings → About → tap logo 5× → Secret Settings → "Use third party CA certificates", or use Chrome). Also, Android shows a persistent "network may be monitored" notice once a user CA is installed; that is expected.

If the phone shows a name mismatch, `TEACHER_PROXY_HOST` is not the address you typed (IP vs name, or a different VPN address). Use exactly the same string on both sides.

## Real certificates

Use this when the host has a real name, for example from a public DNS name or an internal CA that the devices already trust (then nothing needs installing on phones).

1. Put the certificate chain (leaf first) and private key in `certs/` in the repo root, or point `TEACHER_TLS_DIR` elsewhere. Keep the key readable only by you; it is mounted read-only into the container.
2. In `.env`:

   ```bash
   TEACHER_PROXY_HOST=teacher.example.internal    # a name in the certificate
   TEACHER_TLS=/certs/fullchain.pem /certs/privkey.pem
   TEACHER_HSTS=max-age=31536000
   ```

3. `make dev-up` (recreates the proxy). Renewal is up to you: replace the files and run `docker compose ... restart proxy`.

Other ways to get a trusted certificate without touching phones: Tailscale (`tailscale cert <name>.ts.net` gives a publicly trusted certificate for the tailnet name; use those files as above) or a DNS-challenge certificate from Let's Encrypt for an internal name.

## Headers and forwarding

- Caddy adds `Strict-Transport-Security`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy`, and removes `Server`. The API adds its own CSP and `Cache-Control: no-store` (see [security.md](security.md)).
- **HSTS** is `max-age=0` by default. Browsers remember HSTS per host name, not per port: a real HSTS header on `localhost` would make the browser force HTTPS on `http://localhost:8080` too and break plain-HTTP dev. Switch it on (`max-age=31536000`) once you use real certificates on a dedicated name.
- The request `Host` is passed through unchanged, so the API's CSRF `Origin`/`Host` check works. `X-Forwarded-Proto: https`, `X-Forwarded-For` and `X-Forwarded-Host` are set by Caddy and overwrite anything the client sent, so the API's `Secure` cookie logic (`TEACHER_COOKIE_SECURE=auto`) cannot be fooled from outside.
- Behind the proxy the API sees every request coming from the proxy container, so the per-address login rate limit is shared; see [security.md](security.md).
- HTTP/3 is disabled (the UDP port is not published); HTTP/1.1 and HTTP/2 are on.
- Request bodies are capped at 100 MB (voice uploads). There is no response timeout, because LLM answers can take minutes.

## Checking it

```bash
make compose-config                      # merged config, includes the proxy
curl -sI http://localhost:8088/          # 308 -> https://localhost:8443/
curl --cacert teacher-ca.crt -sI https://localhost:8443/health   # 200, with security headers
docker compose ... logs proxy            # certificate and request log
```

In a browser, log in and check in dev tools → Application → Cookies that `teacher_session` has `Secure`, `HttpOnly` and `SameSite=Lax`.

## Production

The Ansible deploy does not use this overlay; it installs Caddy on the host (`reverse_proxy_optional` role). Wire the overlay into the deploy when the real deployment is planned, and then make sure the host Caddy and the container do not both claim ports 80/443.
