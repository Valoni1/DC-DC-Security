#!/usr/bin/env python3
"""
add_hosts_dc1_dc2.py

Adds the end hosts listed under "hosts:" in ip_plan_dc1_dc2.yaml to NetBox:
one device per host in its DC's site, tagged "demo", with one interface
cabled to its leaf. Without hosts the fabric only carries traffic between
router loopbacks; hosts give the experiments real endpoints (replication,
failover, attack traffic).

For each host the script makes sure these exist, creating what is missing:
  - device role "Server" (slug server), so nrx exports it (EXPORT_DEVICE_ROLES)
  - manufacturer "Generic" and a device type per OS ("Linux Host", "Windows Host")
  - platform per OS: "ubuntu" (Linux, mapped to containerlab kind linux in
    platform_map.yaml) or "windows"
  - the host device in Site DC1/DC2, tagged "demo"
  - the host interface (eth1) and a cable from it to the leaf port in the plan

The leaf port must exist and be free. Anything that already exists is left
alone, so it is safe to re-run. Addresses are not set here: run
assign_ips_dc1_dc2.py afterwards, which reads the same plan.

REQUIREMENTS:
  pip install pynetbox pyyaml   (already in your venv)

USAGE:
  export NETBOX_URL="http://localhost:8000"
  export NETBOX_TOKEN="your-api-token-here"
  python3 add_hosts_dc1_dc2.py --dry-run     # see what would be created
  python3 add_hosts_dc1_dc2.py               # create it
"""

import os
import re
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
DEMO_TAG_NAME = "demo"

ROLE = {"name": "Server", "slug": "server"}
MANUFACTURER = {"name": "Generic", "slug": "generic"}
# Per host OS: the NetBox device type and platform the host gets
OS_TYPES = {
    "linux": {
        "device_type": {"model": "Linux Host", "slug": "linux-host"},
        "platform": {"name": "Ubuntu", "slug": "ubuntu"},
    },
    "windows": {
        "device_type": {"model": "Windows Host", "slug": "windows-host"},
        "platform": {"name": "Windows", "slug": "windows"},
    },
}


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


def load_hosts(path):
    with open(path) as f:
        plan = yaml.safe_load(f)
    hosts = plan.get("hosts") or []
    if not hosts:
        fail(f"No hosts listed under 'hosts:' in {path}.")
    for h in hosts:
        if h.get("os") not in OS_TYPES:
            fail(f'{h.get("name")}: os must be one of {", ".join(OS_TYPES)}, not {h.get("os")!r}.')
        if not re.fullmatch(r"dc\d+-.+", h["name"]):
            fail(f'{h["name"]}: host names must start with dcN- so the site is known.')
    return hosts


