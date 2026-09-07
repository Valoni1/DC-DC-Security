#!/usr/bin/env python3
"""
set_tiered_memory.py

Creates a "memory" custom field on NetBox Device objects (if it doesn't
already exist) and sets a role-based tiered value on every device in DC1
and DC2, so that a future ./4_run_nrx.sh regeneration will automatically
emit a `memory:` line in the generated .clab.yaml for each node -- no
hand-editing required.

TIERS APPLIED (by device role slug):
  leaf        -> 1Gb    (confirmed stable in a real single-node test:
                          16+ min uptime, all ~26 SR Linux management
                          daemons running, sr_cli responsive)
  spine       -> 1.5Gb  (more forwarding/protocol state than a leaf)
  superspine  -> 1.5Gb  (same reasoning as spine)
  dc-gateway  -> 2Gb    (carries the DCI eBGP session -- the single link
                          your failover/adversarial experiments depend on,
                          given the most headroom deliberately)

These role slugs are taken directly from the real, active EXPORT_DEVICE_ROLES
line in this project's own nrx.conf: ['dc-gateway', 'superspine', 'spine',
'leaf'] -- not guessed.

WHAT THIS SCRIPT DOES NOT DO:
  - It does not run ./4_run_nrx.sh or redeploy anything. Run those
    yourself afterwards to actually apply the change to a running lab.
  - It does not touch devices outside DC1 and DC2 (e.g. the original,
    now-unused syd1-ss1/syd1-ss2 or the original SR-OS dc-gateway under
    SYD1).

REQUIREMENTS:
  pip install pynetbox   (already in your venv)

USAGE:
  export NETBOX_URL="http://localhost:8000"
  export NETBOX_TOKEN="your-api-token-here"
  python3 set_tiered_memory.py --dry-run     # see the plan, no changes
  python3 set_tiered_memory.py               # actually apply it
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

CUSTOM_FIELD_NAME = "memory"

# Role slug -> memory value. Taken directly from nrx.conf's real, active
# EXPORT_DEVICE_ROLES list, not guessed.
MEMORY_TIERS = {
    "leaf": "1Gb",
    "spine": "1.5Gb",
    "superspine": "1.5Gb",
    "dc-gateway": "2Gb",
}

TARGET_SITE_NAMES = ["DC1", "DC2"]


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
    # STEP 0: Discover devices in DC1/DC2 and their roles.
    # -----------------------------------------------------------------
    print("=== Current state ===")
    sites = []
    for name in TARGET_SITE_NAMES:
        site = nb.dcim.sites.get(name=name)
        if not site:
            fail(f'Site "{name}" not found.')
        sites.append(site)
        print(f'Found site "{site.name}" (id={site.id})')

    devices_by_role = {}
    all_devices = []
    for site in sites:
        for dev in nb.dcim.devices.filter(site_id=site.id):
            all_devices.append(dev)
            role_slug = dev.role.slug if dev.role else "(no role)"
            devices_by_role.setdefault(role_slug, []).append(dev)

    print(f"\nTotal devices found across {TARGET_SITE_NAMES}: {len(all_devices)}")
    for role_slug, devs in sorted(devices_by_role.items()):
        tier = MEMORY_TIERS.get(role_slug, "NO TIER DEFINED -- will be skipped")
        print(f'  role "{role_slug}": {len(devs)} device(s) -> memory={tier}')
        for d in devs:
            print(f"      {d.name}")

    unhandled = [r for r in devices_by_role if r not in MEMORY_TIERS]
    if unhandled:
        print(
            f"\nWARNING: role(s) {unhandled} have no entry in MEMORY_TIERS "
            "and will be left untouched. Add them to MEMORY_TIERS at the "
            "top of this script if they should get a memory value too."
        )

    # -----------------------------------------------------------------
    # STEP 1: Ensure the custom field exists.
    # -----------------------------------------------------------------
    cf = nb.extras.custom_fields.get(name=CUSTOM_FIELD_NAME)
    print(f'\n=== Custom field "{CUSTOM_FIELD_NAME}" ===')
    if cf:
        print(f'Already exists (id={cf.id}, type={cf.type}).')
    else:
        print(f'Does not exist yet -- will be created as a text field on dcim.device.')

    total_to_set = sum(
        len(devs) for role, devs in devices_by_role.items() if role in MEMORY_TIERS
    )
    print(f"\n{total_to_set} device(s) will have their \"{CUSTOM_FIELD_NAME}\" "
          f"custom field set according to the tiers above.")

    if args.dry_run:
        print("\n--dry-run set: no changes made. Re-run without --dry-run to apply.")
        return

    confirm = input(f'\nType "yes" to apply: ').strip().lower()
    if confirm != "yes":
        print("Aborted. No changes made.")
        return

    # -----------------------------------------------------------------
    # STEP 2: Create the custom field if needed.
    # -----------------------------------------------------------------
    if not cf:
        cf = nb.extras.custom_fields.create(
            name=CUSTOM_FIELD_NAME,
            label="Memory",
            type="text",
            object_types=["dcim.device"],
            description="containerlab per-node memory limit, e.g. 1Gb",
        )
        print(f'\nCreated custom field "{cf.name}" (id={cf.id}).')

    # -----------------------------------------------------------------
    # STEP 3: Apply tiered values.
    # -----------------------------------------------------------------
    print("\n=== Applying tiered memory values ===")
    updated = 0
    skipped = 0
    failed = 0

    for role_slug, devs in devices_by_role.items():
        value = MEMORY_TIERS.get(role_slug)
        if not value:
            for d in devs:
                print(f'  SKIP: "{d.name}" (role "{role_slug}" has no tier defined)')
                skipped += 1
            continue

        for d in devs:
            try:
                d.custom_fields[CUSTOM_FIELD_NAME] = value
                d.save()
                print(f'  OK: "{d.name}" (role "{role_slug}") -> memory={value}')
                updated += 1
            except Exception as e:
                print(f'  FAILED: "{d.name}" -- {e}')
                failed += 1

    print("\n=== Done ===")
    print(f"Updated: {updated}   Skipped: {skipped}   Failed: {failed}")
    print(
        "\nNext steps:\n"
        "  ./4_run_nrx.sh\n"
        '  grep "memory:" *.clab.yaml   # confirm it now appears per node\n'
        "  sudo clab destroy -t <filename>.clab.yaml\n"
        "  sudo clab deploy -t <filename>.clab.yaml"
    )


if __name__ == "__main__":
    main()
