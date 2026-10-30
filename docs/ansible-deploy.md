# Ansible Deploy

The deployment path assumes this repository is the source of truth and copies the stack onto a separate VM under `/opt/teacher-agent`.

## Ansible Collection Requirement

Install the required collection before running playbooks:

```bash
ansible-galaxy collection install -r ansible/requirements.yml
```

## Playbooks

- `deploy.yml`: full deploy
- `stop.yml`: stop the stack
- `restart.yml`: restart running containers
- `healthcheck.yml`: wait for container health, then run the egress audit (containers on the internal network only, no ports, live probe; see docs/security.md)
- `mount-share.yml`: mount the library share only
- `reindex.yml`: trigger manual reindex

## Default Directories

- Stack files: `/opt/teacher-agent`
- Runtime data: `/srv/teacher-agent`
- Network share mount: `/mnt/library`

## Example

```bash
ansible-playbook -i ansible/inventory/dev.ini ansible/playbooks/deploy.yml
```

## Inventory Strategy

- `dev.ini` is local-machine friendly
- `selfhosted.ini` targets the self-hosted GitHub Actions runner VM (Ansible runs on the same host it deploys to); the CI deploy job uses this
- `prod.example.ini` is a template only
- real production inventory and vault files stay out of git

Group variables live in `ansible/inventory/group_vars/` (next to the inventories). Each inventory adds its hosts to the `teacher` group and then maps a matching child group (`dev`, `selfhosted`, `prod`) so the corresponding `group_vars/<group>.yml` is loaded. Roles live in `ansible/playbooks/roles/` so role resolution works regardless of the working directory.

## Secrets

Use Ansible Vault or external secret injection for:

- network share credentials
- app login users: `teacher_auth_users`, `username:argon2-hash` entries separated by `;` (see `docs/security.md`)
- reverse proxy credentials

The CI deploy to the self-hosted runner reads `teacher_auth_users` from the GitHub secret `TEACHER_AUTH_USERS`. Until that secret is set the app starts but nobody can log in.

`teacher_ingest_token` can stay empty: the deploy keeps the token already in the host's `.env`, or generates one on the first deploy. `reindex.yml` reads it from there.

## GPU Path

If `teacher_gpu_enabled=true`, the deploy role adds the NVIDIA Compose overlay and can optionally install the NVIDIA Container Toolkit role.

## Network

The deployment always includes the compose proxy (`docker-compose.proxy.yml`): the private services sit on an internal network, publish no host ports, and the proxy is the only entry point (`TEACHER_PROXY_HOST`/`TEACHER_PROXY_BIND`/`TEACHER_HTTPS_PORT` set from `group_vars`; see docs/security.md). `teacher_enable_reverse_proxy=true` additionally installs a host-level Caddy in front of the stack proxy (real certificates etc.); it forwards to `https://127.0.0.1:{{ teacher_https_port }}` and skips the stack's internal CA.
