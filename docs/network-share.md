# Network Share

The teacher-agent host reads documents from a mounted library share. Source files stay on the share and are mounted read-only by default.

## Supported Share Types

- `cifs`
- `nfs`

## Main Variables

- `share_type`
- `share_server`
- `share_path`
- `teacher_mount_point`
- `mount_options` (extra options, appended to the ones below)
- `teacher_share_credentials_file`
- `read_only`
- `teacher_share_smb_version` (CIFS dialect, default `3.0`)
- `teacher_share_file_mode`, `teacher_share_dir_mode` (CIFS, read-only mounts; default `0444`/`0555`, so the containers can read whatever user they run as)
- `teacher_share_uid`, `teacher_share_gid` (optional numeric owner of the mounted files)
- `teacher_share_automount`, `teacher_share_mount_timeout`
- `teacher_mount_propagation`, `teacher_library_sentinel`

## CIFS Example

```yaml
share_type: cifs
share_server: nas.example.lan
share_path: /ebooks
teacher_mount_point: /mnt/library
read_only: true
teacher_share_credentials_file: /root/.smb-teacher-agent
```

## NFS Example

```yaml
share_type: nfs
share_server: 192.0.2.20
share_path: /srv/exports/library
teacher_mount_point: /mnt/library
read_only: true
mount_options: vers=4,soft,timeo=30
```

## Mount Options

The role writes an `/etc/fstab` entry with these options (CIFS adds the ones marked):

| Option | Why |
| --- | --- |
| `ro` | Read-only is the default (`read_only: true`). |
| `_netdev` | Mount after the network is up. |
| `nofail` | A NAS that is down at boot does not stop the host from booting. |
| `x-systemd.mount-timeout=30` | Bounds the wait for a slow NAS. |
| `x-systemd.automount` | Mount on first access instead of at boot (`teacher_share_automount`, on by default). The mount point is then always a live automount point: a NAS that reboots or appears late is picked up on the next access, and never leaves an empty local folder that looks like an empty library. |
| `soft` | Reads fail with an error instead of hanging forever when the NAS goes away. |
| `nosuid,nodev,noexec` | The library is data only. |
| `vers=3.0` (CIFS) | Explicit SMB dialect; do not rely on negotiation. Lower it (`2.1`) only for an old server. |
| `iocharset=utf8` (CIFS) | Hungarian file names. |
| `file_mode=0444,dir_mode=0555` (CIFS, read-only) | Files stay readable for the non-root container user. Set `teacher_share_uid`/`teacher_share_gid` if you want a specific owner. |
| `credentials=<file>` (CIFS) | Username and password in a root-only file (`0600`), written from Vault-backed variables. Nothing is committed. |

With `teacher_share_automount: false` the role mounts the share immediately instead. If you are switching an existing host from a plain mount to the automount, unmount it once first (`umount /mnt/library`) so systemd can place the automount unit there.

The role also drops `/etc/systemd/system/docker.service.d/teacher-library.conf` (`After=remote-fs.target`): Docker starts after the remote filesystems have been tried. This is ordering only, there is no hard dependency, so a NAS that is down never keeps Docker from starting.

## Bind-Mount Timing

The containers bind-mount the library from the host (`compose/docker-compose.yml`). A plain bind mount captures whatever is at the path when the container starts, so a share mounted afterwards would stay hidden behind the empty folder.

What is used: the bind mount has `propagation: rslave` (`TEACHER_MOUNT_PROPAGATION`) and `create_host_path: false`, and the mount point is an automount. Why this and not only ordering:

- With the automount, the mount point is an automount point from boot. The container binds that, and the real share mounts underneath it on first access and propagates into the container through `rslave`. No ordering between Docker and the share is needed, and a remount after a NAS reboot reaches the containers too.
- A systemd ordering dependency alone (`RequiresMountsFor=`) would also work for the boot case, but it cannot help when the NAS comes up after the containers, and a hard `Requires=` would keep Docker down while the NAS is down. Only the soft `After=remote-fs.target` ordering is used.
- `create_host_path: false` makes Docker fail loudly if the folder does not exist, instead of creating an empty one that looks like an empty library.

