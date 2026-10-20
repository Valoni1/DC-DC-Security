#!/usr/bin/env python3
"""
remove_old_links_dc1_dc2.py

Removes the NetBox cables that still join DC1/DC2 devices to devices outside
those two sites, and the IP addresses on the DC1/DC2 end of each one.

Why: restructure_dc1_dc2.py moved the reference lab's pod spines into DC1 and
DC2 but deliberately left the shared superspines syd1-ss1/syd1-ss2 in place,
still cabled to all four spines. configure.py builds BGP neighbours from
cables, so every spine was configured with two extra eBGP neighbours
(10.0.0.x, AS 65000) that do not exist in the lab and stay in state
"active". The baseline capture of 20 October 2026 showed 8 of 10 sessions
established on each spine for this reason.

The syd1-ss1/syd1-ss2 devices themselves are left in NetBox; nrx and
configure.py ignore them once nothing in DC1/DC2 is cabled to them.

USAGE (on the lab host, venv active, NETBOX_URL and NETBOX_TOKEN exported):
  python3 remove_old_links_dc1_dc2.py --dry-run   # list what would be removed
  python3 remove_old_links_dc1_dc2.py             # remove it
  ./5_run_config_mgmt.sh                          # push configs without the old peers
"""

import os
import sys
import argparse

try:
    import pynetbox
except ImportError:
    print("pynetbox is not installed in this Python environment.")
    print("Run: pip install pynetbox   (inside your venv)")
    sys.exit(1)

NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")
SITES = {"DC1", "DC2"}


def fail(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="only list what would be removed")
    args = parser.parse_args()

    if not NETBOX_URL:
        fail("NETBOX_URL environment variable is not set.")
    if not NETBOX_TOKEN:
        fail("NETBOX_TOKEN environment variable is not set.")
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    try:
        nb.status()
    except Exception as e:
        fail(f"Could not connect to NetBox at {NETBOX_URL}: {e}")
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    site_of = {}
    found = []
    for site in SITES:
        for dev in nb.dcim.devices.filter(site=site.lower()):  # slugs dc1, dc2
            site_of[dev.name] = site
    if not site_of:
        fail("No devices found in sites DC1/DC2.")

    for name in sorted(site_of):
        for intf in nb.dcim.interfaces.filter(device=name, cabled=True):
            for peer in intf.link_peers or []:
                peer_dev = peer["device"]["name"] if isinstance(peer, dict) else peer.device.name
                peer_if = peer["name"] if isinstance(peer, dict) else peer.name
                if peer_dev in site_of:
                    continue
                # Only old fabric routers; never touch a cable to an end host
                far = nb.dcim.devices.get(name=peer_dev)
                if far and far.role and far.role.slug == "server":
                    continue
                ips = list(nb.ipam.ip_addresses.filter(device=name, interface=intf.name))
                found.append((name, intf, peer_dev, peer_if, ips))

    if not found:
        print("Nothing to do: no DC1/DC2 interface is cabled outside DC1/DC2.")
        return

    print("DC1/DC2 interfaces cabled to devices outside DC1/DC2:")
    for name, intf, peer_dev, peer_if, ips in found:
        ip_txt = ", ".join(str(ip.address) for ip in ips) or "no IP"
        print(f"  {name} {intf.name} ({ip_txt})  ->  {peer_dev} {peer_if}")

    if args.dry_run:
        print("\n--dry-run: nothing removed.")
        return

    print()
    done = set()
    for name, intf, peer_dev, peer_if, ips in found:
        cable = intf.cable
        if cable and cable.id not in done:
            nb.dcim.cables.get(cable.id).delete()
            done.add(cable.id)
            print(f"  deleted cable {cable.id}: {name} {intf.name} <-> {peer_dev} {peer_if}")
        for ip in ips:
            ip.delete()
            print(f"  deleted IP {ip.address} from {name} {intf.name}")

    print(f"\nRemoved {len(done)} cable(s). Now run ./5_run_config_mgmt.sh to push the configs.")


if __name__ == "__main__":
    main()
