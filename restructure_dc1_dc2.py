#!/usr/bin/env python3
"""
restructure_dc1_dc2.py

Restructures the current NetBox model (one Site "SYD1" with two Locations
"Pod 1" / "Pod 2" sharing superspines) into two independent Sites, DC1 and
DC2, each carrying its own devices plus a new dc-gateway device role (matches the role name NRX's EXPORT_DEVICE_ROLES already expects).

WHAT THIS SCRIPT DOES, IN ORDER:
  1. Verifies connectivity and prints what it found (existing site, existing
     "Pod 1" / "Pod 2" locations, existing devices) before changing anything.
  2. Creates two new Sites: DC1 and DC2 (slugs dc1 / dc2) if they don't
     already exist.
  3. Creates a new Device Role: "dc-gateway" (matches EXPORT_DEVICE_ROLES in nrx.conf) if it doesn't
     already exist.
  4. Re-homes every device currently in Location "Pod 1" to Site DC1, and
     every device in Location "Pod 2" to Site DC2. Their Location field is
     cleared (DC1/DC2 are now the site itself, so a sub-location isn't
     needed unless you want one).
  5. Creates two new superspine devices in DC1 (dc1-ss1, dc1-ss2) and two
     in DC2 (dc2-ss1, dc2-ss2), re-using the same device_type, platform and
     device role as your existing syd1-ss1/syd1-ss2 (looked up dynamically),
     so each DC has its own, independent superspine pair rather than a
     shared one. This is a deliberate design decision, not a default: the
     dissertation's premise is two independent, geographically separate
     data centres connected only by a DCI link, so sharing a superspine
     between them would contradict that. The ORIGINAL syd1-ss1/syd1-ss2
     devices are left exactly as they are -- this script does not delete or
     modify them. Remove them yourself in the UI once you've confirmed the
     new per-DC superspines are correctly wired, if you no longer want them.
  6. Creates one new dc-gateway device in DC1 and one in DC2, re-using the
     same device_type and platform as your existing leaf/spine devices
     (looked up dynamically -- nothing hardcoded), tagged "demo" so NRX
     picks them up.
  7. Prints a summary of every change made.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - It does NOT delete or modify the original syd1-ss1/syd1-ss2 devices --
    see point 5 above.
  - It does NOT create any interfaces, cables, or IP addresses on the new
    DCI routers. It does NOT configure BGP. This script only builds the
    NetBox *inventory* records; the actual eBGP config and the containerlab
    link between the two DCI routers is a separate step (NRX regeneration +
    manual SR Linux config, or an interface/cable creation script once you
    confirm addressing).
  - It does NOT run automatically. It prints exactly what it is about to do
    and asks for a typed "yes" before writing anything, except when run
    with --dry-run, which only prints and changes nothing.

REQUIREMENTS:
  pip install pynetbox   (already in your venv from the nrx toolchain)

USAGE:
  export NETBOX_URL="http://localhost:8000"
  export NETBOX_TOKEN="your-api-token-here"
  python3 restructure_dc1_dc2.py --dry-run     # see what it would do
  python3 restructure_dc1_dc2.py               # actually do it

Get an API token from the NetBox UI: top-right profile icon -> API Tokens ->
Add a token. It must have write permission (admin token is simplest for a
lab instance).
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


# ---------------------------------------------------------------------------
# Configuration -- read from environment, never hardcoded, never guessed.
# ---------------------------------------------------------------------------

NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")

SOURCE_SITE_NAME = "SYD1"          # your existing site
POD1_LOCATION_NAME = "Pod 1"       # existing location under SYD1
POD2_LOCATION_NAME = "Pod 2"       # existing location under SYD1

NEW_SITE_1_NAME = "DC1"
NEW_SITE_1_SLUG = "dc1"
NEW_SITE_2_NAME = "DC2"
NEW_SITE_2_SLUG = "dc2"

DCI_ROLE_NAME = "dc-gateway"
DCI_ROLE_SLUG = "dc-gateway"
DCI_ROLE_COLOR = "9c27b0"          # purple, purely cosmetic

DEMO_TAG_NAME = "demo"

# Names for the two new DCI router devices. Adjust if you want different
# naming -- nothing else in the script depends on this exact string.
DC1_DCI_DEVICE_NAME = "dc1-dci-r1"
DC2_DCI_DEVICE_NAME = "dc2-dci-r1"

# A device already known to exist in Pod 1 or Pod 2, used ONLY to look up
# which device_type and platform your fabric already uses, so the new DCI
# routers are built from real, existing NetBox objects rather than a
# guessed ID. Change this if your leaf-1 device is named differently.
REFERENCE_DEVICE_NAME = "syd1-pd1-l1"

# The two existing superspine devices, used ONLY to look up their
# device_type, platform, and device role, so the new per-DC superspines are
# built from real, existing NetBox objects. Change these if your superspine
# devices are named differently.
EXISTING_SUPERSPINE_1_NAME = "syd1-ss1"
EXISTING_SUPERSPINE_2_NAME = "syd1-ss2"

# Names for the four new per-DC superspine devices.
DC1_SS1_NAME = "dc1-ss1"
DC1_SS2_NAME = "dc1-ss2"
DC2_SS1_NAME = "dc2-ss1"
DC2_SS2_NAME = "dc2-ss2"


def fail(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)


def connect():
    if not NETBOX_URL:
        fail("NETBOX_URL environment variable is not set.")
    if not NETBOX_TOKEN:
        fail("NETBOX_TOKEN environment variable is not set.")
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    # Force an actual request now so connection/auth problems surface here,
    # not halfway through making changes.
    try:
        list(nb.dcim.sites.all())
    except Exception as e:
        fail(f"Could not connect to NetBox at {NETBOX_URL}: {e}")
    return nb


def get_or_none(endpoint, **kwargs):
    """Thin wrapper so a missing object is None, not an exception."""
    return endpoint.get(**kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; make no changes.",
    )
    args = parser.parse_args()

    nb = connect()
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    # -----------------------------------------------------------------
    # STEP 0: Discover current state. Nothing here writes anything.
    # -----------------------------------------------------------------
    print("=== Current state ===")

    source_site = get_or_none(nb.dcim.sites, name=SOURCE_SITE_NAME)
    if not source_site:
        fail(f'Site "{SOURCE_SITE_NAME}" was not found. Check the name.')
    print(f'Found source site: "{source_site.name}" (id={source_site.id})')

    pod1_location = get_or_none(
        nb.dcim.locations, name=POD1_LOCATION_NAME, site_id=source_site.id
    )
    pod2_location = get_or_none(
        nb.dcim.locations, name=POD2_LOCATION_NAME, site_id=source_site.id
    )
    if not pod1_location:
        fail(f'Location "{POD1_LOCATION_NAME}" not found under {SOURCE_SITE_NAME}.')
    if not pod2_location:
        fail(f'Location "{POD2_LOCATION_NAME}" not found under {SOURCE_SITE_NAME}.')
    print(f'Found location "{pod1_location.name}" (id={pod1_location.id})')
    print(f'Found location "{pod2_location.name}" (id={pod2_location.id})')

    pod1_devices = list(nb.dcim.devices.filter(location_id=pod1_location.id))
    pod2_devices = list(nb.dcim.devices.filter(location_id=pod2_location.id))
    print(f"Pod 1 devices found: {len(pod1_devices)} -> "
          f"{[d.name for d in pod1_devices]}")
    print(f"Pod 2 devices found: {len(pod2_devices)} -> "
          f"{[d.name for d in pod2_devices]}")

    if not pod1_devices:
        fail("No devices found in Pod 1. Nothing to re-home. Aborting.")
    if not pod2_devices:
        fail("No devices found in Pod 2. Nothing to re-home. Aborting.")

    reference_device = get_or_none(nb.dcim.devices, name=REFERENCE_DEVICE_NAME)
    if not reference_device:
        fail(
            f'Reference device "{REFERENCE_DEVICE_NAME}" not found. '
            "Edit REFERENCE_DEVICE_NAME at the top of this script to match "
            "a real device name in your NetBox instance."
        )
    device_type_id = reference_device.device_type.id
    platform_id = reference_device.platform.id if reference_device.platform else None
    print(
        f'Reference device "{reference_device.name}" uses '
        f"device_type id={device_type_id}, "
        f"platform id={platform_id} -- new DCI routers will reuse these."
    )

    demo_tag = get_or_none(nb.extras.tags, name=DEMO_TAG_NAME)
    if not demo_tag:
        fail(
            f'Tag "{DEMO_TAG_NAME}" not found. It should already exist '
            "since your existing devices use it. Check spelling/case."
        )
    print(f'Found tag "{demo_tag.name}" (id={demo_tag.id})')

    existing_ss1 = get_or_none(nb.dcim.devices, name=EXISTING_SUPERSPINE_1_NAME)
    existing_ss2 = get_or_none(nb.dcim.devices, name=EXISTING_SUPERSPINE_2_NAME)
    if not existing_ss1 or not existing_ss2:
        fail(
            f'Superspine reference device(s) not found: '
            f'"{EXISTING_SUPERSPINE_1_NAME}" '
            f'({"found" if existing_ss1 else "MISSING"}), '
            f'"{EXISTING_SUPERSPINE_2_NAME}" '
            f'({"found" if existing_ss2 else "MISSING"}). '
            "Edit EXISTING_SUPERSPINE_1_NAME / _2_NAME at the top of this "
            "script to match real device names in your NetBox instance."
        )
    ss_device_type_id = existing_ss1.device_type.id
    ss_platform_id = existing_ss1.platform.id if existing_ss1.platform else None
    ss_role_id = existing_ss1.role.id if existing_ss1.role else None
    print(
        f'Found superspine references "{existing_ss1.name}" / '
        f'"{existing_ss2.name}" -- new per-DC superspines will reuse '
        f"device_type id={ss_device_type_id}, role id={ss_role_id}, "
        f"platform id={ss_platform_id}."
    )

    site1_exists = get_or_none(nb.dcim.sites, slug=NEW_SITE_1_SLUG)
    site2_exists = get_or_none(nb.dcim.sites, slug=NEW_SITE_2_SLUG)
    role_exists = get_or_none(nb.dcim.device_roles, slug=DCI_ROLE_SLUG)

    print("\n=== Planned changes ===")
    print(
        f'1. Site "{NEW_SITE_1_NAME}": '
        + ("already exists, will reuse it." if site1_exists else "will be created.")
    )
    print(
        f'2. Site "{NEW_SITE_2_NAME}": '
        + ("already exists, will reuse it." if site2_exists else "will be created.")
    )
    print(
        f'3. Device role "{DCI_ROLE_NAME}": '
        + ("already exists, will reuse it." if role_exists else "will be created.")
    )
    print(
        f"4. Re-home {len(pod1_devices)} device(s) from Pod 1 -> {NEW_SITE_1_NAME}, "
        f"clearing their location and rack (rack belongs to the old site)."
    )
    print(
        f"5. Re-home {len(pod2_devices)} device(s) from Pod 2 -> {NEW_SITE_2_NAME}, "
        f"clearing their location and rack (rack belongs to the old site)."
    )
    print(
        f'6. Create superspines "{DC1_SS1_NAME}", "{DC1_SS2_NAME}" in '
        f"{NEW_SITE_1_NAME}, and \"{DC2_SS1_NAME}\", \"{DC2_SS2_NAME}\" in "
        f'{NEW_SITE_2_NAME}, tagged "{DEMO_TAG_NAME}" (each skipped if it '
        f"already exists). Original {EXISTING_SUPERSPINE_1_NAME}/"
        f"{EXISTING_SUPERSPINE_2_NAME} are left untouched."
    )
    print(
        f'7. Create device "{DC1_DCI_DEVICE_NAME}" in {NEW_SITE_1_NAME}, '
        f'role "{DCI_ROLE_NAME}", tagged "{DEMO_TAG_NAME}" '
        f"(skipped if it already exists)."
    )
    print(
        f'8. Create device "{DC2_DCI_DEVICE_NAME}" in {NEW_SITE_2_NAME}, '
        f'role "{DCI_ROLE_NAME}", tagged "{DEMO_TAG_NAME}" '
        f"(skipped if it already exists)."
    )

    if args.dry_run:
        print("\n--dry-run set: no changes made. Re-run without --dry-run to apply.")
        return

    confirm = input('\nType "yes" to apply these changes: ').strip().lower()
    if confirm != "yes":
        print("Aborted. No changes made.")
        return

    # -----------------------------------------------------------------
    # STEP 1: Create (or fetch) the two new sites.
    # -----------------------------------------------------------------
    print("\n=== Applying changes ===")

    dc1_site = site1_exists or nb.dcim.sites.create(
        name=NEW_SITE_1_NAME, slug=NEW_SITE_1_SLUG, status="active"
    )
    print(f'Site "{dc1_site.name}" ready (id={dc1_site.id}).')

    dc2_site = site2_exists or nb.dcim.sites.create(
        name=NEW_SITE_2_NAME, slug=NEW_SITE_2_SLUG, status="active"
    )
    print(f'Site "{dc2_site.name}" ready (id={dc2_site.id}).')

    # -----------------------------------------------------------------
    # STEP 2: Create (or fetch) the dc-gateway device role.
    # -----------------------------------------------------------------
    dci_role = role_exists or nb.dcim.device_roles.create(
        name=DCI_ROLE_NAME, slug=DCI_ROLE_SLUG, color=DCI_ROLE_COLOR
    )
    print(f'Device role "{dci_role.name}" ready (id={dci_role.id}).')

    # -----------------------------------------------------------------
    # STEP 3: Re-home Pod 1 devices into DC1, Pod 2 devices into DC2.
    # -----------------------------------------------------------------
    for dev in pod1_devices:
        dev.site = dc1_site.id
        dev.location = None
        dev.rack = None
        dev.face = None
        dev.position = None
        dev.save()
        print(f'  Re-homed "{dev.name}" -> {NEW_SITE_1_NAME}')

    for dev in pod2_devices:
        dev.site = dc2_site.id
        dev.location = None
        dev.rack = None
        dev.face = None
        dev.position = None
        dev.save()
        print(f'  Re-homed "{dev.name}" -> {NEW_SITE_2_NAME}')

    # -----------------------------------------------------------------
    # STEP 4: Create per-DC superspines (dc1-ss1/ss2, dc2-ss1/ss2), if not
    # already present. Original syd1-ss1/syd1-ss2 are left untouched.
    # -----------------------------------------------------------------
    def create_superspine_if_missing(name, site_id):
        existing = get_or_none(nb.dcim.devices, name=name)
        if existing:
            print(f'Device "{name}" already exists, skipping creation.')
            return
        create_kwargs = dict(
            name=name,
            device_type=ss_device_type_id,
            site=site_id,
            status="active",
        )
        if ss_role_id:
            create_kwargs["role"] = ss_role_id
        if ss_platform_id:
            create_kwargs["platform"] = ss_platform_id
        new_dev = nb.dcim.devices.create(**create_kwargs)
        try:
            new_dev.tags = [{"name": demo_tag.name}]
            new_dev.save()
        except Exception as e:
            print(
                f'  Created "{new_dev.name}" but could not auto-tag it '
                f'("{DEMO_TAG_NAME}"): {e}. Add the tag manually in the UI.'
            )
        else:
            print(f'  Created "{new_dev.name}" in site id={site_id}, tagged.')

    create_superspine_if_missing(DC1_SS1_NAME, dc1_site.id)
    create_superspine_if_missing(DC1_SS2_NAME, dc1_site.id)
    create_superspine_if_missing(DC2_SS1_NAME, dc2_site.id)
    create_superspine_if_missing(DC2_SS2_NAME, dc2_site.id)

    # -----------------------------------------------------------------
    # STEP 5: Create the two DCI router devices, if not already present.
    # -----------------------------------------------------------------
    existing_dc1_dci = get_or_none(nb.dcim.devices, name=DC1_DCI_DEVICE_NAME)
    if existing_dc1_dci:
        print(f'Device "{DC1_DCI_DEVICE_NAME}" already exists, skipping creation.')
    else:
        create_kwargs = dict(
            name=DC1_DCI_DEVICE_NAME,
            device_type=device_type_id,
            role=dci_role.id,
            site=dc1_site.id,
            status="active",
        )
        if platform_id:
            create_kwargs["platform"] = platform_id
        new_dev = nb.dcim.devices.create(**create_kwargs)
        try:
            new_dev.tags = [{"name": demo_tag.name}]
            new_dev.save()
        except Exception as e:
            print(
                f'  Created "{new_dev.name}" but could not auto-tag it '
                f'("{DEMO_TAG_NAME}"): {e}. Add the tag manually in the UI.'
            )
        else:
            print(f'  Created "{new_dev.name}" in {NEW_SITE_1_NAME}, tagged.')

    existing_dc2_dci = get_or_none(nb.dcim.devices, name=DC2_DCI_DEVICE_NAME)
    if existing_dc2_dci:
        print(f'Device "{DC2_DCI_DEVICE_NAME}" already exists, skipping creation.')
    else:
        create_kwargs = dict(
            name=DC2_DCI_DEVICE_NAME,
            device_type=device_type_id,
            role=dci_role.id,
            site=dc2_site.id,
            status="active",
        )
        if platform_id:
            create_kwargs["platform"] = platform_id
        new_dev = nb.dcim.devices.create(**create_kwargs)
        try:
            new_dev.tags = [{"name": demo_tag.name}]
            new_dev.save()
        except Exception as e:
            print(
                f'  Created "{new_dev.name}" but could not auto-tag it '
                f'("{DEMO_TAG_NAME}"): {e}. Add the tag manually in the UI.'
            )
        else:
            print(f'  Created "{new_dev.name}" in {NEW_SITE_2_NAME}, tagged.')

    # -----------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------
    print("\n=== Done ===")
    print(f"DC1 now has {len(pod1_devices) + 3} device(s): "
          f"{len(pod1_devices)} re-homed leaf/spine + 2 new superspines "
          f"({DC1_SS1_NAME}, {DC1_SS2_NAME}) + 1 DCI router "
          f"({DC1_DCI_DEVICE_NAME}).")
    print(f"DC2 now has {len(pod2_devices) + 3} device(s): "
          f"{len(pod2_devices)} re-homed leaf/spine + 2 new superspines "
          f"({DC2_SS1_NAME}, {DC2_SS2_NAME}) + 1 DCI router "
          f"({DC2_DCI_DEVICE_NAME}).")
    print(
        f"\nOriginal {EXISTING_SUPERSPINE_1_NAME} / "
        f"{EXISTING_SUPERSPINE_2_NAME} were left untouched under "
        f"{SOURCE_SITE_NAME} -- delete them yourself in the UI once you've "
        "confirmed the new per-DC superspines and DCI routers are wired "
        "correctly, if you no longer need them."
    )
    print(
        "\nNOT done by this script, still needed:\n"
        "  - Cable the leaf/spine/superspine layers together within each "
        "DC to match your intended fabric (this script creates devices, "
        "not the cabling between them -- your original Pod1/Pod2 cabling "
        "connected into the OLD shared superspines, so the new per-DC "
        "superspines start with no links and need wiring in NetBox, e.g. "
        "under each device's Interfaces tab, or via a follow-up cabling "
        "script once you confirm interface names).\n"
        "  - Assign IP addresses / interfaces on the two new DCI routers "
        "and cable them together in NetBox so NRX can generate the "
        "DC1<->DC2 link.\n"
        "  - Decide and configure AS numbers for the eBGP session between "
        "the two DCI routers (e.g. AS 65001 for DC1, AS 65002 for DC2).\n"
        "  - Update nrx.conf if its site filter is scoped to SYD1 only -- "
        "it now needs to also cover DC1 and DC2 (or rely on the demo tag "
        "alone).\n"
        "  - Regenerate and redeploy: ./4_run_nrx.sh && "
        "sudo clab deploy -t <topology-name>.clab.yaml (the generated "
        "filename will likely change once it reflects DC1/DC2 rather than "
        "SYD1 -- check what ./4_run_nrx.sh actually names it)."
    )


if __name__ == "__main__":
    main()
