#!/usr/bin/env python3
"""
measure_failover.py

Measures how long traffic between two end hosts stops when the primary DCI
link fails, and shows that it moves to the backup link.

It sends a steady ping stream (100 packets/s by default) from one host to a
host in the other DC, breaks the primary DCI link (ethernet-1/3 between
dc1-dci-r1 and dc2-dci-r1) part-way through, and reports:
  - packets sent, received and lost, and the outage (longest gap between
    replies)
  - how long BGP took to drop the session on the primary link (detection)
  - how many packets crossed each DCI link, before and during the test
It then repairs the link, and appends a row to results/failover_results.csv.

Failure modes (--fail):
  silent  the link stays up but carries nothing (100% loss in both directions
          with tc netem). Only BFD or the BGP hold timer can notice this; it
          is also what an attacker dropping or blackholing DCI traffic causes.
  down    the primary port on dc1-dci-r1 is shut (admin-state disable).
  none    no failure: a baseline of the steady stream.

Only the Python standard library is used, so it runs with sudo outside the
venv. sudo is needed for "silent" (tc runs in the routers' network namespace).

USAGE (on the lab host, from the repo folder, lab deployed and configured):
  sudo python3 measure_failover.py                      # silent failure, dc1-h1 -> dc2-h1
  sudo python3 measure_failover.py --fail down
  sudo python3 measure_failover.py --fail none --duration 20
  sudo python3 measure_failover.py --src dc1-h3 --dst dc2-h5 --repeat 5
"""

import argparse
import base64
import csv
import datetime
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results", "failover_results.csv")
PREFIX = "clab-DC1-DC2-"
GATEWAYS = ("dc1-dci-r1", "dc2-dci-r1")
PRIMARY_IF, BACKUP_IF = "ethernet-1/3", "ethernet-1/4"
SRL_USER, SRL_PASS = "admin", "NokiaSrl1!"


def sh(cmd, check=False):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"Failed: {cmd}\n{r.stdout}{r.stderr}")
    return r.stdout.strip()


def docker_ip(node):
    return sh(["docker", "inspect", "-f",
               "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", PREFIX + node]).split()[0]


def jsonrpc(router, method, commands):
    req = urllib.request.Request(
        f"http://{docker_ip(router)}/jsonrpc",
        data=json.dumps({"jsonrpc": "2.0", "id": 0, "method": method,
                         "params": {"commands": commands}}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(f"{SRL_USER}:{SRL_PASS}".encode()).decode()},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read())
    if "error" in body:
        raise RuntimeError(f"{router}: {body['error']}")
    return body["result"]


def state(router, path):
    try:
        return jsonrpc(router, "get", [{"path": path, "datastore": "state"}])[0]
    except Exception as e:
        return f"error: {e}"


def out_packets(router, iface):
    v = state(router, f"/interface[name={iface}]/statistics/out-packets")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def bgp_state(router, peer):
    return state(router, f"/network-instance[name=default]/protocols/bgp/neighbor[peer-address={peer}]/session-state")


def bfd_peer(router, peer):
    """The router's BFD session state for peer (a dict), or None if it has none.
    Read over JSON-RPC: the SR Linux CLI has no top-level "show bfd" command."""
    def sessions(node):  # every dict carrying a remote-address, however it is nested
        if isinstance(node, dict):
            if "remote-address" in node:
                yield node
            for v in node.values():
                yield from sessions(v)
        elif isinstance(node, list):
            for v in node:
                yield from sessions(v)
    return next((p for p in sessions(state(router, "/bfd/network-instance[name=default]/peer"))
                 if p["remote-address"] == peer), None)


def bfd_up(router, peer):
    """True while the BFD session to peer is UP, False once it leaves UP,
    None if there is no session at all (BFD not running)."""
    p = bfd_peer(router, peer)
    if p is None:
        return None
    return p.get("session-state") == "UP"


def pid_of(node):
    return sh(["docker", "inspect", "-f", "{{.State.Pid}}", PREFIX + node])


def kernel_if(iface):  # ethernet-1/3 -> e1-3
    return "e" + iface.split("-", 1)[1].replace("/", "-")


def break_link(mode):
    if mode == "silent":
        for gw in GATEWAYS:
            sh(f"nsenter -t {pid_of(gw)} -n tc qdisc replace dev {kernel_if(PRIMARY_IF)} root netem loss 100%", check=True)
    elif mode == "down":
        jsonrpc(GATEWAYS[0], "set", [{"action": "update",
                                       "path": f"/interface[name={PRIMARY_IF}]/admin-state", "value": "disable"}])


