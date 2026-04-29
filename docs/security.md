# Security

This system handles private library content and private user questions. Treat it as sensitive.

## Required Baseline

- no secrets in git
- `.env.example` only
- Vault or external secret handling for real passwords
- read-only network-share mount by default
- private access path for the UI

## Exposure Rules

- Keep Qdrant internal.
- Keep Ollama internal.
- Prefer VPN for remote access.
- If exposed publicly, require HTTPS and authentication.

## Logging

- User prompts and retrieved snippets may be sensitive.
- Avoid shipping logs to third-party services without explicit consent.
- Rotate logs and keep retention short unless needed.

## Local Storage Privacy

The local SSD stores:

- embeddings
- chunk text payloads
- SQLite metadata
- model cache
- logs

That means the host itself becomes part of the trust boundary.

