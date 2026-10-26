#!/usr/bin/env bash
# deploy_lab.sh
#
# Redeploys the DC1-DC2 lab and configures it, in one go:
#   1. destroys the running lab and removes its old clab-DC1-DC2 folder
#   2. deploys it, without the Windows hosts unless --with-windows is given
#      (with them the lab does not fit in ser10's memory)
#   3. pushes the router configs (./5_run_config_mgmt.sh)
#   4. sets the host addresses and routes (configure_hosts_dc1_dc2.py)
#   5. checks that dc1-h1 reaches dc2-h1 (check_hosts_dc1_dc2.py)
#   6. sets up DVWA on dc1-h6/dc2-h6, replicated DC1 -> DC2 (setup_dvwa_dc1_dc2.py)
#
# Steps 3-5 need NetBox: export NETBOX_TOKEN (and NETBOX_URL if it is not
# http://localhost:8000) first. Run it as your normal user; it uses sudo for
# containerlab.
#
# USAGE (from the repo folder):
#   ./deploy_lab.sh
#   ./deploy_lab.sh --with-windows

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
sudo rm -rf clab-DC1-DC2

echo "===== 2. deploy ====="
if [ "$1" = "--with-windows" ]; then
    sudo clab dep -t "$TOPO" || exit 1
else
    NODES=$(grep -oE '^        [a-z0-9-]+:$' "$TOPO" | tr -d ' :' | grep -v -- '-w1$' | paste -sd, -)
    sudo clab dep -t "$TOPO" --node-filter "$NODES" || exit 1
fi

echo "===== 3. router configs ====="
./5_run_config_mgmt.sh 2>&1 | grep -iE "deployed|skipping|error|failed"

echo "===== 4. hosts ====="
source ./venv/bin/activate
python3 configure_hosts_dc1_dc2.py   # the Windows hosts fail when they are not deployed

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

echo "===== 6. DVWA ====="
python3 setup_dvwa_dc1_dc2.py | sed -n '/===== VERDICT =====/,$p'

echo
echo "Log saved to $LOG. Push it: git add results && git commit -m 'deploy log' && git push"