def site_name(host):
    return "DC" + re.match(r"dc(\d+)-", host["name"]).group(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created; make no changes.",
    )
    parser.add_argument("--plan", default=PLAN_FILE, help="IP plan file")
    args = parser.parse_args()

    hosts = load_hosts(args.plan)

    nb = connect()
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    if not nb.extras.tags.get(name=DEMO_TAG_NAME):
        fail(f'Tag "{DEMO_TAG_NAME}" not found in NetBox.')

    # -----------------------------------------------------------------
    # Check every host against NetBox before creating anything.
    # -----------------------------------------------------------------
    plan = []  # hosts that still need a device and/or cable
    for h in hosts:
        site = nb.dcim.sites.get(name=site_name(h))
        if not site:
            fail(f'Site "{site_name(h)}" not found. Run restructure_dc1_dc2.py first.')
        leaf = nb.dcim.devices.get(name=h["leaf"])
        if not leaf:
            fail(f'{h["name"]}: leaf "{h["leaf"]}" not found. Run rename_dc1_dc2.py first.')
        leaf_if = nb.dcim.interfaces.get(device_id=leaf.id, name=h["leaf_if"])
        if not leaf_if:
            fail(f'{h["name"]}: interface {h["leaf_if"]} not found on {h["leaf"]}.')

        dev = nb.dcim.devices.get(name=h["name"])
        host_if = nb.dcim.interfaces.get(device_id=dev.id, name=h["host_if"]) if dev else None

        if leaf_if.cable is not None:
            peers = leaf_if.link_peers or []
            if host_if and any(getattr(p, "id", None) == host_if.id for p in peers):
                print(f'  OK: {h["name"]} is already cabled to {h["leaf"]}:{h["leaf_if"]}')
                continue
            fail(
                f'{h["leaf"]}:{h["leaf_if"]} is already cabled to something else; '
                f'pick a free leaf port for {h["name"]} in the plan.'
            )
        plan.append((h, site, leaf_if, dev))

    if not plan:
        print("\nNothing to do: every host exists and is cabled.")
        return

    print(f"\n=== Hosts to add ({len(plan)}) ===")
    for h, _, _, dev in plan:
        what = "cable only" if dev else "device, interface, cable"
        print(f'  {h["name"]:8} {h["os"]:8} {h["host_if"]} <-> {h["leaf"]}:{h["leaf_if"]}   ({what})')

    if args.dry_run:
        print("\n--dry-run set: no changes made. Re-run without --dry-run to apply.")
        return

    confirm = input(f'\nType "yes" to add these {len(plan)} hosts: ').strip().lower()
    if confirm != "yes":
        print("Aborted. No changes made.")
        return

    # -----------------------------------------------------------------
    # Shared objects: role, manufacturer, device types, platforms.
    # -----------------------------------------------------------------
    role = nb.dcim.device_roles.get(slug=ROLE["slug"]) or nb.dcim.device_roles.create(
        **ROLE, color="9e9e9e"
    )
    manufacturer = nb.dcim.manufacturers.get(slug=MANUFACTURER["slug"]) or (
        nb.dcim.manufacturers.create(**MANUFACTURER)
    )
    os_objects = {}
    for os_name in sorted({h["os"] for h, _, _, _ in plan}):
        spec = OS_TYPES[os_name]
        dtype = nb.dcim.device_types.get(slug=spec["device_type"]["slug"]) or (
            nb.dcim.device_types.create(manufacturer=manufacturer.id, **spec["device_type"])
        )
        platform = nb.dcim.platforms.get(slug=spec["platform"]["slug"]) or (
            nb.dcim.platforms.create(**spec["platform"])
        )
        os_objects[os_name] = (dtype, platform)

    # -----------------------------------------------------------------
    # Hosts
    # -----------------------------------------------------------------
    print("\n=== Adding hosts ===")
    done = failed = 0
    for h, site, leaf_if, dev in plan:
        try:
            if not dev:
                dtype, platform = os_objects[h["os"]]
                dev = nb.dcim.devices.create(
                    name=h["name"],
                    role=role.id,
                    device_type=dtype.id,
                    platform=platform.id,
                    site=site.id,
                    status="active",
                    tags=[{"name": DEMO_TAG_NAME}],
                )
            host_if = nb.dcim.interfaces.get(device_id=dev.id, name=h["host_if"]) or (
                nb.dcim.interfaces.create(device=dev.id, name=h["host_if"], type="1000base-t")
            )
            nb.dcim.cables.create(
                a_terminations=[{"object_type": "dcim.interface", "object_id": host_if.id}],
                b_terminations=[{"object_type": "dcim.interface", "object_id": leaf_if.id}],
                status="connected",
            )
            print(f'  OK: {h["name"]}')
            done += 1
        except Exception as e:
            print(f'  FAILED: {h["name"]} -- {e}')
            failed += 1

    print("\n=== Done ===")
    print(f"Added: {done}   Failed: {failed}")
    if failed:
        print("\nIt is safe to re-run: hosts that already exist and are cabled are skipped.")
    print(
        "\nNext: python3 assign_ips_dc1_dc2.py, then ./4_run_nrx.sh (or use the "
        "committed DC1-DC2.clab.yaml), redeploy the lab, ./5_run_config_mgmt.sh "
        "and python3 configure_hosts_dc1_dc2.py."
    )


if __name__ == "__main__":
    main()
