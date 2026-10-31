# Ansible Deploy

The deployment path assumes this repository is the source of truth and copies the stack onto a dedicated host or VM under `/opt/teacher-agent`.

## Ansible Collection Requirement

Install the required collection before running playbooks:

```bash
ansible-galaxy collection install -r ansible/requirements.yml
```

## Playbooks

- `deploy.yml`: full deploy
- `stop.yml`: stop the stack
- `restart.yml`: restart running containers
- `healthcheck.yml`: call service health endpoints
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
- `selfhosted.ini` targets the self-hosted GitHub Actions runner host (Ansible runs on the same host it deploys to); the CI deploy job uses this
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
