#!/usr/bin/env python3
"""
assign_ips_dc1_dc2.py

Makes NetBox's IP addresses for every DC1/DC2 node and link match
ip_plan_dc1_dc2.yaml, the single file that defines the lab's addressing.
config_mgmt/configure.py renders device configs from NetBox, so after this
script runs, the configs follow the plan.

For every loopback and link end in the plan:
  - the address is already there          -> left alone
  - the interface has one other address    -> that address is changed
  - the interface has no address           -> the address is created
  - the interface has several addresses    -> reported, left alone
  - the address is used on another
    interface in NetBox                    -> reported, left alone
Loopbacks go on interface lo0 (created if missing) and become the device's
primary IPv4.

Run it after rename_dc1_dc2.py: the plan uses the dcN-* device names.

REQUIREMENTS:
  pip install pynetbox pyyaml   (already in your venv)

USAGE:
  export NETBOX_URL="http://localhost:8000"
  export NETBOX_TOKEN="your-api-token-here"
  python3 assign_ips_dc1_dc2.py --dry-run     # see what would change
  python3 assign_ips_dc1_dc2.py               # apply the plan
"""

import os
import sys
import argparse
import ipaddress

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
LOOPBACK_INTERFACE = "lo0"


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


def load_plan(path):
    """Flatten the plan into (device, interface, address, is_loopback) tuples."""
    with open(path) as f:
        plan = yaml.safe_load(f)
    entries = []
    for device, address in plan["loopbacks"].items():
        entries.append((device, LOOPBACK_INTERFACE, address, True))
    for link in plan["links"]:
        entries.append((link["a"], link["a_if"], link["a_ip"], False))
        entries.append((link["b"], link["b_if"], link["b_ip"], False))

    # Catch typos before touching NetBox: every address must be valid and unique
    seen = {}
    for device, iface, address, _ in entries:
        try:
            host = ipaddress.ip_interface(address).ip
        except ValueError:
            fail(f"{device}:{iface} has an invalid address in the plan: {address}")
        if host in seen:
            fail(f"{address} is planned twice: {seen[host]} and {device}:{iface}")
        seen[host] = f"{device}:{iface}"
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change; make no changes.",
    )
    parser.add_argument("--plan", default=PLAN_FILE, help="IP plan file")
    args = parser.parse_args()

    entries = load_plan(args.plan)

    nb = connect()
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    # host address -> (ip record) for every address NetBox already has
    existing = {
        ipaddress.ip_interface(ip.address).ip: ip for ip in nb.ipam.ip_addresses.all()
    }

    devices = {}
    changes = []  # (description, function that applies it)
    warnings = []
    unchanged = 0

    for device_name, iface_name, address, is_loopback in entries:
        where = f"{device_name}:{iface_name}"
        if device_name not in devices:
            devices[device_name] = nb.dcim.devices.get(name=device_name)
        dev = devices[device_name]
        if not dev:
            warnings.append(f'{where}: device "{device_name}" not in NetBox; skipped.')
            continue

        iface = nb.dcim.interfaces.get(device_id=dev.id, name=iface_name)
        if not iface and not is_loopback:
            warnings.append(f"{where}: interface not in NetBox; skipped.")
            continue

        current = list(nb.ipam.ip_addresses.filter(interface_id=iface.id)) if iface else []
        host = ipaddress.ip_interface(address).ip
        holder = existing.get(host)
        if holder and not any(ip.id == holder.id for ip in current):
            warnings.append(f"{where}: {address} is already used elsewhere in NetBox; skipped.")
            continue

        needs_primary = is_loopback and (
            not dev.primary_ip4
            or ipaddress.ip_interface(dev.primary_ip4.address).ip != host
        )

        if any(ip.address == address for ip in current):
            if not needs_primary:
                unchanged += 1
                continue
            desc = f"{where}  {address}  (set as primary IPv4)"
        elif len(current) > 1:
            warnings.append(
                f"{where}: has {len(current)} addresses "
                f"({', '.join(ip.address for ip in current)}); skipped."
            )
            continue
        elif current:
            desc = f"{where}  {current[0].address} -> {address}"
        else:
            desc = f"{where}  {address}  (new)"

        def apply(dev=dev, iface=iface, iface_name=iface_name, address=address,
                  is_loopback=is_loopback):
            if not iface:
                iface = nb.dcim.interfaces.create(
                    device=dev.id, name=iface_name, type="virtual"
                )
            current = list(nb.ipam.ip_addresses.filter(interface_id=iface.id))
            match = [ip for ip in current if ip.address == address]
            if match:
                ip = match[0]
            elif current:
                ip = current[0]
                ip.address = address
                ip.save()
            else:
                ip = nb.ipam.ip_addresses.create(
                    address=address,
                    status="active",
                    assigned_object_type="dcim.interface",
                    assigned_object_id=iface.id,
                )
            if is_loopback:
                dev.primary_ip4 = ip.id
                dev.save()

        changes.append((desc, apply))

    for w in warnings:
        print(f"  WARNING: {w}")

    print(f"\nAlready matching the plan: {unchanged}")
    if not changes:
        print("Nothing to change: NetBox matches the plan.")
        return

    print(f"\n=== Changes ({len(changes)}) ===")
    for desc, _ in changes:
        print(f"  {desc}")

    if args.dry_run:
        print("\n--dry-run set: no changes made. Re-run without --dry-run to apply.")
        return

    confirm = input(f'\nType "yes" to apply these {len(changes)} changes: ').strip().lower()
    if confirm != "yes":
        print("Aborted. No changes made.")
        return

    print("\n=== Applying ===")
    done = failed = 0
    for desc, apply in changes:
        try:
            apply()
            print(f"  OK: {desc}")
            done += 1
        except Exception as e:
            print(f"  FAILED: {desc} -- {e}")
            failed += 1

    print("\n=== Done ===")
    print(f"Applied: {done}   Failed: {failed}")
    if failed:
        print("\nIt is safe to re-run: anything already matching the plan is skipped.")


if __name__ == "__main__":
    main()
