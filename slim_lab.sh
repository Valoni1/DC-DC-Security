#!/usr/bin/env bash
# slim_lab.sh
#
# One run that moves the running lab on ser10 to the smaller DC1-DC2 lab
# (November 2026): one DC gateway, one superspine, one spine and one leaf per
# DC, one Linux host (dcN-h1) and one Windows host (dcN-w1) per DC, no DVWA.
# Both DCI links, BFD and the failover policy stay. 22 routers become 8,
# which frees about 28 GB of router memory.
#
#   1. records the starting point: branch, memory, running lab containers
#   2. updates NetBox to the smaller plan: prune_netbox_dc1_dc2.py deletes the
#      removed routers and hosts and the addresses left on their links;
#      add_hosts_dc1_dc2.py adds any host cable that is missing;
#      assign_ips_dc1_dc2.py sets the addresses; tag_dc1_dc2.py keeps tags right
#   3. redeploys the lab with ./deploy_lab.sh (destroys the old 22-router lab,
#      including nodes the new topology no longer lists, deploys, pushes the
#      router configs, configures the hosts, checks dc1-h1 -> dc2-h1)
#   4. checks the result: router count, OSPF/BGP/BFD on the DCI routers, host
#      reachability (Linux and, if deployed, Windows), memory in use
#   5. one failover run of each kind (silent, then down, one after the other)
#      to show the DCI failover still works on the smaller lab
#
# Everything goes to the screen and to results/slim_<date-time>.txt.
# Nothing is pushed: push results/ afterwards (command printed at the end).
#
# USAGE (on ser10, from the repo folder, NETBOX_TOKEN exported or in ~/.netbox_env):
#   git fetch origin && git checkout claude/project-thread-dmnsoh && git pull
#   ./slim_lab.sh
#   ./slim_lab.sh --without-windows     # leave the Windows hosts out on purpose

cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p results
STAMP=$(date +%Y%m%d_%H%M%S)
LOG="results/slim_$STAMP.txt"
exec > >(tee "$LOG") 2>&1
P=clab-DC1-DC2-
cli() { docker exec "$P$1" /opt/srlinux/bin/sr_cli "$2" 2>&1; }
VERDICT=()
pass() { VERDICT+=("PASS  $1"); }
failv() { VERDICT+=("FAIL  $1"); }

echo "slim_lab.sh run $(date -Is) on $(hostname)"
[ -z "$NETBOX_TOKEN" ] && [ -f ~/.netbox_env ] && source ~/.netbox_env
export NETBOX_URL="${NETBOX_URL:-http://localhost:8000}"
if [ -z "$NETBOX_TOKEN" ]; then
    echo "NETBOX_TOKEN is not set and ~/.netbox_env does not set it; export it and run again."
    exit 1
fi

echo "===== 1. starting point ====="
echo "branch $(git rev-parse --abbrev-ref HEAD) at $(git log -1 --format='%h %s')"
free -g
echo "lab containers running: $(docker ps -q --filter label=containerlab=DC1-DC2 | wc -l)"
docker ps --filter label=containerlab=DC1-DC2 --format '{{.Names}}' | sort | paste -sd' ' -
echo "NetBox $NETBOX_URL: HTTP $(curl -s -o /dev/null -m 5 -w '%{http_code}' -H "Authorization: Token $NETBOX_TOKEN" "$NETBOX_URL/api/status/")"

echo "===== 2. NetBox ====="
source ./venv/bin/activate
echo "--- 2a. prune (what goes)"
# Look first: only the removed routers and hosts may be deleted. Anything else
# in the list means NetBox holds something this run did not expect, so stop.
python3 prune_netbox_dc1_dc2.py --dry-run > results/prune_dryrun_$STAMP.txt 2>&1
cat results/prune_dryrun_$STAMP.txt
unexpected=$(sed -n '/=== Devices to delete/,/^$/p' results/prune_dryrun_$STAMP.txt | grep '^  ' \
    | tr -d ' ' | grep -vE '^dc[12]-(l[2-6]|sp2|ss2|h[2-6])$')
if [ -n "$unexpected" ]; then
    echo "STOPPED: the prune would also delete $(echo $unexpected), which this change does not remove."
    echo "Nothing was changed. Log saved to $LOG; push it so this can be looked at."
    exit 1
