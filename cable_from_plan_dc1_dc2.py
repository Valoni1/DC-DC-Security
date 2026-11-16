#!/usr/bin/env python3
"""
cable_from_plan_dc1_dc2.py

Makes NetBox's cabling match the "links:" list in ip_plan_dc1_dc2.yaml: every
router-to-router link in the plan gets a NetBox cable if it does not have one.
Use it after adding a link to the plan, such as the second DCI link
(dc1-dci-r1 ethernet-1/4 <-> dc2-dci-r1 ethernet-1/4).

For each link in the plan:
  - both ends already cabled to each other  -> left alone
  - both ends free                          -> cable created
  - either end cabled somewhere else        -> reported, left alone
Host cables come from add_hosts_dc1_dc2.py, not from here.

Addresses are not set here: run assign_ips_dc1_dc2.py afterwards, which reads
the same plan.

REQUIREMENTS:
  pip install pynetbox pyyaml   (already in your venv)

USAGE:
  export NETBOX_URL="http://localhost:8000"
  export NETBOX_TOKEN="your-api-token-here"
  python3 cable_from_plan_dc1_dc2.py --dry-run     # see what would be created
  python3 cable_from_plan_dc1_dc2.py               # create it
"""

import os
import sys
import argparse

try:
    import pynetbox
    import yaml
except ImportError:
    print("pynetbox and pyyaml are needed in this Python environment.")
    print("Run: pip install pynetbox pyyaml   (inside your venv)")
    sys.exit(1)


NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")

PLAN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ip_plan_dc1_dc2.yaml")


def fail(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)


def connect():
    if not NETBOX_URL:
        fail("NETBOX_URL environment variable is not set.")
    if not NETBOX_TOKEN:
        fail("NETBOX_TOKEN environment variable is not set.")
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    try:
        list(nb.dcim.sites.all())
    except Exception as e:
        fail(f"Could not connect to NetBox at {NETBOX_URL}: {e}")
    return nb


def cabled_to(nb, iface):
    """(device, interface) at the other end of iface's cable, or None."""
    if iface.cable is None:
        return None
    peers = dict(iface).get("link_peers") or []
    if not peers:
        return ("?", "?")
    return (peers[0]["device"]["name"], peers[0]["name"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created; make no changes.",
    )
    args = parser.parse_args()

    with open(PLAN_FILE) as f:
        links = yaml.safe_load(f).get("links") or []
    if not links:
        fail(f"No links listed under 'links:' in {PLAN_FILE}.")

    nb = connect()
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    devices = {}
    to_create = []
    ok = conflicts = 0
    for link in links:
        ends = []
        for side in ("a", "b"):
            name = link[side]
            if name not in devices:
                devices[name] = nb.dcim.devices.get(name=name)
                if not devices[name]:
                    fail(f"Device {name} is not in NetBox. Run rename_dc1_dc2.py first.")
            iface = nb.dcim.interfaces.get(device_id=devices[name].id, name=link[f"{side}_if"])
            if not iface:
                fail(f"{name} has no interface {link[f'{side}_if']} in NetBox.")
            ends.append(iface)
        label = f'{link["a"]}:{link["a_if"]} <-> {link["b"]}:{link["b_if"]}'
        a_peer, b_peer = cabled_to(nb, ends[0]), cabled_to(nb, ends[1])
        if a_peer == (link["b"], link["b_if"]):
            ok += 1
        elif a_peer is None and b_peer is None:
            print(f"  NEW: {label}")
            to_create.append((label, ends))
        else:
            print(f"  CONFLICT: {label} -- a end goes to {a_peer}, b end goes to {b_peer}; left alone")
            conflicts += 1

    print(f"\nAlready cabled as planned: {ok}   To create: {len(to_create)}   Conflicts: {conflicts}")
    if args.dry_run or not to_create:
        if args.dry_run:
            print("--dry-run set: no changes made.")
        return

    created = failed = 0
    for label, (a_iface, b_iface) in to_create:
        try:
            nb.dcim.cables.create(
                a_terminations=[{"object_type": "dcim.interface", "object_id": a_iface.id}],
                b_terminations=[{"object_type": "dcim.interface", "object_id": b_iface.id}],
                status="connected",
            )
            print(f"  OK: {label}")
            created += 1
        except Exception as e:
            print(f"  FAILED: {label} -- {e}")
            failed += 1

    print(f"\nCreated: {created}   Failed: {failed}")
    print(
        "\nNext: python3 assign_ips_dc1_dc2.py, redeploy the lab (./deploy_lab.sh), "
        "./5_run_config_mgmt.sh"
    )


if __name__ == "__main__":
    main()