Needs: Linux, with the host mount point under a shared mount (the default on systemd hosts, `findmnt -o TARGET,PROPAGATION /` shows `shared`). Where that is not the case (Docker Desktop, a rootless setup) set `TEACHER_MOUNT_PROPAGATION=rprivate` and start the stack after mounting the share.

Even so, the ingestion service does not trust the mount to be there; see below.

## When the Library Is Absent

An unmounted share leaves an empty folder, which looks like a library whose files were all deleted. The ingestion service therefore never treats a missing library as a clean-up:

- A run is **skipped** (status `skipped`, the reason in `message` of `GET /ingest/status`, nothing indexed or removed, a full reindex does not wipe the index first) when a watched folder is missing, or when `LIBRARY_SENTINEL` is set and that file is not in the library. Create the sentinel once on the share (for example an empty `.teacher-library` file in its root) and set `LIBRARY_SENTINEL=.teacher-library` (`teacher_library_sentinel` in Ansible). A mount point check does not work from inside a container: a bind-mounted folder is always a mount point there.
- A watched folder that is completely empty although it held indexed documents is kept as before.
- A run that would remove at least 5 documents and more than `INGEST_MAX_REMOVAL_FRACTION` (default `0.5`) of those indexed in the watched folders removes nothing and says so in the run message. A manual reindex with `"confirm_removal": true` carries it out. Scheduled runs never confirm. Set the fraction to `1` to turn this off.

Stored quizzes, flashcards and citations depend on indexed documents, which is why the index is protected this way.

## Without Ansible

Any way of getting the share mounted on the host works; the stack only needs `TEACHER_MOUNT_POINT` to point at it.

By hand (CIFS, read-only):

```bash
sudo apt install cifs-utils            # or your distribution's equivalent
sudo mkdir -p /mnt/library
printf 'username=USER\npassword=PASSWORD\n' | sudo tee /root/.smb-teacher > /dev/null
sudo chmod 600 /root/.smb-teacher
sudo mount -t cifs //nas.example.lan/ebooks /mnt/library \
  -o ro,credentials=/root/.smb-teacher,vers=3.0,iocharset=utf8,file_mode=0444,dir_mode=0555,soft,nosuid,nodev,noexec
```

Permanently, in `/etc/fstab` (one line):

```
//nas.example.lan/ebooks  /mnt/library  cifs  ro,credentials=/root/.smb-teacher,vers=3.0,iocharset=utf8,file_mode=0444,dir_mode=0555,soft,nosuid,nodev,noexec,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=30  0  0
```

then `sudo systemctl daemon-reload && sudo systemctl start mnt-library.automount`. Set `TEACHER_MOUNT_POINT=/mnt/library` in `.env`, and optionally `LIBRARY_SENTINEL`.

Verify (on the host, from the repository):

```bash
make check-library
```

It checks that the mount point is a real mount, is readable and not empty, has the sentinel (if configured) and contains every folder under `watch.folders` in `config/ingestion/ingestion.yml`. It exits non-zero on any problem. `REQUIRE_MOUNTPOINT=0 make check-library` accepts a plain local folder.

## Not Verified Without a Real Share

The mount options, the automount unit and `rslave` propagation into the containers depend on a real NAS, Docker and systemd; only the ingestion guard, the check script and the playbook syntax are covered by automated checks. After the first real deployment, check: the share mounts after a reboot with the NAS down and then up; `docker compose exec ingestion ls /library` shows the files; and a reindex while the NAS is unplugged reports `skipped` and removes nothing.

## Security Notes

- Keep the share mounted read-only unless writes are truly needed.
- Never commit share credentials.
- Prefer Vault-backed credentials variables.
- Limit the exported share to only the required library paths.

