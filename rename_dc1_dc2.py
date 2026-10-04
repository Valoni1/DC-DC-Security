#!/usr/bin/env python3
"""
rename_dc1_dc2.py

Renames the leaf and spine devices that restructure_dc1_dc2.py moved into
Sites DC1 and DC2 so every device follows the same dcN-<name> pattern as the
superspines and DCI routers:

  syd1-pd1-sp1 -> dc1-sp1      syd1-pd2-sp1 -> dc2-sp1
  syd1-pd1-l1  -> dc1-l1       syd1-pd2-l1  -> dc2-l1
  ...

The names live in NetBox, and NetBox is where nrx (DC1-DC2.clab.yaml) and
config_mgmt/configure.py (device hostnames, BGP descriptions) read them from,
so renaming here keeps the topology file, the running lab and the generated
configs consistent. Re-run ./4_run_nrx.sh afterwards to regenerate
DC1-DC2.clab.yaml, then redeploy the lab so the containers get the new names.

Run it after restructure_dc1_dc2.py and cable_dc1_dc2.py: those scripts look
devices up by their old syd1-pdN-* names.

SAFETY:
  - Only devices in Sites DC1/DC2 whose name is syd1-pdN-<rest> are renamed,
    and only when N matches the site's number (a Pod 2 device sitting in DC1
    is reported, not renamed).
  - If the new name is already taken, that device is skipped.
  - Safe to re-run: already-renamed devices no longer match the old pattern.

REQUIREMENTS:
  pip install pynetbox   (already in your venv)

USAGE:
  export NETBOX_URL="http://localhost:8000"
  export NETBOX_TOKEN="your-api-token-here"
  python3 rename_dc1_dc2.py --dry-run     # see the renames, no changes
  python3 rename_dc1_dc2.py               # actually rename
"""

import os
import re
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

# site name -> DC number expected in the old syd1-pdN-* name
TARGET_SITES = {"DC1": "1", "DC2": "2"}

OLD_NAME_PATTERN = re.compile(r"^syd1-pd(\d+)-(.+)$")


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned renames; make no changes.",
    )
    args = parser.parse_args()

    nb = connect()
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    renames = []
    for site_name, dc_id in TARGET_SITES.items():
        site = nb.dcim.sites.get(name=site_name)
        if not site:
            fail(f'Site "{site_name}" was not found. Run restructure_dc1_dc2.py first.')

        for dev in nb.dcim.devices.filter(site_id=site.id):
            m = OLD_NAME_PATTERN.match(dev.name or "")
            if not m:
                continue
            if m.group(1) != dc_id:
                print(
                    f'  WARNING: "{dev.name}" is in {site_name} but its name says '
                    f"pod {m.group(1)}; not renaming it."
                )
                continue
            renames.append((dev, f"dc{dc_id}-{m.group(2)}"))

    if not renames:
        print("Nothing to rename: no syd1-pdN-* devices left in DC1/DC2.")
        return

    print(f"=== Planned renames ({len(renames)}) ===")
    for dev, new_name in renames:
        print(f"  {dev.name:<16} -> {new_name}")

    if args.dry_run:
        print("\n--dry-run set: no changes made. Re-run without --dry-run to apply.")
        return

    confirm = input(f'\nType "yes" to rename these {len(renames)} devices: ').strip().lower()
    if confirm != "yes":
        print("Aborted. No changes made.")
        return

    print("\n=== Renaming ===")
    renamed = skipped = failed = 0
    for dev, new_name in renames:
        if nb.dcim.devices.get(name=new_name):
            print(f'  SKIP: "{new_name}" already exists; "{dev.name}" left as is.')
            skipped += 1
            continue
        old_name = dev.name
        try:
            dev.name = new_name
            dev.save()
            print(f"  OK: {old_name} -> {new_name}")
            renamed += 1
        except Exception as e:
            print(f"  FAILED: {old_name} -> {new_name} -- {e}")
            failed += 1

    print("\n=== Done ===")
    print(f"Renamed: {renamed}   Skipped: {skipped}   Failed: {failed}")
    print(
        "\nNext: re-run ./4_run_nrx.sh to regenerate DC1-DC2.clab.yaml and "
        "redeploy the lab so the container names match."
    )


if __name__ == "__main__":
    main()
