#!/usr/bin/env bash
# deploy_lab.sh
#
# Redeploys the DC1-DC2 lab and configures it, in one go:
#   1. destroys the running lab and removes its old clab-DC1-DC2 folder
#   2. deploys it, with the Windows hosts unless --without-windows is given
#      (the 8-router lab leaves room for them; they need /dev/kvm and
#      windows/win2022.iso, and are left out with a warning when either is missing)
#   3. pushes the router configs (./5_run_config_mgmt.sh)
#   4. sets the host addresses and routes (configure_hosts_dc1_dc2.py)
#   5. checks that dc1-h1 reaches dc2-h1 (check_hosts_dc1_dc2.py)
#
# Steps 3-5 need NetBox: export NETBOX_TOKEN (and NETBOX_URL if it is not
# http://localhost:8000) first. Run it as your normal user; it uses sudo for
# containerlab.
#
# USAGE (from the repo folder):
#   ./deploy_lab.sh
#   ./deploy_lab.sh --without-windows

TOPO="DC1-DC2.clab.yaml"
cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p results
LOG="results/deploy_$(date +%Y%m%d_%H%M%S).txt"
exec > >(tee "$LOG") 2>&1   # everything below is also saved, to push instead of paste

if [ -z "$NETBOX_TOKEN" ]; then
    echo "NETBOX_TOKEN is not set; export it first (steps 3-5 need NetBox)." >&2
    exit 1
fi

echo "===== 1. destroy ====="
sudo clab des -t "$TOPO" --cleanup
# Nodes that were in an older, larger topology are not in $TOPO any more, so
# the destroy above does not see them; remove whatever is left of the lab.
left=$(docker ps -aq --filter label=containerlab=DC1-DC2)
[ -n "$left" ] && echo "removing $(echo $left | wc -w) leftover lab containers" && docker rm -f $left >/dev/null
sudo rm -rf clab-DC1-DC2

echo "===== 2. deploy ====="
# The topology has a kind:bridge node named clab-ext for the external/untrusted
# segment (198.51.100.0/29). containerlab needs that Linux bridge to exist
# first; we make it as a docker network so the ScopeWeaver compose stack can
# join the same L2 later (see scopeweaver/setup_scopeweaver.sh). Idempotent.
if ! docker network inspect clab-ext >/dev/null 2>&1; then
    echo "creating docker network clab-ext (external segment 198.51.100.0/29)"
    docker network create --subnet 198.51.100.0/29 --gateway 198.51.100.6 \
        -o com.docker.network.bridge.name=clab-ext clab-ext >/dev/null
fi
# The attacker node uses lab-kali:latest (Kali + manual-attack tools). Build it
# once if missing; redeploys then reuse the cached image. Needs network for apt.
if ! docker image inspect lab-kali:latest >/dev/null 2>&1; then
    echo "building lab-kali:latest (Kali manual-attacker image; first build only)"
    docker build -t lab-kali:latest kali/ || { echo "WARNING: lab-kali build failed; the attacker node will not deploy until it succeeds."; }
fi
WINDOWS=yes
[ "$1" = "--without-windows" ] && WINDOWS=no
if [ $WINDOWS = yes ] && [ ! -e /dev/kvm ]; then
    echo "WARNING: /dev/kvm is missing, so the Windows hosts cannot run; deploying without them."
    WINDOWS=no
fi
if [ $WINDOWS = yes ] && [ ! -s windows/win2022.iso ]; then
    echo "WARNING: windows/win2022.iso is missing; deploying without the Windows hosts."
    echo "         Download the Windows Server 2022 evaluation ISO, save it as windows/win2022.iso"
    echo "         and run this script again to add them (see README, End hosts)."
    WINDOWS=no
fi
if [ $WINDOWS = yes ]; then
    mkdir -p windows/dc1-w1 windows/dc2-w1
    sudo clab dep -t "$TOPO" || exit 1
else
    NODES=$(grep -oE '^        [a-z0-9-]+:$' "$TOPO" | tr -d ' :' | grep -v -- '-w1$' | paste -sd, -)
    sudo clab dep -t "$TOPO" --node-filter "$NODES" || exit 1
fi

echo "===== 3. router configs ====="
./5_run_config_mgmt.sh 2>&1 | grep -iE "deployed|skipping|error|failed"

echo "===== 4. hosts ====="
source ./venv/bin/activate
python3 configure_hosts_dc1_dc2.py   # the Windows hosts fail here when they are not deployed

echo "===== 4b. waiting for routes between the DCs ====="
# BGP over the DCI and the redistribution into OSPF take a while after the
# configs are pushed; checking straight away reports missing routes.
for i in $(seq 1 36); do
    if docker exec clab-DC1-DC2-dc1-l1 /opt/srlinux/bin/sr_cli \
        "show network-instance default route-table ipv4-unicast prefix 172.30.2.0/28" 2>/dev/null \
        | grep -q "172.30.2.0/28"; then
        echo "dc1-l1 has a route to DC2's hosts after $((i * 5)) s"
        break
    fi
    sleep 5
done

echo "===== 5. check ====="
python3 check_hosts_dc1_dc2.py | sed -n '/===== VERDICT =====/,$p'

echo "===== 6. memory ====="
free -g
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' | grep clab-DC1-DC2 | sort

echo
echo "Log saved to $LOG. Push it: git add results && git commit -m 'deploy log' && git push"