fi
python3 prune_netbox_dc1_dc2.py --yes || failv "NetBox prune reported failures" 
echo "--- 2b. hosts and cables"
echo yes | python3 add_hosts_dc1_dc2.py
echo "--- 2c. addresses"
echo yes | python3 assign_ips_dc1_dc2.py
echo "--- 2d. tags"
echo yes | python3 tag_dc1_dc2.py
echo "--- 2e. check: a second prune must find nothing"
if python3 prune_netbox_dc1_dc2.py --dry-run | tee /dev/stderr | grep -q "Nothing to remove"; then
    pass "NetBox matches the smaller plan"
else
    failv "NetBox still has devices, cables or addresses the plan does not list"
fi
python3 assign_ips_dc1_dc2.py --dry-run | grep -E "WARNING|Nothing to change|Changes"
deactivate

echo "===== 3. redeploy ====="
./deploy_lab.sh "$@"
# deploy_lab.sh keeps its own log in results/ as well

echo "===== 4. checks ====="
routers=$(docker ps --filter label=containerlab=DC1-DC2 --format '{{.Names}} {{.Image}}' | awk '/srlinux/' | wc -l)
echo "SR Linux routers running: $routers"
[ "$routers" = 8 ] && pass "8 routers running" || failv "$routers routers running, expected 8"
left=$(docker ps -a --filter label=containerlab=DC1-DC2 --format '{{.Names}}' | grep -E -- '-l[2-6]$|-sp2$|-ss2$|-h[2-6]$|-h6-(db|web)$')
[ -z "$left" ] && pass "no removed node is left" || failv "removed nodes still present: $(echo $left)"

for r in dc1-dci-r1 dc2-dci-r1; do
    echo "--- $r BGP"
    cli $r "show network-instance default protocols bgp neighbor"
    echo "--- $r BFD"
    cli $r "info from state /bfd network-instance default"
done
for r in dc1-sp1 dc2-sp1; do
    echo "--- $r OSPF neighbours"
    cli $r "show network-instance default protocols ospf neighbor"
done
est=$(cli dc1-dci-r1 "show network-instance default protocols bgp neighbor" | grep -oE '[0-9]+ configured sessions are established' | awk '{print $1}')
[ -n "$est" ] && [ "$est" -ge 2 ] && pass "dc1-dci-r1: $est BGP sessions established (both DCI links)" \
    || failv "dc1-dci-r1: ${est:-no} BGP sessions established, expected at least 2"

echo "--- host pings"
source ./venv/bin/activate
if docker exec ${P}dc1-h1 ping -c 5 -W 1 172.30.2.2 | tee /dev/stderr | grep -q " 0% packet loss"; then
    pass "dc1-h1 -> dc2-h1 (Linux to Linux across the DCI)"
else
    failv "dc1-h1 -> dc2-h1"; python3 check_hosts_dc1_dc2.py dc1-h1 dc2-h1
fi
for w in dc1-w1 dc2-w1; do
    if [ "$(docker inspect -f '{{.State.Running}}' $P$w 2>/dev/null)" != true ]; then
        echo "  $w is not deployed"
        failv "$w not deployed (see the deploy warnings above)"
        continue
    fi
    ip=$(awk -v n="$w" '$0 ~ "name: "n"," {match($0, /host_ip: [0-9.]+/); print substr($0, RSTART+9, RLENGTH-9)}' ip_plan_dc1_dc2.yaml)
    echo "  $w container address $ip; Windows install progress (last log lines):"
    docker logs --tail 5 $P$w 2>&1 | sed 's/^/    /'
    # The container holds the address, so this checks the routed path to it;
    # Windows itself answers only once installed and its firewall allows ping.
    if docker exec ${P}dc1-h1 ping -c 3 -W 1 "$ip" | grep -q " 0% packet loss"; then
        pass "dc1-h1 -> $w ($ip) answers"
    else
        failv "dc1-h1 -> $w ($ip) no answer (normal while Windows is still installing)"
    fi
done
deactivate

echo "--- memory"
free -g
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' | grep "$P" | sort
avail=$(free -g | awk '/^Mem:/ {print $7}')
echo "available memory: ${avail} GB"

echo "===== 5. failover check (one silent, one down, in turn) ====="
# One run of each, sequentially, only to confirm failover still works after
# the change; the D0 batch is run separately.
for mode in silent down; do
    out=$(sudo python3 measure_failover.py --fail $mode 2>&1)
    echo "$out" | tail -30
    if echo "$out" | grep -q "RUN VALID"; then
        pass "failover $mode run valid"
    else
        failv "failover $mode run not valid (see above)"
    fi
done

echo
echo "===== VERDICT ====="
printf '%s\n' "${VERDICT[@]}"
echo
echo "Log saved to $LOG. Push it:"
echo "  git add results && git commit -m 'slim lab run' && git push"
