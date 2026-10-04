#!/usr/bin/env python3
"""
assign_ips_dc1_dc2.py

Fills in the IP addresses config_mgmt/configure.py needs but that
restructure_dc1_dc2.py and cable_dc1_dc2.py do not create: a loopback
(primary IPv4) on the new superspines and DCI routers, and a /31 on both
ends of every new cable, including the DC1 <-> DC2 DCI link. Without these,
configure.py skips the device and reports what is missing.

ADDRESS PLAN (only used where NetBox has nothing yet):
  Loopbacks (interface lo0, set as the device's primary IPv4), next to the
  existing 172.20.N.11-12 spine and 172.20.N.101-106 leaf loopbacks:
    dc-gateway    172.20.N.1, .2, ...
    superspine    172.20.N.21, .22, ...
    spine         172.20.N.11, ...
    leaf          172.20.N.101, ...
  Point-to-point /31s:
    links inside DCN    10.1.N.0/24
    DCI link            10.1.0.0/24

Existing addresses are never changed. An address already present anywhere
in NetBox is never handed out again. A link where only one end has an IP is
reported and left alone, since that needs a human decision.

REQUIREMENTS:
  pip install pynetbox   (already in your venv)

USAGE:
  export NETBOX_URL="http://localhost:8000"
  export NETBOX_TOKEN="your-api-token-here"
  python3 assign_ips_dc1_dc2.py --dry-run     # see the plan, no changes
  python3 assign_ips_dc1_dc2.py               # actually create the IPs
"""

import os
import re
import sys
import argparse
import ipaddress

try:
    import pynetbox
except ImportError:
    print("pynetbox is not installed in this Python environment.")
    print("Run: pip install pynetbox   (inside your venv)")
    sys.exit(1)


NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")

TARGET_SITE_NAMES = ["DC1", "DC2"]
DEMO_TAG_NAME = "demo"

LOOPBACK_INTERFACE = "lo0"
LOOPBACK_POOL = "172.20.{dc}.0/24"
LOOPBACK_FIRST_HOST = {
    "dc-gateway": 1,
    "spine": 11,
    "superspine": 21,
    "leaf": 101,
}
P2P_POOL = "10.1.{dc}.0/24"
DCI_P2P_POOL = "10.1.0.0/24"


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


def dc_number(device):
    m = re.fullmatch(r"DC(\d+)", device.site.name, re.IGNORECASE)
    return m.group(1) if m else None


def used_addresses(nb):
    """Every host address NetBox already knows about, mask ignored."""
    return {
        ipaddress.ip_interface(ip.address).ip for ip in nb.ipam.ip_addresses.all()
    }


def next_loopback(pool, first_host, used):
    net = ipaddress.ip_network(pool)
    for host in list(net.hosts())[first_host - 1:]:
        if host not in used:
            used.add(host)
            return f"{host}/32"
    fail(f"Loopback pool {pool} is exhausted.")


