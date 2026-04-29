# Mobile Access

The simplest acceptable user experience is a phone browser opening the teacher UI over a private path.

## Preferred Access Modes

1. `WireGuard`
2. `Tailscale` or `headscale`

These give network-level access without exposing the UI directly to the public internet.

## Alternative

Use `Caddy` with HTTPS and authentication if VPN is not acceptable.

## Non-Goals

- No unauthenticated public UI
- No direct public Qdrant or Ollama exposure

## User Flow

1. Father opens the phone browser.
2. He connects through VPN or a protected HTTPS entrypoint.
3. He types a Hungarian question.
4. He gets a Hungarian answer and source list.

