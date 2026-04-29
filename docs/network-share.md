# Network Share

The teacher-agent VM reads documents from a mounted library share. Source files stay on the share and are mounted read-only by default.

## Supported Share Types

- `cifs`
- `nfs`

## Main Variables

- `share_type`
- `share_server`
- `share_path`
- `teacher_mount_point`
- `mount_options`
- `teacher_share_credentials_file`
- `read_only`

## CIFS Example

```yaml
share_type: cifs
share_server: 192.168.1.10
share_path: /ebooks
teacher_mount_point: /mnt/library
read_only: true
teacher_share_credentials_file: /root/.smb-teacher-agent
```

## NFS Example

```yaml
share_type: nfs
share_server: 192.168.1.20
share_path: /srv/exports/library
teacher_mount_point: /mnt/library
read_only: true
mount_options: vers=4,soft,timeo=30
```

## Security Notes

- Keep the share mounted read-only unless writes are truly needed.
- Never commit share credentials.
- Prefer Vault-backed credentials variables.
- Limit the exported share to only the required library paths.

