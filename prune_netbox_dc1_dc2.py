#!/usr/bin/env python3
"""
prune_netbox_dc1_dc2.py

Removes from NetBox whatever in sites DC1/DC2 is no longer in
ip_plan_dc1_dc2.yaml, so that NetBox, nrx and configure.py match the
smaller lab:

  - devices in DC1/DC2 that the plan does not list (as a router loopback
    or as a host), with their IP addresses; NetBox deletes their interfaces
    and cables with them
  - cables between remaining devices that the plan does not list (for
    example a host moved to another leaf port), and the addresses on both
    ends that the plan does not want there
  - addresses left on remaining interfaces that lost their cable this way
    (for example the remaining spine's port to a removed leaf)

Run it before add_hosts_dc1_dc2.py and assign_ips_dc1_dc2.py: those add the
new cable and addresses, and refuse while the old ones are in the way.

Why: in November 2026 the lab was cut to one DC gateway, superspine, spine
and leaf per DC (22 routers to 8) and to two hosts per DC (one Linux, one
Windows) to free memory on the lab host, and DVWA was removed. configure.py builds interfaces and BGP
neighbours from NetBox cables and addresses, so leftovers there would
configure ports and peers that no longer exist.

USAGE (on the lab host, venv active, NETBOX_URL and NETBOX_TOKEN exported):
  python3 prune_netbox_dc1_dc2.py --dry-run   # list what would be removed
  python3 prune_netbox_dc1_dc2.py             # asks for "yes", then removes it
  python3 prune_netbox_dc1_dc2.py --yes       # removes it without asking
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
SITES = ("DC1", "DC2")


def fail(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)


def load_plan(path):
    """Devices, cables (as frozensets of (device, interface)) and wanted addresses."""
    with open(path) as f:
        plan = yaml.safe_load(f)
    devices = set(plan["loopbacks"])
    cables = set()
    wanted = {}  # (device, interface) -> address
    for l in plan["links"]:
        cables.add(frozenset([(l["a"], l["a_if"]), (l["b"], l["b_if"])]))
        wanted[(l["a"], l["a_if"])] = l["a_ip"]
        wanted[(l["b"], l["b_if"])] = l["b_ip"]
    for h in plan.get("hosts", []):
        devices.add(h["name"])
        cables.add(frozenset([(h["leaf"], h["leaf_if"]), (h["name"], h["host_if"])]))
        wanted[(h["leaf"], h["leaf_if"])] = h["leaf_ip"]
        wanted[(h["name"], h["host_if"])] = h["host_ip"]
    for d, a in plan["loopbacks"].items():
        wanted[(d, "lo0")] = a
    return devices, cables, wanted


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="List what would be removed; change nothing.")
    parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
    parser.add_argument("--plan", default=PLAN_FILE, help="IP plan file")
    args = parser.parse_args()

    plan_devices, plan_cables, wanted = load_plan(args.plan)

    if not NETBOX_URL or not NETBOX_TOKEN:
        fail("NETBOX_URL and NETBOX_TOKEN must both be exported.")
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    try:
        site_ids = {}
        for s in SITES:
            site = nb.dcim.sites.get(name=s)
            if not site:
                fail(f'Site "{s}" not found in NetBox.')
            site_ids[s] = site.id
    except pynetbox.RequestError as e:
        fail(f"Could not talk to NetBox at {NETBOX_URL}: {e}")
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    nb_devices = {d.name: d for d in nb.dcim.devices.filter(site_id=list(site_ids.values()))}
    missing = sorted(plan_devices - set(nb_devices))
    if missing:
        print(f"  NOTE: in the plan but not in NetBox (add_hosts/restructure create them): {', '.join(missing)}")

    remove_devices = sorted(set(nb_devices) - plan_devices)

    # Every interface of the devices that stay: their cables and addresses
    keep = [nb_devices[n] for n in sorted(set(nb_devices) & plan_devices)]
    ifaces = {}  # (device, interface) -> interface record
    for dev in keep:
        for i in nb.dcim.interfaces.filter(device_id=dev.id):
            ifaces[(dev.name, i.name)] = i

    remove_cables = {}  # cable id -> description
    strand_ifaces = {}  # (device, interface) -> interface record: ports left unconnected
    unplugged = set()   # (device, interface) of remaining devices whose cable goes
    for (dname, iname), i in ifaces.items():
        if i.cable is None:
            continue
        peers = [(p.device.name, p.name) for p in (i.link_peers or []) if hasattr(p, "device")]
        peer_dev = peers[0][0] if peers else None
        if peer_dev in remove_devices or peer_dev is None:
            # The far end is being removed (or already gone), so this port is
            # left stranded on a device that stays. Delete the port and its
            # cable unless the plan still wants this port: configure.py rejects
            # the whole device if it has a port with no IP and no peer.
            unplugged.add((dname, iname))
            if (dname, iname) not in wanted:
                strand_ifaces[(dname, iname)] = i
                if i.cable is not None:
                    remove_cables[i.cable.id] = f"{dname}:{iname} (dangling, peer removed)"
            continue
        if any(frozenset([(dname, iname), p]) in plan_cables for p in peers):
            continue
        remove_cables[i.cable.id] = f"{dname}:{iname} <-> " + ", ".join(f"{d}:{n}" for d, n in peers)
        unplugged.add((dname, iname))
        unplugged.update(p for p in peers if p[0] in plan_devices)

    remove_ips = []  # (ip record, description)
    for key in sorted(unplugged):
        i = ifaces.get(key)
        if not i:
            continue
        for ip in nb.ipam.ip_addresses.filter(interface_id=i.id):
            if wanted.get(key) == ip.address:
                continue
            remove_ips.append((ip, f"{key[0]}:{key[1]}  {ip.address}"))

    # Addresses of removed devices go with them; list them so they are deleted, not orphaned
    for name in remove_devices:
        for ip in nb.ipam.ip_addresses.filter(device_id=nb_devices[name].id):
            remove_ips.append((ip, f"{name}:{ip.assigned_object.name if ip.assigned_object else '?'}  {ip.address}"))

    print(f"=== Devices to delete ({len(remove_devices)}) ===")
    for n in remove_devices:
        print(f"  {n}")
    print(f"\n=== Cables to delete between remaining devices ({len(remove_cables)}) ===")
    for desc in remove_cables.values():
        print(f"  {desc}")
    print(f"\n=== Stranded ports to delete on remaining devices ({len(strand_ifaces)}) ===")
    for key in sorted(strand_ifaces):
        print(f"  {key[0]}:{key[1]}")
    print(f"\n=== Addresses to delete ({len(remove_ips)}) ===")
    for _, desc in remove_ips:
        print(f"  {desc}")

    if not (remove_devices or remove_cables or remove_ips or strand_ifaces):
        print("\nNothing to remove: NetBox already matches the plan.")
        return
    if args.dry_run:
        print("\n--dry-run set: nothing changed.")
        return
    if not args.yes:
        if input('\nType "yes" to delete all of the above: ').strip().lower() != "yes":
            print("Aborted. Nothing changed.")
            return

    print("\n=== Deleting ===")
    failed = 0

    def attempt(desc, fn):
        nonlocal failed
        try:
            fn()
            print(f"  OK: {desc}")
        except Exception as e:  # keep going; a re-run picks up what is left
            failed += 1
            print(f"  FAILED: {desc} -- {e}")

    for name in remove_devices:   # primary IPs first, so the addresses can go
        dev = nb_devices[name]
        if dev.primary_ip4:
            def clear(dev=dev):
                dev.primary_ip4 = None
                dev.save()
            attempt(f"{name}: clear primary IPv4", clear)
    for ip, desc in remove_ips:
        attempt(f"address {desc}", ip.delete)
    for cid, desc in remove_cables.items():
        attempt(f"cable {desc}", lambda cid=cid: nb.dcim.cables.get(cid).delete())
    for key, iface in sorted(strand_ifaces.items()):
        attempt(f"port {key[0]}:{key[1]}", iface.delete)
    for name in remove_devices:
        attempt(f"device {name}", nb_devices[name].delete)

    print(f"\n=== Done: {failed} failed ===")
    if failed:
        print("Safe to re-run: anything already removed is skipped.")
        sys.exit(1)
    print("Next: python3 add_hosts_dc1_dc2.py, python3 assign_ips_dc1_dc2.py, then ./deploy_lab.sh")


if __name__ == "__main__":
    main()
