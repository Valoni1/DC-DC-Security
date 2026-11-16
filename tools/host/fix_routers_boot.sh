#!/usr/bin/env bash
# fix_routers_boot.sh
#
# For when every SR Linux router container is "Up" but none answers JSON-RPC
# and sr_cli prints nothing, even though the management bridge is correct
# (fix_mgmt_bridge.sh reports "Bridge and cables are now correct but routers
# still do not answer"). That means SR Linux itself did not finish booting
# inside the containers.
#
# The script:
#   1. looks inside two routers: container log, SR Linux processes, the
#      management address, sr_cli and JSON-RPC from inside the router
#   2. collects the host's AppArmor denials and runs diagnose_srl_boot.sh --fix
#      (the 18 October cause: AppArmor's unix-chkpwd profile stops sudo
#      inside the routers, so SR Linux never starts)
#   3. if the routers did not boot, redeploys the lab with deploy_lab.sh and
#      checks every router's JSON-RPC again
#
# Everything is saved to results/routers_boot_<date-time>.txt to push.
#
# USAGE (from the repo folder, as your normal user, NETBOX_TOKEN exported):
#   bash tools/host/fix_routers_boot.sh
#   git add results && git commit -m "routers boot log" && git push

cd "$(dirname "$(readlink -f "$0")")/../.." || exit 1
mkdir -p results
LOG="results/routers_boot_$(date +%Y%m%d_%H%M%S).txt"
exec > >(tee "$LOG") 2>&1
P=clab-DC1-DC2-
section() { printf '\n===== %s =====\n' "$1"; }
routers() { docker ps --filter "name=^$P" --format '{{.Names}} {{.Image}}' | awk '/srlinux/ {print $1}' | sort; }
ip_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$1" | awk '{print $1}'; }
answering() {
    n=0
    for r in $(routers); do
        [ "$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://$(ip_of "$r")/jsonrpc")" != "000" ] && n=$((n + 1))
    done
    echo $n
}

echo "Router boot check $(date -Is)"
uptime
[ -z "$NETBOX_TOKEN" ] && echo "WARNING: NETBOX_TOKEN is not set; the redeploy in step 3 needs it."

section "1. INSIDE THE ROUTERS"
for r in dc1-l1 dc2-dci-r1; do
    c=$P$r
    echo "--- $r  (management address $(ip_of "$c"))"
    echo "[container log, last 25 lines]"
    docker logs --tail 25 "$c" 2>&1
    echo "[SR Linux processes]"
    docker exec "$c" sh -c 'ps -eo comm | grep -E "^sr_" | sort | uniq -c | head -40; echo "sr_ processes: $(ps -eo comm | grep -c "^sr_")"' 2>&1
    echo "[management namespace]"
    docker exec "$c" sh -c 'ip netns list; ip netns exec srbase-mgmt ip -4 addr show 2>&1 | grep -E "inet|mgmt"' 2>&1
    echo "[sr_cli show version]"
    timeout 30 docker exec "$c" /opt/srlinux/bin/sr_cli "show version" 2>&1 | head -15
    echo "[JSON-RPC from inside the router]"
    docker exec "$c" sh -c "ip netns exec srbase-mgmt curl -s -o /dev/null -m 3 -w 'HTTP %{http_code}\n' http://127.0.0.1/jsonrpc" 2>&1
done

section "2. APPARMOR AND THE SUDO TEST"
sudo sh -c 'journalctl -k --since "-2h" 2>/dev/null || dmesg' | grep -i 'apparmor="DENIED"' | tail -10
echo "AppArmor denials in the last 2 hours: $(sudo sh -c 'journalctl -k --since "-2h" 2>/dev/null || dmesg' | grep -ci 'apparmor="DENIED"')"
sudo bash tools/host/diagnose_srl_boot.sh --fix >/dev/null 2>&1
sed -n '/===== SUDO TESTS/,$p' tools/host/srl_diag.txt 2>/dev/null | tail -40

section "3. ROUTERS ANSWERING JSON-RPC"
n=$(answering)
echo "before: $n of $(routers | wc -l)"
if [ "$n" -lt "$(routers | wc -l)" ]; then
    echo "Routers did not boot: redeploying the lab (deploy_lab.sh)"
    ./deploy_lab.sh 2>&1 | grep -vE '^\s*$' | tail -60
    n=$(answering)
    echo "after the redeploy: $n of $(routers | wc -l) routers answer JSON-RPC"
fi

section "VERDICT"
if [ "$n" = "$(routers | wc -l)" ] && [ "$n" -gt 0 ]; then
    echo "All routers answer. The lab is back; run the failover test next."
else
    echo "Routers still do not answer. Push this log."
fi
echo "Log saved to $LOG. Push it: git add results && git commit -m 'routers boot log' && git push"
