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
- `prod.example.ini` is a template only
- real production inventory and vault files stay out of git

## Secrets

Use Ansible Vault or external secret injection for:

- network share credentials
- app auth password
- reverse proxy credentials

## GPU Path

If `teacher_gpu_enabled=true`, the deploy role adds the NVIDIA Compose overlay and can optionally install the NVIDIA Container Toolkit role.
