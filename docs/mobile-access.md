# Mobile Access

The simplest acceptable user experience is a phone browser opening the teacher UI over a private path.

## Preferred Access Modes

1. `WireGuard`
2. `Tailscale` or `headscale`

These give network-level access without exposing the UI directly to the public internet.

On top of the VPN, the app is served over HTTPS by the Caddy container in compose and asks for a login (see [security.md](security.md)). Setup, including installing the certificate on iPhone and Android, is in [https.md](https.md).

## Alternative

Use `Caddy` with HTTPS if VPN is not acceptable. The app login still applies; Caddy's own basic auth is an optional extra layer (`teacher_caddy_username`, `teacher_caddy_password_hash`).

## Certificate on the Phone

The default certificate comes from Caddy's own CA, so each phone must trust that root once: open `http://<host>:8088/ca.crt` on the phone, install it, then browse to `https://<host>:8443`. Steps for iOS and Android are in [https.md](https.md#trust-it-on-a-phone). With a real certificate (for example from Tailscale) nothing needs installing.

## Login on the Phone

- The first visit shows a Hungarian login page ("Bejelentkezés"): username, password, "Belépés".
- The form uses the standard `username` / `current-password` autocomplete hints, so the phone's password manager can save and fill it.
- A login lasts 30 days by default (`TEACHER_SESSION_TTL_HOURS`), so the phone rarely asks again. Adding the page to the home screen keeps the same session.
- "Kijelentkezés" at the top of the page logs out on the server too.
- After 10 wrong passwords in 15 minutes the login page asks to wait before trying again.

## Non-Goals

- No unauthenticated UI, even on the VPN
- No direct public Qdrant or Ollama exposure

## User Flow

1. Father opens the phone browser.
2. He connects through VPN or a protected HTTPS entrypoint.
3. He logs in once on that phone.
4. He types a Hungarian question.
5. He gets a Hungarian answer and source list.