def next_p2p(pool, used):
    net = ipaddress.ip_network(pool)
    for subnet in net.subnets(new_prefix=31):
        a, b = subnet[0], subnet[1]
        if a not in used and b not in used:
            used.update((a, b))
            return f"{a}/31", f"{b}/31"
    fail(f"Point-to-point pool {pool} is exhausted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned addresses; make no changes.",
    )
    args = parser.parse_args()

    nb = connect()
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    devices = []
    for site_name in TARGET_SITE_NAMES:
        site = nb.dcim.sites.get(name=site_name)
        if not site:
            fail(f'Site "{site_name}" was not found. Run restructure_dc1_dc2.py first.')
        devices += list(nb.dcim.devices.filter(site_id=site.id, tag=DEMO_TAG_NAME))
    device_ids = {d.id for d in devices}

    used = used_addresses(nb)

    # Each entry: (description, function that applies it)
    plan = []
    warnings = []

    # -----------------------------------------------------------------
    # Loopbacks
    # -----------------------------------------------------------------
    for dev in sorted(devices, key=lambda d: d.name):
        if dev.primary_ip4:
            continue
        dc = dc_number(dev)
        role = dev.role.slug
        if role not in LOOPBACK_FIRST_HOST:
            warnings.append(f'{dev.name}: role "{role}" has no loopback range; skipped.')
            continue
        address = next_loopback(
            LOOPBACK_POOL.format(dc=dc), LOOPBACK_FIRST_HOST[role], used
        )

        def apply_loopback(dev=dev, address=address):
            iface = nb.dcim.interfaces.get(device_id=dev.id, name=LOOPBACK_INTERFACE)
            if not iface:
                iface = nb.dcim.interfaces.create(
                    device=dev.id, name=LOOPBACK_INTERFACE, type="virtual"
                )
            # Reuse an IP left on lo0 by an earlier, interrupted run
            existing = list(nb.ipam.ip_addresses.filter(interface_id=iface.id))
            ip = existing[0] if existing else nb.ipam.ip_addresses.create(
                address=address,
                status="active",
                assigned_object_type="dcim.interface",
                assigned_object_id=iface.id,
            )
            dev.primary_ip4 = ip.id
            dev.save()

        plan.append((f"{dev.name}:{LOOPBACK_INTERFACE}  {address}  (primary IPv4)", apply_loopback))

    # -----------------------------------------------------------------
    # Point-to-point links
    # -----------------------------------------------------------------
    seen_cables = set()
    for dev in sorted(devices, key=lambda d: d.name):
        for iface in nb.dcim.interfaces.filter(device_id=dev.id, cabled=True):
            if not iface.cable or iface.cable.id in seen_cables or not iface.link_peers:
                continue
            seen_cables.add(iface.cable.id)

            peer_iface = nb.dcim.interfaces.get(iface.link_peers[0]["id"])
            peer_dev = nb.dcim.devices.get(peer_iface.device.id)
            if peer_dev.id not in device_ids:
                continue

            link = f"{dev.name}:{iface.name} <-> {peer_dev.name}:{peer_iface.name}"
            a_ips = list(nb.ipam.ip_addresses.filter(interface_id=iface.id))
            b_ips = list(nb.ipam.ip_addresses.filter(interface_id=peer_iface.id))
            if a_ips and b_ips:
                continue
            if a_ips or b_ips:
                warnings.append(f"{link}: only one end has an IP; skipped.")
                continue

            a_dc, b_dc = dc_number(dev), dc_number(peer_dev)
            pool = DCI_P2P_POOL if a_dc != b_dc else P2P_POOL.format(dc=a_dc)
            a_addr, b_addr = next_p2p(pool, used)

            def apply_p2p(a=iface, b=peer_iface, a_addr=a_addr, b_addr=b_addr):
                for target, addr in ((a, a_addr), (b, b_addr)):
                    nb.ipam.ip_addresses.create(
                        address=addr,
                        status="active",
                        assigned_object_type="dcim.interface",
                        assigned_object_id=target.id,
                    )

            plan.append((f"{link}  {a_addr} / {b_addr}", apply_p2p))

    for w in warnings:
        print(f"  WARNING: {w}")

    if not plan:
        print("Nothing to do: every DC1/DC2 device and link already has its IPs.")
        return

    print(f"\n=== Planned addresses ({len(plan)}) ===")
    for desc, _ in plan:
        print(f"  {desc}")

    if args.dry_run:
        print("\n--dry-run set: no changes made. Re-run without --dry-run to apply.")
        return

    confirm = input(f'\nType "yes" to create these {len(plan)} assignments: ').strip().lower()
    if confirm != "yes":
        print("Aborted. No changes made.")
        return

    print("\n=== Creating ===")
    done = failed = 0
    for desc, apply in plan:
        try:
            apply()
            print(f"  OK: {desc}")
            done += 1
        except Exception as e:
            print(f"  FAILED: {desc} -- {e}")
            failed += 1

    print("\n=== Done ===")
    print(f"Created: {done}   Failed: {failed}")
    if failed:
        print("\nIt is safe to re-run: anything already assigned is skipped.")


if __name__ == "__main__":
    main()
