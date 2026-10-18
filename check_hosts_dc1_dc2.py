#!/usr/bin/env python3
"""
check_hosts_dc1_dc2.py

Finds where traffic between two end hosts of the running DC1-DC2 lab stops.
It walks the path one hop at a time and prints the first one that fails with
what to do about it. Everything is also saved to host_check.txt.

Checks, in order:
  1. source host: address on eth1 and routes via its leaf
  2. source leaf: host port up, gateway address configured, port in the
     default network-instance, ARP entry for the host
  3. pings from the source host: its gateway, its leaf's loopback, the remote
     leaf's loopback, the remote host
  4. routes to the remote host's /28 on the source leaf and on both DCI
     gateways (OSPF inside a DC, eBGP over the DCI)
  5. the same host and leaf checks on the remote side (the return path)

USAGE (on the lab host, after configure_hosts_dc1_dc2.py):
  python3 check_hosts_dc1_dc2.py                 # dc1-h1 -> dc2-h1
  python3 check_hosts_dc1_dc2.py dc1-h3 dc2-h5   # any two hosts in the plan
"""

import os
import re
import sys
import ipaddress
import subprocess

try:
    import yaml
except ImportError:
    print("pyyaml is needed in this Python environment.")
    print("Run: pip install pyyaml   (inside your venv)")
    sys.exit(1)


HERE = os.path.dirname(os.path.abspath(__file__))
PLAN_FILE = os.path.join(HERE, "ip_plan_dc1_dc2.yaml")
OUT_FILE = os.path.join(HERE, "host_check.txt")
PREFIX = "clab-DC1-DC2-"

log_lines = []


def say(msg=""):
    print(msg)
    log_lines.append(msg)