def repair_link(mode):
    if mode == "silent":
        for gw in GATEWAYS:
            sh(f"nsenter -t {pid_of(gw)} -n tc qdisc del dev {kernel_if(PRIMARY_IF)} root")
    elif mode == "down":
        jsonrpc(GATEWAYS[0], "set", [{"action": "update",
                                       "path": f"/interface[name={PRIMARY_IF}]/admin-state", "value": "enable"}])


def dci_peer_ip(iface):
    """dc2-dci-r1's address on the given DCI link, from the plan addressing on dc1's side."""
    addr = state(GATEWAYS[0], f"/interface[name={iface}]/subinterface[index=0]/ipv4/address")
    m = re.search(r"10\.1\.0\.(\d+)/31", json.dumps(addr))
    return f"10.1.0.{int(m.group(1)) ^ 1}" if m else None


def wait_primary_back(peer, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if bgp_state(GATEWAYS[0], peer) == "established":
            return time.time() - t0
        time.sleep(1)
    return None


def run_once(args, dst_ip, primary_peer):
    print(f"\n=== {args.fail} failure: {args.src} -> {args.dst} ({dst_ip}), "
          f"{args.duration}s at {1 / args.interval:.0f} pkt/s, failure at {args.at}s ===")
    before = {i: out_packets(GATEWAYS[0], i) for i in (PRIMARY_IF, BACKUP_IF)}
    print(f"BGP to {primary_peer} (primary): {bgp_state(GATEWAYS[0], primary_peer)}")
    p0 = bfd_peer(GATEWAYS[0], primary_peer)
    print(f"BFD to {primary_peer} (primary): " + (
        f"{p0.get('session-state')}, transmit interval {p0.get('active-transmit-interval')} us, "
        f"remote minimum receive {p0.get('remote-minimum-receive-interval')} us" if p0
        else "no session (BFD not running)"))

    ping = subprocess.Popen(
        ["docker", "exec", PREFIX + args.src, "ping", "-D", "-n", "-i", str(args.interval),
         "-W", "1", "-w", str(args.duration), dst_ip],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    reader = threading.Thread(target=lambda: lines.extend(ping.stdout), daemon=True)
    reader.start()

    t_fail = t_detect = t_bfd = None
    if args.fail != "none":
        time.sleep(args.at)
        mid = {i: out_packets(GATEWAYS[0], i) for i in (PRIMARY_IF, BACKUP_IF)}
        t_fail = time.time()
        break_link(args.fail)
        print(f"link broken ({args.fail}) at {datetime.datetime.fromtimestamp(t_fail):%H:%M:%S.%f}"[:-3])
        # Watch BFD and BGP separately: BFD should declare the peer down first
        # (interval x multiplier), then BGP drops the session on that event.
        while ping.poll() is None and time.time() - t_fail < args.duration:
            if t_bfd is None and bfd_up(GATEWAYS[0], primary_peer) is False:
                t_bfd = time.time()
                print(f"BFD to {primary_peer} went down after {t_bfd - t_fail:.2f}s")
            if bgp_state(GATEWAYS[0], primary_peer) != "established":
                t_detect = time.time()
                print(f"BGP to {primary_peer} dropped after {t_detect - t_fail:.2f}s")
                break
            time.sleep(0.1)
        # One snapshot of the BFD peer table for the record
        snap = os.path.join(os.path.dirname(RESULTS),
                            f"bfd_{args.fail}_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        # The router's own timestamps (last-failure-time, last-state-transition)
        # are more exact than this script's polling
        with open(snap, "w") as f:
            json.dump({"bfd_sessions": state(GATEWAYS[0], "/bfd/network-instance[name=default]/peer"),
                       "bgp_neighbor": state(GATEWAYS[0], "/network-instance[name=default]/protocols/bgp/"
                                                          f"neighbor[peer-address={primary_peer}]"),
                       "link_broken_at": datetime.datetime.fromtimestamp(t_fail).astimezone().isoformat()},
                      f, indent=2, default=str)
        print(f"BFD and BGP state saved to {os.path.relpath(snap, HERE)}")
    else:
        mid = before

    ping.wait()
    reader.join(timeout=5)
    after = {i: out_packets(GATEWAYS[0], i) for i in (PRIMARY_IF, BACKUP_IF)}

    replies = [float(m.group(1)) for l in lines if (m := re.match(r"\[(\d+\.\d+)\].*icmp_seq=\d+", l))]
    summary = next((l for l in lines if "packets transmitted" in l), "")
    sent = int(re.search(r"(\d+) packets transmitted", summary).group(1)) if summary else 0
    received = len(replies)
    gaps = [b - a for a, b in zip(replies, replies[1:])]
    outage = max(gaps) - args.interval if gaps else None

    def delta(a, b, i):
        return b[i] - a[i] if a[i] is not None and b[i] is not None else None

    print(f"\nsent {sent}, received {received}, lost {sent - received}"
          f" ({(sent - received) / sent * 100 if sent else 0:.1f}%)")
    print(f"outage (longest gap between replies): {outage:.3f}s" if outage is not None else "outage: no replies")
    if t_bfd:
        print(f"BFD detection time: {t_bfd - t_fail:.2f}s")
    elif args.fail != "none":
        print("BFD did not go down (BFD not running, or the failure did not block it)")
    if t_detect:
        print(f"BGP detection time: {t_detect - t_fail:.2f}s")
        if t_bfd:
            print(f"  (BGP dropped {t_detect - t_bfd:.2f}s after BFD; the rest is reconvergence)")
    elif args.fail != "none":
        print(f"BGP session on the primary stayed up for the whole {args.duration}s run")
    for i in (PRIMARY_IF, BACKUP_IF):
        print(f"dc1-dci-r1 {i} packets out: before failure {delta(before, mid, i)}, after {delta(mid, after, i)}")

    if args.fail != "none":
        repair_link(args.fail)
        back = wait_primary_back(primary_peer)
        print(f"link repaired; BGP on the primary " +
              (f"re-established after {back:.0f}s" if back is not None else "NOT back after 180s"))

    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    new = not os.path.exists(RESULTS)
    with open(RESULTS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "fail", "src", "dst", "interval_s", "duration_s", "sent", "received",
                        "lost", "outage_s", "bgp_detect_s", "primary_pkts_after", "backup_pkts_after"])
        w.writerow([datetime.datetime.now().isoformat(timespec="seconds"), args.fail, args.src, args.dst,
                    args.interval, args.duration, sent, received, sent - received,
                    f"{outage:.3f}" if outage is not None else "",
                    f"{t_detect - t_fail:.2f}" if t_detect else "",
                    delta(mid, after, PRIMARY_IF), delta(mid, after, BACKUP_IF)])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", default="dc1-h1")
    p.add_argument("--dst", default="dc2-h1")
    p.add_argument("--fail", choices=["silent", "down", "none"], default="silent")
    p.add_argument("--duration", type=int, default=40, help="seconds of traffic (default 40)")
    p.add_argument("--at", type=float, default=10, help="seconds in when the link breaks (default 10)")
    p.add_argument("--interval", type=float, default=0.01, help="seconds between pings (default 0.01)")
    p.add_argument("--repeat", type=int, default=1, help="run the test this many times")
    args = p.parse_args()

    if args.fail == "silent" and os.geteuid() != 0:
        sys.exit("The silent failure needs root: sudo python3 measure_failover.py")

    dst_ip = sh(["docker", "exec", PREFIX + args.dst, "sh", "-c",
                 "ip -4 -o addr show eth1 | awk '{print $4}' | cut -d/ -f1"])
    if not dst_ip:
        sys.exit(f"{args.dst} has no address on eth1. Run python3 configure_hosts_dc1_dc2.py first.")
    primary_peer, backup_peer = dci_peer_ip(PRIMARY_IF), dci_peer_ip(BACKUP_IF)
    if not backup_peer:
        sys.exit(f"dc1-dci-r1 has no address on {BACKUP_IF}: the second DCI link is not configured yet.")
    print(f"primary DCI peer {primary_peer} ({bgp_state(GATEWAYS[0], primary_peer)}), "
          f"backup DCI peer {backup_peer} ({bgp_state(GATEWAYS[0], backup_peer)})")

    for _ in range(args.repeat):
        # Tee everything this run prints into a per-run report file, so the
        # full output (BFD and BGP timings included) is saved, not just stdout.
        report = io.StringIO()
        real = sys.stdout

        class Tee:
            def write(self, s):
                real.write(s)
                report.write(s)

            def flush(self):
                real.flush()

        sys.stdout = Tee()
        try:
            run_once(args, dst_ip, primary_peer)
        finally:
            sys.stdout = real
        run_file = os.path.join(os.path.dirname(RESULTS),
                                f"run_{args.fail}_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
        os.makedirs(os.path.dirname(run_file), exist_ok=True)
        with open(run_file, "w") as f:
            f.write(report.getvalue())
        print(f"full run report saved to {os.path.relpath(run_file, HERE)}")
    print(f"\nResults appended to {RESULTS}")


if __name__ == "__main__":
    main()
