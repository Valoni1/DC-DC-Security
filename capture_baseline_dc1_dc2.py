#!/usr/bin/env python3
"""
capture_baseline_dc1_dc2.py

Records the state of the running DC1-DC2 lab as the phase-one baseline, so
later experiments can be compared against it. Run it once on the lab host
while the single-DCI baseline is deployed and configured, before the lab is
redeployed for phase two.

It saves, under results/baseline_<date-time>/:
  - lab_host.txt     kernel, Docker and containerlab versions, running containers
  - <router>.txt     for each of the 22 routers: version, interfaces, OSPF and
                     BGP neighbours, IPv4 route table (DCI routers also get the
                     BGP routes they receive and advertise)
  - pings.txt        loopback pings DC1 -> DC2 and DC2 -> DC1, and timed host
                     pings in both directions for every host pair dcN-hX
  - summary.txt      one line per router (OSPF full/expected, BGP established/
                     total) and per ping, then a verdict

USAGE (on the lab host, from the repo folder, after ./5_run_config_mgmt.sh
and configure_hosts_dc1_dc2.py):
  python3 capture_baseline_dc1_dc2.py
"""

import os
import re
import sys
import datetime
import subprocess

try:
    import yaml
except ImportError:
    print("pyyaml is needed in this Python environment.")
    print("Run: pip install pyyaml   (inside your venv)")
    sys.exit(1)


HERE = os.path.dirname(os.path.abspath(__file__))
PLAN_FILE = os.path.join(HERE, "ip_plan_dc1_dc2.yaml")
PREFIX = "clab-DC1-DC2-"

