"""Helpers for scripts/test-egress-block.sh (egress rule for the private services).

The shell script calls docker/compose and hands the JSON to this module; the module
decides what is allowed. Two subcommands:

  config <merged-config.json>
      The merged compose config must have an `internal: true` `private` network; the
      private services must sit on it and on nothing else and publish no host ports;
      only the proxy pins ports, on the edge + private networks.

  container <service> <private-net> <container-id>
      One running container: reads docker inspect itself; the networks attached must be
      the private network only, and it must publish no host ports.

Exit status 0 when the rule holds, 1 on a violation or missing piece, 2 on use errors.
"""

import json
import subprocess
import sys

PRIVATE_SERVICES = ("api", "ingestion", "qdrant", "ollama", "voice")
PRIVATE_NETWORK_KEY = "private"
PROBE_PROFILE = "egress-test"


def _docker_inspect_json(container, path):
    result = subprocess.run(
        ["docker", "inspect", "--format", path, container],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)


def _ok(msg):
    print(f"  ok: {msg}")


def check_config(path):
    with open(path) as f:
        cfg = json.load(f)
    services = cfg.get("services", {}) or {}
    networks = cfg.get("networks", {}) or {}

    errors = []

    private_net = (networks.get(PRIVATE_NETWORK_KEY) or {}).get("name", PRIVATE_NETWORK_KEY)
    if not (networks.get(PRIVATE_NETWORK_KEY) or {}).get("internal"):
        errors.append("the `private` network is missing or not `internal: true` (no egress rule)")

    present = []
    for name in PRIVATE_SERVICES:
        if name not in services:
            continue
        present.append(name)
        svc = services[name]
        nets = set(svc.get("networks") or {})
        if nets != {PRIVATE_NETWORK_KEY}:
            errors.append(f"`{name}` is on networks {sorted(nets)}, must be exactly [private]")
        ports = svc.get("ports")
        if ports:
            errors.append(f"`{name}` publishes host ports {ports}, must publish none")

    if not present:
        errors.append(f"none of the private services {list(PRIVATE_SERVICES)} in the config")

    proxy = services.get("proxy")
    if proxy:
        nets = set(proxy.get("networks") or {})
        if not {"default", PRIVATE_NETWORK_KEY} <= nets:
            errors.append(
                f"`proxy` is on networks {sorted(nets)}, must join the edge (`default`) and `private`"
            )
        if not proxy.get("ports"):
            errors.append("`proxy` publishes no host ports; nothing would be exposed")
    else:
        errors.append("`proxy` missing; it is the only service allowed to pin ports")

    probe = services.get("egress_probe")
    if probe:
        nets = set(probe.get("networks") or {})
        profiles = set(probe.get("profiles") or [])
        if nets != {PRIVATE_NETWORK_KEY}:
            errors.append(f"`egress_probe` is on networks {sorted(nets)}, must be exactly [private]")
        if PROBE_PROFILE not in profiles:
            errors.append(f"`egress_probe` must carry the `{PROBE_PROFILE}` profile")
    else:
        errors.append("`egress_probe` missing from the merged config")

    known = set(present) | {"proxy", "egress_probe"}
    for other in services:
        if other in known:
            continue
        nets = set(services[other].get("networks") or {})
        if "default" in nets:
            errors.append(
                f"`{other}` is not in the private-service list "
                f"but sits on the routable edge network: {sorted(nets)}"
            )

    if errors:
        print(f"config audit FAILED (private network: {private_net}):")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"config audit PASS (private network: {private_net}):")
    for name in present:
        _ok(f"`{name}` on `{private_net}` only, no published ports")
    _ok("only `proxy` publishes ports (edge + private networks)")
    _ok("`egress_probe` on the private network, behind the `egress-test` profile")
    _ok(f"`{private_net}` is `internal: true` -> Docker gives it no route to the internet")
    return 0


def check_container(service, private_net, cid):
    try:
        nets = set(json.loads(_docker_inspect_json(cid, "{{json .NetworkSettings.Networks}}")))
        ports_raw = _docker_inspect_json(cid, "{{json .NetworkSettings.Ports}}")
    except subprocess.CalledProcessError as e:
        _fail(
            f"docker inspect on `{cid}` failed ({(e.stderr or e).strip().splitlines()[-1] if (e.stderr or e) else 'unknown error'}); "
            "is the container still running?"
        )
        return 1
    try:
        ports = {
            key: value
            for key, value in (json.loads(ports_raw) or {}).items()
            if value
        }
    except (json.JSONDecodeError, AttributeError):
        ports = {}

    ok = True
    if nets != {private_net}:
        _fail(
            f"`{service}` is attached to networks {sorted(nets)}, must be exactly [{private_net}]"
        )
        ok = False
    elif not ports:
        _ok(f"`{service}` attached to `{private_net}` only")
    if ports:
        _fail(f"`{service}` publishes host ports {ports}")
        ok = False
    return 0 if ok else 1


def main(argv):
    usage = (
        "usage: egress_check.py config <config.json> | "
        "container <service> <private-net> <container-id>"
    )
    if len(argv) < 3:
        print(usage, file=sys.stderr)
        return 2
    command = argv[1]
    if command == "config" and len(argv) == 3:
        return check_config(argv[2])
    if command == "container" and len(argv) == 5:
        return check_container(argv[2], argv[3], argv[4])
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
