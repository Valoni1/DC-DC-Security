#!/usr/bin/env python3
"""
configure_hosts_dc1_dc2.py

Gives every end host in the running DC1-DC2 lab its address and routes from
the "hosts:" list in ip_plan_dc1_dc2.yaml. configure.py only configures the
SR Linux routers; the hosts are plain containers, so this script sets them up
with `docker exec`.

On each host container it:
  - sets the planned address on the leaf-facing interface (eth1)
  - routes the lab's ranges via the leaf: 172.20.0.0/16 (loopbacks),
    172.30.0.0/16 (host LANs) and 10.1.0.0/16 (fabric links)
The default route stays on eth0, the containerlab management network.

Windows hosts are a Windows VM inside a container in NAT mode: the container
holds the planned address and forwards traffic for it to the VM, and the VM's
own traffic leaves NATed to that address. Their first boot installs Windows,
which takes a while; the container's network can be set up straight away.

Safe to re-run: addresses and routes are replaced, not added twice.

USAGE (on the lab host, after the lab is deployed):
  python3 configure_hosts_dc1_dc2.py --dry-run   # print the commands
  python3 configure_hosts_dc1_dc2.py             # run them
"""

import os
import sys
import argparse
import ipaddress
import subprocess

try:
    import yaml
except ImportError:
    print("pyyaml is needed in this Python environment.")
    print("Run: pip install pyyaml   (inside your venv)")
    sys.exit(1)


PLAN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ip_plan_dc1_dc2.yaml")
LAB_PREFIX = "clab-DC1-DC2-"
LAB_ROUTES = ["172.20.0.0/16", "172.30.0.0/16", "10.1.0.0/16"]


def host_commands(host):
    gateway = ipaddress.ip_interface(host["leaf_ip"]).ip
    dev = host["host_if"]
    cmds = [
        f"ip link set {dev} up",
        f"ip addr flush dev {dev}",
        f"ip addr add {host['host_ip']} dev {dev}",
    ]
    cmds += [f"ip route replace {r} via {gateway} dev {dev}" for r in LAB_ROUTES]
    return cmds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print the commands; run nothing.")
    parser.add_argument("--plan", default=PLAN_FILE, help="IP plan file")
    parser.add_argument("--prefix", default=LAB_PREFIX, help="containerlab container name prefix")
    args = parser.parse_args()

    with open(args.plan) as f:
        hosts = yaml.safe_load(f).get("hosts") or []
    if not hosts:
        print(f"No hosts listed under 'hosts:' in {args.plan}.")
        sys.exit(1)

    done = failed = 0
    for host in hosts:
        container = args.prefix + host["name"]
        script = " && ".join(host_commands(host))
        print(f"{container}: {host['host_ip']} via {host['leaf']}")
        if args.dry_run:
            print(f"  docker exec {container} sh -c '{script}'")
            continue
        r = subprocess.run(
            ["docker", "exec", container, "sh", "-c", script],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            done += 1
        else:
            print(f"  FAILED: {(r.stderr or r.stdout).strip()}")
            failed += 1

    if not args.dry_run:
        print(f"\nConfigured: {done}   Failed: {failed}")
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
