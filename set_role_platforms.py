#!/usr/bin/env python3
"""
set_role_platforms.py

Creates four new NetBox Platform objects, one per device role
(leaf/spine/superspine/dc-gateway), and reassigns every DC1/DC2 device's
Platform field to the matching new one. This is the mechanism that
actually enables tiered per-role memory limits with nrx, since nrx's own
source code (confirmed by grepping nrx/nrx directly -- it contains zero
references to NetBox custom fields) only ever reads node parameters from
platform_map.yaml, keyed by NetBox's Device Platform slug. A NetBox custom
field, however named, is never consulted by nrx -- this was verified
directly against the tool's source, not assumed from documentation.

NEW PLATFORMS CREATED:
  srl-leaf        -> for devices with role "leaf"
  srl-spine       -> for devices with role "spine"
  srl-superspine  -> for devices with role "superspine"
  srl-gateway     -> for devices with role "dc-gateway"

Each new Platform copies the manufacturer of your existing SR Linux
Platform (looked up dynamically from a real reference device -- not
guessed), so nothing about your existing platform metadata is lost.

WHAT THIS SCRIPT DOES NOT DO:
  - It does NOT edit platform_map.yaml. That is a local file on this
    machine, not something reachable via the NetBox API, and it also
    holds your live API token, so it's edited by you directly. The exact
    YAML block to add is printed at the end of this script's run.
  - It does NOT touch the old, single SR Linix Platform object -- it is
    left in place but will simply no longer be referenced by any DC1/DC2
    device once this script finishes. You can delete it later via the UI
    if you want to tidy up.
  - It does NOT remove the "memory" custom field created by
    set_tiered_memory.py -- it is now unused (nrx never read it), but
    harmless to leave in place. Delete it via NetBox UI if you want to
    tidy up: Admin -> Custom Fields.

REQUIREMENTS:
  pip install pynetbox   (already in your venv)

USAGE:
  export NETBOX_URL="http://localhost:8000"
  export NETBOX_TOKEN="your-api-token-here"
  python3 set_role_platforms.py --dry-run     # see the plan, no changes
  python3 set_role_platforms.py               # actually apply it
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

# Real device name used purely to look up the existing SR Linux Platform's
# id/slug/manufacturer -- not guessed.
REFERENCE_DEVICE_NAME = "syd1-pd1-l1"

# role slug -> (new platform name, new platform slug, memory value for
# reference in the printed platform_map.yaml block at the end)
ROLE_PLATFORM_PLAN = {
    "leaf": ("SR Linux Leaf", "srl-leaf", "1Gb"),
    "spine": ("SR Linux Spine", "srl-spine", "1.5Gb"),
    "superspine": ("SR Linux Superspine", "srl-superspine", "1.5Gb"),
    "dc-gateway": ("SR Linux Gateway", "srl-gateway", "2Gb"),
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan; make no changes.",
    )
    args = parser.parse_args()

    nb = connect()
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    # -----------------------------------------------------------------
    # STEP 0: Discover current state.
    # -----------------------------------------------------------------
    print("=== Current state ===")

    ref_dev = nb.dcim.devices.get(name=REFERENCE_DEVICE_NAME)
    if not ref_dev:
        fail(f'Reference device "{REFERENCE_DEVICE_NAME}" not found.')
    if not ref_dev.platform:
        fail(
            f'Reference device "{REFERENCE_DEVICE_NAME}" has no Platform set '
            "-- cannot copy manufacturer/slug info from it."
        )
    old_platform = nb.dcim.platforms.get(id=ref_dev.platform.id)
    old_manufacturer_id = old_platform.manufacturer.id if old_platform.manufacturer else None
    print(
        f'Existing SR Linux platform: "{old_platform.name}" '
        f"(id={old_platform.id}, slug={old_platform.slug}, "
        f"manufacturer_id={old_manufacturer_id})"
    )

    sites = []
    for name in TARGET_SITE_NAMES:
        site = nb.dcim.sites.get(name=name)
        if not site:
            fail(f'Site "{name}" not found.')
        sites.append(site)

    devices_by_role = {}
    for site in sites:
        for dev in nb.dcim.devices.filter(site_id=site.id):
            role_slug = dev.role.slug if dev.role else "(no role)"
            devices_by_role.setdefault(role_slug, []).append(dev)

    print(f"\nDevices found across {TARGET_SITE_NAMES}:")
    for role_slug, devs in sorted(devices_by_role.items()):
        plan = ROLE_PLATFORM_PLAN.get(role_slug)
        if plan:
            print(f'  role "{role_slug}": {len(devs)} device(s) -> platform "{plan[1]}" (memory {plan[2]})')
        else:
            print(f'  role "{role_slug}": {len(devs)} device(s) -> NO PLAN, will be left untouched')

    # -----------------------------------------------------------------
    # Plan summary
    # -----------------------------------------------------------------
    print("\n=== Planned changes ===")
    for role_slug, (name, slug, mem) in ROLE_PLATFORM_PLAN.items():
        existing = nb.dcim.platforms.get(slug=slug)
        status = "already exists, will reuse" if existing else "will be created"
        count = len(devices_by_role.get(role_slug, []))
        print(f'  Platform "{name}" (slug={slug}): {status}. '
              f'{count} device(s) will be reassigned to it.')

    if args.dry_run:
        print("\n--dry-run set: no changes made. Re-run without --dry-run to apply.")
        return

    confirm = input('\nType "yes" to apply: ').strip().lower()
    if confirm != "yes":
        print("Aborted. No changes made.")
        return

    # -----------------------------------------------------------------
    # STEP 1: Create the four new platforms (or reuse if they exist).
    # -----------------------------------------------------------------
    print("\n=== Creating platforms ===")
    platform_ids = {}
    for role_slug, (name, slug, mem) in ROLE_PLATFORM_PLAN.items():
        existing = nb.dcim.platforms.get(slug=slug)
        if existing:
            platform_ids[role_slug] = existing.id
            print(f'  "{name}" already exists (id={existing.id}), reusing.')
            continue
        create_kwargs = dict(name=name, slug=slug)
        if old_manufacturer_id:
            create_kwargs["manufacturer"] = old_manufacturer_id
        new_platform = nb.dcim.platforms.create(**create_kwargs)
        platform_ids[role_slug] = new_platform.id
        print(f'  Created "{name}" (id={new_platform.id}).')

    # -----------------------------------------------------------------
    # STEP 2: Reassign devices.
    # -----------------------------------------------------------------
    print("\n=== Reassigning devices ===")
    updated = 0
    skipped = 0
    for role_slug, devs in devices_by_role.items():
        if role_slug not in platform_ids:
            for d in devs:
                print(f'  SKIP: "{d.name}" (role "{role_slug}" has no plan)')
                skipped += 1
            continue
        for d in devs:
            d.platform = platform_ids[role_slug]
            d.save()
            print(f'  OK: "{d.name}" -> platform id={platform_ids[role_slug]}')
            updated += 1

    print(f"\nUpdated: {updated}   Skipped: {skipped}")

    # -----------------------------------------------------------------
    # Print the platform_map.yaml block the user needs to add manually.
    # -----------------------------------------------------------------
    print("\n=== Add this to platform_map.yaml ===")
    print("Add these entries under the existing 'platforms:' top-level key:\n")
    for role_slug, (name, slug, mem) in ROLE_PLATFORM_PLAN.items():
        print(f"  {slug}:")
        print(f"    kinds:")
        print(f"      clab: {slug}")
    print("\nAdd these entries under the existing 'kinds: clab:' key:\n")
    for role_slug, (name, slug, mem) in ROLE_PLATFORM_PLAN.items():
        print(f"    {slug}:")
        print(f"      nodes:")
        print(f"        template: clab/nodes/srl.j2")
        print(f"        type: ixrd2")
        print(f"        memory: {mem}")
        print(f"      interface_names:")
        print(f"        template: clab/interface_names/srl.j2")
    print(
        "\nAfter editing platform_map.yaml, run:\n"
        "  ./4_run_nrx.sh\n"
        '  grep "memory:" *.clab.yaml | sort | uniq -c\n'
        "  sudo clab destroy -t <filename>.clab.yaml\n"
        "  sudo clab deploy -t <filename>.clab.yaml"
    )


if __name__ == "__main__":
    main()
