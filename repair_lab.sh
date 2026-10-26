#!/usr/bin/env bash
# repair_lab.sh
#
# Finishes a deploy that stopped part-way (for example "dc2-h1 has no address
# on eth1") without destroying the lab, and records everything it sees:
#   1. which lab containers are running, and the host's memory
#   2. whether every host has its eth1 interface
#   3. host addresses and routes (configure_hosts_dc1_dc2.py), retried while
#      interfaces are still appearing
#   4. router configs again (./5_run_config_mgmt.sh) if the DCI routers have
#      no established BGP session
#   5. waits for the routes between the DCs, then the host check, DVWA setup,
#      and the DCI routers' BGP and BFD state
#
# All output goes to the screen and to results/repair_<date-time>.txt, so it
# can be pushed instead of pasted.
#
# USAGE (from the repo folder, NETBOX_TOKEN exported):
#   ./repair_lab.sh
#   git add results && git commit -m "repair log" && git push

cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p results
LOG="results/repair_$(date +%Y%m%d_%H%M%S).txt"
exec > >(tee "$LOG") 2>&1
P=clab-DC1-DC2-
HOSTS="dc1-h1 dc1-h2 dc1-h3 dc1-h4 dc1-h5 dc1-h6 dc2-h1 dc2-h2 dc2-h3 dc2-h4 dc2-h5 dc2-h6"
cli() { docker exec "$P$1" /opt/srlinux/bin/sr_cli "$2" 2>&1; }

echo "Repair run $(date -Is)"
echo "===== 1. containers ====="
docker ps -a --filter "name=$P" --format '{{.Names}}\t{{.Status}}' | sort
echo "running: $(docker ps --filter "name=$P" -q | wc -l)   not running: $(docker ps -a --filter "name=$P" --filter status=exited -q | wc -l)"
free -g

echo "===== 2. host interfaces ====="
missing=0
for h in $HOSTS; do
    if docker exec "$P$h" ip link show eth1 >/dev/null 2>&1; then
        echo "  $h: eth1 present"
    else
        echo "  $h: eth1 MISSING ($(docker inspect -f '{{.State.Status}}' "$P$h" 2>&1))"
        missing=$((missing + 1))
    fi
done

echo "===== 3. host addresses ====="
source ./venv/bin/activate
for try in 1 2 3; do
    python3 configure_hosts_dc1_dc2.py 2>&1 | grep -v -- '-w1'
    ok=1
    for h in $HOSTS; do
        docker exec "$P$h" ip -4 -o addr show eth1 2>/dev/null | grep -q 172.30 || ok=0
    done
    [ $ok = 1 ] && break
    echo "  some hosts still have no address on eth1; waiting 20 s and retrying ($try/3)"
    sleep 20
done
for h in $HOSTS; do
    echo "  $h: $(docker exec "$P$h" ip -4 -o addr show eth1 2>&1 | awk '{print $4}' | tr '\n' ' ')"
done

echo "===== 4. router configs ====="
routers() { docker ps --filter "name=^$P" --format '{{.Names}} {{.Image}}' | awk '/srlinux/ {print $1}' | sort; }
ip_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$1" | awk '{print $1}'; }
dead_routers() {  # routers whose JSON-RPC does not answer from this host
    for r in $(routers); do
        code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://$(ip_of "$r")/jsonrpc")
        [ "$code" = "000" ] && echo "$r"
    done
}
echo "  routers running: $(routers | wc -l)"
dead=$(dead_routers)
for i in $(seq 1 12); do   # routers can take a few minutes to boot after a deploy
    [ -z "$dead" ] && break
    echo "  $(echo "$dead" | wc -l) router(s) not answering on management yet; waiting 15 s ($i/12)"
    sleep 15; dead=$(dead_routers)
done
if [ -n "$dead" ]; then
    echo "  still not answering: $(echo $dead)"
    echo "  running the management-bridge fix (the same problem as on 19 October)"
    sudo bash tools/host/fix_mgmt_bridge.sh | tail -30
    dead=$(dead_routers)
    echo "  after the fix, not answering: ${dead:-none}"
fi
NB="${NETBOX_URL:-http://localhost:8000}"
echo "  NetBox at $NB: HTTP $(curl -s -o /dev/null -m 5 -w '%{http_code}' -H "Authorization: Token $NETBOX_TOKEN" "$NB/api/status/")"

bgp=$(cli dc1-dci-r1 "show network-instance default protocols bgp neighbor" | grep -oE '[0-9]+ configured sessions are established')
echo "  dc1-dci-r1: ${bgp:-no BGP output}"
if ! echo "$bgp" | grep -qE '^[1-9]'; then
    if [ -z "$NETBOX_TOKEN" ]; then
        echo "  BGP is not up and NETBOX_TOKEN is not set: export it and run this script again."
    elif [ -n "$dead" ]; then
        echo "  BGP is not up, but routers do not answer on management, so the configs cannot be pushed."
    else
        echo "  BGP is not up: pushing the router configs again (stops after 10 minutes at most)"
        timeout 600 ./5_run_config_mgmt.sh 2>&1 | grep -iE "deploying|deployed|skipping|error|failed|timed out"
    fi
fi

echo "===== 5. routes between the DCs ====="
for i in $(seq 1 36); do
    if cli dc1-l1 "show network-instance default route-table ipv4-unicast prefix 172.30.2.0/28" | grep -q "172.30.2.0/28"; then
        echo "  dc1-l1 has a route to DC2's hosts after $((i * 5)) s"; break
    fi
    [ "$i" = 36 ] && echo "  dc1-l1 still has NO route to DC2's hosts after 180 s"
    sleep 5
done

echo "===== 6. host check ====="
python3 check_hosts_dc1_dc2.py

echo "===== 7. DVWA ====="
python3 setup_dvwa_dc1_dc2.py

echo "===== 8. DCI routers: BGP and BFD ====="
for r in dc1-dci-r1 dc2-dci-r1; do
    echo "--- $r"
    cli $r "show network-instance default protocols bgp neighbor"
    cli $r "info from state /bfd network-instance default"
done

echo
echo "Log saved to $LOG. Push it: git add results && git commit -m 'repair log' && git push"