def run(container, cmd):
    """Run a shell command in a lab container; return (rc, output)."""
    r = subprocess.run(
        ["docker", "exec", PREFIX + container, "sh", "-c", cmd],
        capture_output=True,
        text=True,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def srl(router, cli):
    """Run an sr_cli command on an SR Linux router."""
    return run(router, f'/opt/srlinux/bin/sr_cli -d "{cli}"')[1]


def network(prefix_cidr):
    """172.30.1.2/28 -> 172.30.1.0/28"""
    return str(ipaddress.ip_interface(prefix_cidr).network)


def dc_of(name):
    return int(re.match(r"dc(\d+)-", name).group(1))


def leaf_loopback(leaf):
    out = srl(leaf, "info from state interface lo0 subinterface 0 ipv4 address")
    m = re.search(r"(172\.20\.\d+\.\d+)/32", out)
    return m.group(1) if m else None


def ping(host, target):
    rc, out = run(host, f"ping -c2 -W1 {target}")
    return rc == 0


def check_host(h):
    """Host side: address and routes. Returns a problem string or None."""
    say(f"\n--- host {h['name']} ---")
    rc, out = run(h["name"], f"ip -br addr show {h['host_if']}; ip route; ip neigh")
    say(out)
    if rc != 0 and "No such container" in out:
        return f"{h['name']} is not running. Redeploy the lab."
    if h["host_ip"].split("/")[0] not in out:
        return (
            f"{h['name']} has no {h['host_ip']} on {h['host_if']}. "
            "Run: python3 configure_hosts_dc1_dc2.py"
        )
    gw = h["leaf_ip"].split("/")[0]
    if f"via {gw}" not in out:
        return f"{h['name']} has no routes via its leaf {gw}. Run: python3 configure_hosts_dc1_dc2.py"
    return None


def check_leaf(h):
    """Leaf side of a host port. Returns a problem string or None."""
    leaf, port = h["leaf"], h["leaf_if"]
    say(f"\n--- leaf {leaf} {port} (gateway for {h['name']}) ---")
    oper = srl(leaf, f"info from state interface {port} oper-state")
    say(oper)
    addr = srl(leaf, f"info flat interface {port} subinterface 0 ipv4")
    say(addr)
    ni = srl(leaf, "info flat network-instance default interface")
    in_ni = f"{port}.0" in ni
    say(f"{port}.0 in network-instance default: {in_ni}")
    arp = srl(leaf, f"show arpnd arp-entries interface {port}")
    say(arp)
    if "No such container" in oper:
        return f"{leaf} is not running. Redeploy the lab."
    if h["leaf_ip"] not in addr:
        return (
            f"{leaf} {port} has no gateway address {h['leaf_ip']}. NetBox is missing the "
            f"host cable or the address: run python3 add_hosts_dc1_dc2.py, "
            f"python3 assign_ips_dc1_dc2.py, then ./5_run_config_mgmt.sh"
        )
    if not in_ni:
        return f"{leaf} {port}.0 is not in the default network-instance. Re-run ./5_run_config_mgmt.sh"
    if "up" not in oper:
        return (
            f"{leaf} {port} is down: the containerlab link to {h['name']} is missing. "
            "Redeploy: sudo clab dep -t DC1-DC2.clab.yaml --reconfigure"
        )
    return None


def route_on(router, prefix):
    out = srl(router, "show network-instance default route-table")
    lines = [l for l in out.splitlines() if prefix in l]
    return lines


def main():
    src_name = sys.argv[1] if len(sys.argv) > 1 else "dc1-h1"
    dst_name = sys.argv[2] if len(sys.argv) > 2 else "dc2-h1"

    with open(PLAN_FILE) as f:
        hosts = {h["name"]: h for h in (yaml.safe_load(f).get("hosts") or [])}
    for n in (src_name, dst_name):
        if n not in hosts:
            print(f"{n} is not in the hosts: list of {PLAN_FILE}")
            sys.exit(1)
    src, dst = hosts[src_name], hosts[dst_name]
    src_gw_router = f"dc{dc_of(src_name)}-dci-r1"
    dst_gw_router = f"dc{dc_of(dst_name)}-dci-r1"
    dst_net = network(dst["host_ip"])
    src_net = network(src["host_ip"])

    say(f"Path check: {src_name} ({src['host_ip']}) -> {dst_name} ({dst['host_ip']})")

    problems = []
    for check in (check_host(src), check_leaf(src)):
        if check:
            problems.append(check)

    say("\n--- pings from " + src_name + " ---")
    src_lo = leaf_loopback(src["leaf"])
    dst_lo = leaf_loopback(dst["leaf"])
    steps = [
        ("its gateway " + src["leaf_ip"].split("/")[0], src["leaf_ip"].split("/")[0]),
        (f"its leaf loopback {src_lo}", src_lo),
        (f"remote leaf loopback {dst_lo}", dst_lo),
        (f"remote host {dst['host_ip'].split('/')[0]}", dst["host_ip"].split("/")[0]),
    ]
    results = {}
    for label, target in steps:
        ok = bool(target) and ping(src_name, target)
        results[label] = ok
        say(f"  {label:38} {'OK' if ok else 'FAIL'}")

    say("\n--- routes to " + dst_net + " and back to " + src_net + " ---")
    route_table = [
        (src["leaf"], dst_net),
        (src_gw_router, dst_net),
        (dst_gw_router, dst_net),
        (dst_gw_router, src_net),
        (dst["leaf"], src_net),
    ]
    missing_routes = []
    for router, prefix in route_table:
        lines = route_on(router, prefix)
        say(f"  {router:12} {prefix:16} {'present' if lines else 'MISSING'}")
        for l in lines:
            say("      " + l.strip())
        if not lines:
            missing_routes.append((router, prefix))

    for check in (check_leaf(dst), check_host(dst)):
        if check:
            problems.append(check)

    say("\n===== VERDICT =====")
    labels = [s[0] for s in steps]
    if results[labels[3]]:
        say(f"{src_name} reaches {dst_name}. The path works.")
    elif problems:
        say(problems[0])
        for p in problems[1:]:
            say("Also: " + p)
    elif not results[labels[0]]:
        say(
            f"{src_name} cannot reach its gateway although both sides are configured. "
            "Check the ARP output above: no entry means the link carries no traffic; "
            "redeploy with --reconfigure."
        )
    elif missing_routes:
        router, prefix = missing_routes[0]
        if router == dst_gw_router and prefix == dst_net:
            say(
                f"{dst_gw_router} has no OSPF route to {prefix}: {dst['leaf']} is not "
                "advertising its host port. Re-run ./5_run_config_mgmt.sh"
            )
        elif router == src_gw_router or (router == dst_gw_router and prefix == src_net):
            say(
                f"{router} has no route to {prefix}: the DCI eBGP is not carrying host LANs. "
                "The gateways need the dc-hosts statement in dci-export; re-run ./5_run_config_mgmt.sh"
            )
        else:
            say(
                f"{router} has no route to {prefix}: its gateway is not redistributing BGP into "
                "OSPF (bgp-to-ospf). Re-run ./5_run_config_mgmt.sh"
            )
    else:
        say("Every hop is configured and routed but pings fail. Paste host_check.txt back.")

    with open(OUT_FILE, "w") as f:
        f.write("\n".join(log_lines) + "\n")
    print(f"\nSaved to {OUT_FILE}")


if __name__ == "__main__":
    main()