ROUTER_CMDS = [
    "show version",
    "show interface brief",
    "show network-instance default protocols ospf neighbor",
    "show network-instance default protocols bgp neighbor",
    "show network-instance default route-table ipv4-unicast summary",
]
DCI_CMDS = [
    "show network-instance default protocols bgp routes ipv4 summary",
]


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def run(container, cmd):
    """Run a shell command in a lab container; return (rc, output)."""
    r = subprocess.run(
        ["docker", "exec", PREFIX + container, "sh", "-c", cmd],
        capture_output=True,
        text=True,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def srl(router, cli):
    return run(router, f'/opt/srlinux/bin/sr_cli "{cli}"')[1]


def dc_of(name):
    return re.match(r"(dc\d+)-", name).group(1)


def ping_summary(out):
    """Pull sent/received, rtt and TTL out of Linux ping output."""
    m = re.search(r"(\d+) packets transmitted, (\d+) received", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    ttl = re.search(r"ttl=(\d+)", out)
    if not m:
        return "no ping output", False
    sent, recv = int(m.group(1)), int(m.group(2))
    txt = f"{recv}/{sent} received"
    if rtt:
        txt += f", rtt min/avg/max/mdev {rtt.group(1)}/{rtt.group(2)}/{rtt.group(3)}/{rtt.group(4)} ms"
    if ttl:
        txt += f", TTL {ttl.group(1)}"
    return txt, recv == sent


def main():
    with open(PLAN_FILE) as f:
        plan = yaml.safe_load(f)

    routers = sorted(plan["loopbacks"])
    hosts = [h for h in plan.get("hosts", []) if h["os"] == "linux"]

    # Expected OSPF adjacencies: every link inside one DC (the DCI is not in OSPF)
    expected_ospf = {r: 0 for r in routers}
    for l in plan["links"]:
        if dc_of(l["a"]) == dc_of(l["b"]):
            expected_ospf[l["a"]] += 1
            expected_ospf[l["b"]] += 1

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    out_dir = os.path.join(HERE, "results", f"baseline_{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    summary = []
    problems = []

    def note(line):
        print(line)
        summary.append(line)

    note(f"Baseline capture {datetime.datetime.now().isoformat(timespec='seconds')}")
    note(f"Output folder: {out_dir}\n")

    with open(os.path.join(out_dir, "lab_host.txt"), "w") as f:
        for cmd in ["uname -a", "docker version --format '{{.Server.Version}}'",
                    "which docker", "containerlab version",
                    "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep " + PREFIX,
                    "free -g"]:
            f.write(f"$ {cmd}\n{sh(cmd)}\n\n")

    note("ROUTERS (OSPF full/expected, BGP established/total)")
    for r in routers:
        dump = []
        cmds = ROUTER_CMDS + (DCI_CMDS if "dci" in r else [])
        res = {}
        for c in cmds:
            res[c] = srl(r, c)
            dump.append(f"### {c}\n{res[c]}\n")
        with open(os.path.join(out_dir, f"{r}.txt"), "w") as f:
            f.write("\n".join(dump))

        if "No such container" in res[ROUTER_CMDS[0]]:
            note(f"  {r:12} NOT RUNNING")
            problems.append(f"{r} is not running")
            continue
        # SR Linux tables separate columns with spaces, not "|"; the state is the
        # lower-case word "full" (the header says "State", so it never matches)
        ospf_full = len(re.findall(r"\sfull\s", res[ROUTER_CMDS[2]]))
        bgp_text = res[ROUTER_CMDS[3]]
        m = re.search(r"(\d+) configured neighbors, (\d+) configured sessions are established", bgp_text)
        if m:
            bgp_total, bgp_est = int(m.group(1)), int(m.group(2))
        else:
            bgp_est = len(re.findall(r"\sestablished\s", bgp_text))
            bgp_total = bgp_est + len(re.findall(
                r"\s(idle|active|connect|opensent|openconfirm)\s", bgp_text))
        bgp_other = bgp_total - bgp_est
        flag = ""
        if ospf_full != expected_ospf[r]:
            flag += "  <-- OSPF"
            problems.append(f"{r}: {ospf_full} OSPF neighbours full, expected {expected_ospf[r]}")
        if bgp_other:
            flag += "  <-- BGP"
            problems.append(f"{r}: {bgp_other} BGP session(s) not established")
        dci_txt = ""
        if "dci" in r:
            # The DCI peer's row carries "ipv4-unicast [Rx/Active/Tx]"
            m = re.search(r"\|\s*dci\s*\|.*?ipv4-unicast \[(\d+/\d+/\d+)\]", bgp_text, re.S)
            dci_txt = f"   DCI ipv4 Rx/Active/Tx {m.group(1)}" if m else "   DCI prefixes: not found"
        note(f"  {r:12} OSPF {ospf_full}/{expected_ospf[r]}   BGP {bgp_est}/{bgp_total}{dci_txt}{flag}")

    note("\nPINGS")
    ping_log = []
    # Loopback to loopback, both directions (the 12 October test and its reverse)
    lo = plan["loopbacks"]
    for src, dst in [("dc1-l1", "dc2-l1"), ("dc2-l1", "dc1-l1")]:
        s_ip, d_ip = lo[src].split("/")[0], lo[dst].split("/")[0]
        cmd = f"ip netns exec srbase-default ping -c 10 -i 0.2 -I {s_ip} {d_ip}"
        rc, out = run(src, cmd)
        ping_log.append(f"### {src} lo0 -> {dst} lo0: {cmd}\n{out}\n")
        txt, ok = ping_summary(out)
        note(f"  {src} lo0 -> {dst} lo0: {txt}")
        if not ok:
            problems.append(f"loopback ping {src} -> {dst}: {txt}")

    # Host to host, both directions, matching host numbers across the DCs
    by_name = {h["name"]: h for h in hosts}
    for h in hosts:
        if not h["name"].startswith("dc1-"):
            continue
        peer = by_name.get(h["name"].replace("dc1-", "dc2-", 1))
        if not peer:
            continue
        for a, b in [(h, peer), (peer, h)]:
            d_ip = b["host_ip"].split("/")[0]
            cmd = f"ping -c 20 -i 0.2 {d_ip}"
            rc, out = run(a["name"], cmd)
            ping_log.append(f"### {a['name']} -> {b['name']}: {cmd}\n{out}\n")
            txt, ok = ping_summary(out)
            note(f"  {a['name']} -> {b['name']}: {txt}")
            if not ok:
                problems.append(f"host ping {a['name']} -> {b['name']}: {txt}")
    with open(os.path.join(out_dir, "pings.txt"), "w") as f:
        f.write("\n".join(ping_log))

    note("\n===== VERDICT =====")
    if problems:
        note("Baseline NOT clean:")
        for p in problems:
            note(f"  - {p}")
    else:
        note("Baseline clean: every router has its expected OSPF neighbours, every BGP session is "
             "established, and all loopback and host pings succeed in both directions.")

    with open(os.path.join(out_dir, "summary.txt"), "w") as f:
        f.write("\n".join(summary) + "\n")


if __name__ == "__main__":
    main()
