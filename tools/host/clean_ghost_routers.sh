#!/usr/bin/env bash
# clean_ghost_routers.sh
#
# Removes SR Linux routers left running from earlier, interrupted containerlab
# deploys. Docker no longer lists them, but their processes and management
# addresses are still alive on the lab's management bridge. They answer for
# IP addresses that the current routers were given, so:
#   - configure.py pushes configs to a ghost instead of the real router
#     ("Configuration deployed!" while the real router stays empty)
#   - the real router with that IP never gets an ARP reply ("No route to host")
#   - their memory is why ser10 is in swap with only 22 routers running
#
# Run it with the lab destroyed. It refuses if any clab-DC1-DC2 container
# still exists, because then it could not tell a ghost from a real router.
#
# USAGE (on the lab host, from the repo folder):
#   sudo clab des -t DC1-DC2.clab.yaml --cleanup
#   sudo bash tools/host/clean_ghost_routers.sh
# Output is also saved to ghost_cleanup.txt.

OUT="$(dirname "$(readlink -f "$0")")/ghost_cleanup.txt"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash $0"
    exit 1
fi
if docker ps -a --format '{{.Names}}' | grep -q '^clab-DC1-DC2-'; then
    echo "The lab is still deployed. First run: sudo clab des -t DC1-DC2.clab.yaml --cleanup"
    exit 1
fi
exec > >(tee "$OUT") 2>&1

srl_pids() {  # processes running an SR Linux binary
    for p in /proc/[0-9]*; do
        tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q '/opt/srlinux/' && echo "${p#/proc/}"
    done
}

echo "===== BEFORE ====="
free -h | head -2
echo "SR Linux processes still running with no lab deployed: $(srl_pids | wc -l)"
echo "sr_xdp_lc (one per router): $(pgrep -c sr_xdp_lc)"

echo
echo "===== containerd tasks Docker does not know about ====="
known=$(docker ps -aq --no-trunc)
orphans=""
if command -v ctr >/dev/null; then
    for id in $(ctr -n moby tasks ls -q 2>/dev/null); do
        echo "$known" | grep -q "^$id" || orphans="$orphans $id"
    done
    for id in $orphans; do
        echo "removing orphan task $id"
        ctr -n moby tasks kill -a -s SIGKILL "$id" 2>/dev/null
        sleep 1
        ctr -n moby tasks rm -f "$id" 2>/dev/null
        ctr -n moby containers rm "$id" 2>/dev/null
    done
fi
[ -z "$orphans" ] && echo "none found through containerd"

echo
echo "===== leftover SR Linux processes ====="
left=$(srl_pids)
if [ -n "$left" ]; then
    # Kill each ghost's whole container: the lowest PID in its PID namespace
    # (its init) takes the rest with it. Never touch the host's own namespace.
    host_ns=$(readlink /proc/1/ns/pid)
    ghost_ns=$(for p in $left; do readlink /proc/$p/ns/pid 2>/dev/null; done | sort -u | grep -vx "$host_ns")
    [ -z "$ghost_ns" ] && ghost_ns="none-found"
    inits=$(for q in /proc/[0-9]*; do
        ns=$(readlink $q/ns/pid 2>/dev/null) && echo "$ns ${q#/proc/}"
    done | grep -F "$ghost_ns" | sort -k1,1 -k2,2n | awk '!seen[$1]++ {print $2}')
    echo "killing ghost router containers (init PIDs): $(echo $inits)"
    for i in $inits; do kill -9 "$i" 2>/dev/null; done
    sleep 3
    left=$(srl_pids)
    [ -n "$left" ] && { echo "killing remaining SR Linux processes: $(echo $left)"; kill -9 $left 2>/dev/null; sleep 2; }
else
    echo "none"
fi

echo
echo "===== stale ARP and /etc/hosts entries ====="
for br in $(ip -o link show type bridge | awk -F': ' '{print $2}' | grep '^br-'); do
    ip neigh flush dev "$br" nud all 2>/dev/null
done
before=$(grep -ci 'clab-DC1-DC2' /etc/hosts)
sed -i '/CLAB-DC1-DC2-START/,/CLAB-DC1-DC2-END/d' /etc/hosts
sed -i '/clab-DC1-DC2-/Id' /etc/hosts
echo "removed $before old lab lines from /etc/hosts"

echo
echo "===== AFTER ====="
free -h | head -2
remaining=$(srl_pids | wc -l)
echo "SR Linux processes still running: $remaining"

echo
echo "===== VERDICT ====="
if [ "$remaining" -eq 0 ]; then
    echo "Clean. Redeploy the lab without the Windows hosts (commands in the thread)."
else
    echo "$remaining SR Linux processes survived. Reboot ser10 to clear them, then redeploy."
fi
echo
echo "Saved to $OUT"
