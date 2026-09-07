#!/usr/bin/env python3
"""
cable_dc1_dc2.py

Creates the cabling (NetBox Cable objects) for the DC1 and DC2 fabrics
built by restructure_dc1_dc2.py, plus the single DCI link joining the two
data centres. Written against NetBox 4.x's a_terminations/b_terminations
cable API, verified against NetBox's own current documentation and
community examples for this exact version line -- not guessed.

CABLING PLAN (identical pattern applied to both DC1 and DC2, deterministic,
not "pick any free port" -- every interface used is fixed and printed
before anything is created):

  Leaf lN         -> ethernet-1/1 to spine sp1
                  -> ethernet-1/2 to spine sp2
  Spine sp1       -> ethernet-1/1..1/6 to leaves l1..l6 (matching index)
                  -> ethernet-1/7 to superspine ss1
                  -> ethernet-1/8 to superspine ss2
  Spine sp2       -> same pattern as sp1
  Superspine ss1  -> ethernet-1/1 to sp1, ethernet-1/2 to sp2,
                     ethernet-1/3 to the DC's dc-gateway
  Superspine ss2  -> same pattern as ss1
  dc-gateway      -> ethernet-1/1 to ss1, ethernet-1/2 to ss2,
                     ethernet-1/3 to the OTHER DC's dc-gateway (the DCI link)

That is 12 (leaf-spine) + 4 (spine-superspine) + 2 (superspine-gateway)
= 18 links per DC, x2 DCs = 36, plus 1 DCI link = 37 links total.

IDEMPOTENCY: before creating any cable, the script checks whether either
end's interface already has a cable attached. If so, that specific link is
SKIPPED (not overwritten, not duplicated) and reported in the summary. This
makes it safe to re-run after a partial failure.

REQUIREMENTS:
  pip install pynetbox   (already in your venv)

USAGE:
  export NETBOX_URL="http://localhost:8000"
  export NETBOX_TOKEN="your-api-token-here"
  python3 cable_dc1_dc2.py --dry-run     # see the full link list, no changes
  python3 cable_dc1_dc2.py               # actually create the cables
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

# Real device names, taken directly from restructure_dc1_dc2.py's own
# printed output earlier in this session -- not guessed.
DC1_LEAVES = [f"syd1-pd1-l{i}" for i in range(1, 7)]
DC1_SP1 = "syd1-pd1-sp1"
DC1_SP2 = "syd1-pd1-sp2"
DC1_SS1 = "dc1-ss1"
DC1_SS2 = "dc1-ss2"
DC1_GW = "dc1-dci-r1"

DC2_LEAVES = [f"syd1-pd2-l{i}" for i in range(1, 7)]
DC2_SP1 = "syd1-pd2-sp1"
DC2_SP2 = "syd1-pd2-sp2"
DC2_SS1 = "dc2-ss1"
DC2_SS2 = "dc2-ss2"
DC2_GW = "dc2-dci-r1"


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


def get_device(nb, name):
    dev = nb.dcim.devices.get(name=name)
    if not dev:
        fail(f'Device "{name}" not found. Check the name is exactly right.')
    return dev


def get_interface(nb, device, iface_name):
    iface = nb.dcim.interfaces.get(device_id=device.id, name=iface_name)
    if not iface:
        fail(
            f'Interface "{iface_name}" not found on device "{device.name}". '
            "Check the interface naming matches what your device type "
            "actually provides."
        )
    return iface


def build_dc_links(leaves, sp1, sp2, ss1, ss2, gw):
    """Returns a list of (device_a, iface_a, device_b, iface_b) tuples for
    one DC's internal fabric, following the deterministic plan described
    in this file's docstring."""
    links = []

    # Leaf <-> Spine (full mesh: each leaf to both spines)
    for idx, leaf in enumerate(leaves, start=1):
        links.append((leaf, "ethernet-1/1", sp1, f"ethernet-1/{idx}"))
        links.append((leaf, "ethernet-1/2", sp2, f"ethernet-1/{idx}"))

    # Spine <-> Superspine (full mesh: each spine to both superspines)
    links.append((sp1, "ethernet-1/7", ss1, "ethernet-1/1"))
    links.append((sp1, "ethernet-1/8", ss2, "ethernet-1/1"))
    links.append((sp2, "ethernet-1/7", ss1, "ethernet-1/2"))
    links.append((sp2, "ethernet-1/8", ss2, "ethernet-1/2"))

    # Superspine <-> dc-gateway
    links.append((ss1, "ethernet-1/3", gw, "ethernet-1/1"))
    links.append((ss2, "ethernet-1/3", gw, "ethernet-1/2"))

    return links


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the full link list; make no changes.",
    )
    args = parser.parse_args()

    nb = connect()
    print(f"Connected to NetBox at {NETBOX_URL}\n")

    # -----------------------------------------------------------------
    # Build the full link plan for both DCs plus the DCI link.
    # -----------------------------------------------------------------
    dc1_links = build_dc_links(DC1_LEAVES, DC1_SP1, DC1_SP2, DC1_SS1, DC1_SS2, DC1_GW)
    dc2_links = build_dc_links(DC2_LEAVES, DC2_SP1, DC2_SP2, DC2_SS1, DC2_SS2, DC2_GW)
    dci_link = [(DC1_GW, "ethernet-1/3", DC2_GW, "ethernet-1/3")]

    all_links = dc1_links + dc2_links + dci_link

    print(f"=== Planned links ({len(all_links)} total) ===")
    print(f"DC1 internal: {len(dc1_links)}")
    print(f"DC2 internal: {len(dc2_links)}")
    print(f"DCI link: {len(dci_link)}")
    print()
    for a_dev, a_if, b_dev, b_if in all_links:
        print(f"  {a_dev}:{a_if}  <->  {b_dev}:{b_if}")

    if args.dry_run:
        print("\n--dry-run set: no changes made. Re-run without --dry-run to apply.")
        return

    confirm = input(f'\nType "yes" to create these {len(all_links)} cables: ').strip().lower()
    if confirm != "yes":
        print("Aborted. No changes made.")
        return

    # -----------------------------------------------------------------
    # Resolve every device name once (avoids repeated lookups) and verify
    # they all exist before creating anything.
    # -----------------------------------------------------------------
    print("\n=== Resolving devices ===")
    device_names = set()
    for a_dev, _, b_dev, _ in all_links:
        device_names.add(a_dev)
        device_names.add(b_dev)

    devices = {}
    for name in sorted(device_names):
        devices[name] = get_device(nb, name)
        print(f'  OK: "{name}" (id={devices[name].id})')

    # -----------------------------------------------------------------
    # Create cables, skipping any link where either end is already cabled.
    # -----------------------------------------------------------------
    print("\n=== Creating cables ===")
    created = 0
    skipped = 0
    failed = 0

    for a_dev_name, a_if_name, b_dev_name, b_if_name in all_links:
        a_dev = devices[a_dev_name]
        b_dev = devices[b_dev_name]
        a_iface = get_interface(nb, a_dev, a_if_name)
        b_iface = get_interface(nb, b_dev, b_if_name)

        if a_iface.cable is not None:
            print(
                f"  SKIP: {a_dev_name}:{a_if_name} already has a cable "
                f"(id={a_iface.cable.id})."
            )
            skipped += 1
            continue
        if b_iface.cable is not None:
            print(
                f"  SKIP: {b_dev_name}:{b_if_name} already has a cable "
                f"(id={b_iface.cable.id})."
            )
            skipped += 1
            continue

        try:
            nb.dcim.cables.create(
                a_terminations=[
                    {"object_type": "dcim.interface", "object_id": a_iface.id}
                ],
                b_terminations=[
                    {"object_type": "dcim.interface", "object_id": b_iface.id}
                ],
                status="connected",
            )
            print(f"  OK: {a_dev_name}:{a_if_name} <-> {b_dev_name}:{b_if_name}")
            created += 1
        except Exception as e:
            print(
                f"  FAILED: {a_dev_name}:{a_if_name} <-> {b_dev_name}:{b_if_name} "
                f"-- {e}"
            )
            failed += 1

    print("\n=== Done ===")
    print(f"Created: {created}   Skipped (already cabled): {skipped}   Failed: {failed}")
    if failed:
        print(
            "\nSome cables failed to create -- re-run this script after "
            "checking the error messages above; it is safe to re-run since "
            "already-created cables will be skipped, not duplicated."
        )


if __name__ == "__main__":
    main()
