#!/usr/bin/env python3
"""
tag_dc1_dc2.py

Adds the "demo" tag to every device in Sites DC1 and DC2.

nrx (./4_run_nrx.sh, EXPORT_TAGS in nrx.conf) and config_mgmt/configure.py
only pick up NetBox devices tagged "demo". The original demo data only tagged
a few of the old syd1 devices, so without this the lab file and the generated
configs silently leave out every untagged leaf and spine.

Existing tags on each device are kept. Safe to re-run: devices that already
have the tag are left alone.

REQUIREMENTS:
  pip install pynetbox   (already in your venv)

USAGE:
  export NETBOX_URL="http://localhost:8000"
  export NETBOX_TOKEN="your-api-token-here"
  python3 tag_dc1_dc2.py --dry-run     # see which devices would be tagged
  python3 tag_dc1_dc2.py               # tag them
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

TARGET_SITE_NAMES = ["DC1", "DC2"]
DEMO_TAG_NAME = "demo"


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
        help="Print the devices that would be tagged; make no changes.",
    )
    args = parser.parse_args()

    nb = connect()
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    if not nb.extras.tags.get(name=DEMO_TAG_NAME):
        fail(f'Tag "{DEMO_TAG_NAME}" not found in NetBox.')

    to_tag = []
    already = 0
    for site_name in TARGET_SITE_NAMES:
        site = nb.dcim.sites.get(name=site_name)
        if not site:
            fail(f'Site "{site_name}" was not found. Run restructure_dc1_dc2.py first.')
        for dev in nb.dcim.devices.filter(site_id=site.id):
            if any(t.name == DEMO_TAG_NAME for t in dev.tags):
                already += 1
            else:
                to_tag.append(dev)

    print(f'Already tagged "{DEMO_TAG_NAME}": {already}')
    if not to_tag:
        print("Nothing to do: every DC1/DC2 device is tagged.")
        return

    print(f"\n=== Devices to tag ({len(to_tag)}) ===")
    for dev in to_tag:
        print(f"  {dev.name}")

    if args.dry_run:
        print("\n--dry-run set: no changes made. Re-run without --dry-run to apply.")
        return

    confirm = input(f'\nType "yes" to tag these {len(to_tag)} devices: ').strip().lower()
    if confirm != "yes":
        print("Aborted. No changes made.")
        return

    print("\n=== Tagging ===")
    done = failed = 0
    for dev in to_tag:
        try:
            dev.tags = [{"name": t.name} for t in dev.tags] + [{"name": DEMO_TAG_NAME}]
            dev.save()
            print(f"  OK: {dev.name}")
            done += 1
        except Exception as e:
            print(f"  FAILED: {dev.name} -- {e}")
            failed += 1

    print("\n=== Done ===")
    print(f"Tagged: {done}   Failed: {failed}")
    print(
        "\nNext: re-run ./4_run_nrx.sh to regenerate DC1-DC2.clab.yaml, "
        "redeploy the lab, then run ./5_run_config_mgmt.sh."
    )


if __name__ == "__main__":
    main()
