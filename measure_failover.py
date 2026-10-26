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
It then repairs the link, and appends a row to results/failover_results.csv and
to results/failover_results_v2.csv. The v2 file adds the BFD detection time
from the router's own log, the BFD diagnostic, and whether the run is valid:
a silent failure must end in "Control Detection Time Expired", a port shutdown
in "Path Down".

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
import fcntl
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


def router_bfd_event(router, t_fail):
    """The first BFD down event the router logged after the failure, from its
    own log (microsecond timestamps): (seconds after the failure, diagnostic)."""
    out = sh(["docker", "exec", PREFIX + router, "sh", "-c",
              "find /var/log/srlinux -type f -mmin -3 2>/dev/null | xargs grep -h bfdDownEvent 2>/dev/null"])
    events = []
    for line in out.splitlines():
        m = re.match(r"(\S+) .*Session from \S+ to (\S+) has transitioned to the Down state "
                     r"with local-diagnostic code: ([^(]+)", line)
        if not m:
            continue
        try:
            t = datetime.datetime.fromisoformat(m.group(1)).timestamp()
        except ValueError:
            continue
        if t >= t_fail - 1:
            events.append((t - t_fail, m.group(3).strip(), m.group(2)))
    return min(events) if events else None


def link_snapshot(router):
    """State of the primary DCI port as SR Linux and the kernel see it."""
    st = state(router, f"/interface[name={PRIMARY_IF}]")
    keep = {}
    if isinstance(st, dict):
        keep = {k: st.get(k) for k in ("admin-state", "oper-state", "oper-down-reason", "last-change")}
        sub = next((x for x in st.get("subinterface", []) if x.get("index") == 0), {})
        keep["subinterface-oper-state"] = sub.get("oper-state")
        keep["subinterface-last-change"] = sub.get("last-change")
        keep["arp"] = sub.get("ipv4", {}).get("arp", {}).get("neighbor")
    dev = kernel_if(PRIMARY_IF)
    keep["kernel"] = sh(f"nsenter -t {pid_of(router)} -n sh -c 'ip -d -s link show {dev}; tc qdisc show dev {dev}'")
    return keep


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
        links_before = {gw: link_snapshot(gw) for gw in GATEWAYS}
        t_fail = time.time()
        break_link(args.fail)
        links_after = {}  # taken 0.3 s after the break, beside the polling below so it is not delayed

        def snap_after():
            time.sleep(0.3)
            links_after.update({gw: link_snapshot(gw) for gw in GATEWAYS})
        snapper = threading.Thread(target=snap_after, daemon=True)
        snapper.start()
        print(f"link broken ({args.fail}) at {datetime.datetime.fromtimestamp(t_fail):%H:%M:%S.%f}"[:-3])
        # Watch BFD and BGP separately. SR Linux deletes the BFD session as soon
        # as BGP (its only client) lets go of it, so "gone" counts as down too.
        last_bfd = None  # the primary's BFD session as last seen UP
        bfd_trace = []   # (seconds after the failure, BFD packets received, sent) while it stayed UP
        while ping.poll() is None and time.time() - t_fail < args.duration:
            if t_bfd is None:
                p = bfd_peer(GATEWAYS[0], primary_peer)
                if p is not None and p.get("session-state") == "UP":
                    last_bfd = p
                    a = p.get("async", {})
                    bfd_trace.append((round(time.time() - t_fail, 3), a.get("received-packets"),
                                      a.get("transmitted-packets")))
                else:
                    t_bfd = time.time()
                    print(f"BFD to {primary_peer} went down after {t_bfd - t_fail:.2f}s (seen by polling)")
            if bgp_state(GATEWAYS[0], primary_peer) != "established":
                t_detect = time.time()
                print(f"BGP to {primary_peer} dropped after {t_detect - t_fail:.2f}s (seen by polling)")
                break
            time.sleep(0.1)
        # The routers' own timestamps are more exact than polling: when BGP left
        # established, why (last-event), and when the primary last heard BFD
        far_peer = f"10.1.0.{int(primary_peer.rsplit('.', 1)[1]) ^ 1}"  # dc1's end of the primary link
        record = {"link_broken_at": datetime.datetime.fromtimestamp(t_fail).astimezone().isoformat(),
                  "primary_bfd_last_seen_up": last_bfd}
        for gw, peer in ((GATEWAYS[0], primary_peer), (GATEWAYS[1], far_peer)):
            nb = state(gw, f"/network-instance[name=default]/protocols/bgp/neighbor[peer-address={peer}]")
            record[gw] = {"bgp_neighbor": nb,
                          "bfd_sessions": state(gw, "/bfd/network-instance[name=default]/peer"),
                          "router_log_bfd": sh(["docker", "exec", PREFIX + gw, "sh", "-c",
                                                "find /var/log/srlinux -type f -mmin -3 2>/dev/null | "
                                                "xargs grep -hiE 'bfd|neighbor 10\\.1\\.0' 2>/dev/null | tail -40"])}
            try:
                left = datetime.datetime.fromisoformat(nb["last-established"].replace("Z", "+00:00")).timestamp()
                print(f"{gw}: BGP to {peer} left established {left - t_fail:.2f}s after the failure "
                      f"(router clock), cause {nb.get('last-establish-exit-event')}")
            except (TypeError, KeyError, ValueError):
                print(f"{gw}: no router timestamp for BGP to {peer}")
        snapper.join(timeout=10)
        record["primary_bfd_packet_trace"] = bfd_trace
        rx = [(t, int(r)) for t, r, _ in bfd_trace if r is not None]
        if rx and rx[-1][1] > rx[0][1]:
            t_last = next(t for t, r in rx if r == rx[-1][1])  # first poll that saw the final count
            print(f"primary BFD still received {rx[-1][1] - rx[0][1]} packet(s) after the failure, "
                  f"the last by {t_last:.2f}s")
        record["primary_link_before"], record["primary_link_after_0.3s"] = links_before, links_after
        ev = router_bfd_event(GATEWAYS[0], t_fail)
        exit_event = (record[GATEWAYS[0]]["bgp_neighbor"] or {}).get("last-establish-exit-event") \
            if isinstance(record[GATEWAYS[0]]["bgp_neighbor"], dict) else None
        router_detect, bfd_diag = (ev[0], ev[1]) if ev else (None, None)
        expected = {"silent": "Control Detection Time Expired", "down": "Path Down"}[args.fail]
        valid = bfd_diag == expected
        record["router_detect_s"], record["bfd_diag"], record["valid"] = router_detect, bfd_diag, valid
        if ev:
            print(f"{GATEWAYS[0]} log: BFD went down {router_detect:.3f}s after the failure, "
                  f"diagnostic '{bfd_diag}'; BGP exit event {exit_event}")
        print(f"RUN VALID: BFD diagnostic is '{bfd_diag}' as a {args.fail} failure should give" if valid else
              f"RUN NOT VALID as a {args.fail} failure: expected BFD diagnostic '{expected}', got '{bfd_diag}'")
        for gw in GATEWAYS:
            b, a = links_before[gw], links_after.get(gw, {})
            print(f"{gw} {PRIMARY_IF}: oper-state {b.get('oper-state')} -> {a.get('oper-state')}, "
                  f"subinterface {b.get('subinterface-oper-state')} -> {a.get('subinterface-oper-state')}"
                  + (f", down reason {a.get('oper-down-reason')}" if a.get("oper-down-reason") else ""))
        if last_bfd:
            rx = last_bfd.get("async", {}).get("last-packet-received")
            print(f"primary BFD last packet received (router clock): {rx}")
        snap = os.path.join(os.path.dirname(RESULTS),
                            f"bfd_{args.fail}_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        with open(snap, "w") as f:
            json.dump(record, f, indent=2, default=str)
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

    if args.fail != "none":
        # Second results file with the router-measured detection and a validity
        # flag (failover_results.csv keeps its original columns)
        v2 = RESULTS.replace(".csv", "_v2.csv")
        first = not os.path.exists(v2)
        with open(v2, "a", newline="") as f:
            w = csv.writer(f)
            if first:
                w.writerow(["time", "fail", "src", "dst", "sent", "received", "lost", "outage_s",
                            "poll_bgp_detect_s", "router_bfd_detect_s", "bfd_diag", "bgp_exit_event",
                            "valid", "primary_pkts_after", "backup_pkts_after", "recovery_s"])
            w.writerow([datetime.datetime.now().isoformat(timespec="seconds"), args.fail, args.src, args.dst,
                        sent, received, sent - received, f"{outage:.3f}" if outage is not None else "",
                        f"{t_detect - t_fail:.2f}" if t_detect else "",
                        f"{router_detect:.3f}" if router_detect is not None else "", bfd_diag or "",
                        exit_event or "", valid, delta(mid, after, PRIMARY_IF), delta(mid, after, BACKUP_IF),
                        f"{back:.0f}" if back is not None else ""])

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

    # Two runs at once break the same link twice and spoil both: the 26 October
    # batch lost 23 silent runs that way (the port shutdown of a parallel
    # "down" run turned them into Path Down failures)
    lock = open("/tmp/measure_failover.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Another measure_failover.py is running. Run the batches one after the other.")

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
