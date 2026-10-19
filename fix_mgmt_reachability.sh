#!/usr/bin/env bash
# fix_mgmt_reachability.sh
#
# Some SR Linux routers in the running DC1-DC2 lab are healthy (mgmt0 up with
# its address, apps waiting for config) yet ser10 gets "No route to host" when
# configure.py connects to them. That is an ARP failure on the containerlab
# management bridge: ser10 cannot resolve the router's IP to a MAC address.
#
# For every SR Linux router this script:
#   1. compares the MAC Docker gave the container with the MAC SR Linux uses
#      on mgmt0, and the MAC in ser10's neighbour (ARP) table
#   2. checks for two routers sharing one mgmt0 MAC
#   3. tests whether the router's JSON-RPC answers
# then flushes the bridge's neighbour table (including static entries),
# re-tests, and prints which routers are fixed and which still fail and why.
# Everything is saved to mgmt_fix.txt.
#
# USAGE (on the lab host, from the repo folder):
#   sudo bash fix_mgmt_reachability.sh
# Then: ./5_run_config_mgmt.sh

LAB="clab-DC1-DC2-"
OUT="$(dirname "$(readlink -f "$0")")/mgmt_fix.txt"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash $0"
    exit 1
fi
exec > >(tee "$OUT") 2>&1

routers=$(docker ps --filter "name=^${LAB}" --format '{{.Names}} {{.Image}}' | awk '/srlinux/ {print $1}' | sort)
[ -z "$routers" ] && { echo "No running SR Linux routers named ${LAB}*"; exit 1; }

reachable() {  # JSON-RPC answers on port 80 at all (any HTTP status)
    curl -s -o /dev/null -m 3 -w '%{http_code}' "http://$1/jsonrpc" | grep -qv '^000$'
}

declare -A IP DMAC SMAC NEIGH OK
table() {
    printf '%-22s %-15s %-18s %-18s %-30s %s\n' ROUTER IP DOCKER_MAC MGMT0_MAC "SER10_ARP" JSONRPC
    for r in $routers; do
        printf '%-22s %-15s %-18s %-18s %-30s %s\n' "${r#$LAB}" "${IP[$r]}" "${DMAC[$r]}" "${SMAC[$r]}" "${NEIGH[$r]}" "${OK[$r]}"
    done
}
probe() {
    for r in $routers; do
        NEIGH[$r]=$(ip neigh show to "${IP[$r]}" | awk '{for(i=1;i<=NF;i++) if($i=="lladdr"){m=$(i+1)}} END{print (m?m:"-") " " $NF}')
        reachable "${IP[$r]}" && OK[$r]=yes || OK[$r]=NO
    done
}

echo "===== ROUTERS BEFORE ====="
for r in $routers; do
    IP[$r]=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$r" | awk '{print $1}')
    DMAC[$r]=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.MacAddress}} {{end}}' "$r" | awk '{print tolower($1)}')
    SMAC[$r]=$(docker exec "$r" sr_cli "info from state interface mgmt0 ethernet hw-mac-address" 2>/dev/null \
        | grep -oiE '([0-9a-f]{2}:){5}[0-9a-f]{2}' | head -1 | tr 'A-F' 'a-f')
    [ -z "${SMAC[$r]}" ] && SMAC[$r]="?"
done
probe
table
BRIDGE=$(ip -o route get "${IP[$(echo "$routers" | head -1)]}" | sed -n 's/.* dev \([^ ]*\).*/\1/p')
echo
echo "management bridge: $BRIDGE"
echo "static ARP entries on it: $(ip neigh show dev "$BRIDGE" nud permanent | wc -l)"
DUPS=$(for r in $routers; do echo "${SMAC[$r]}"; done | grep -v '?' | sort | uniq -d)
echo "mgmt0 MACs shared by more than one router: ${DUPS:-none}"
MISMATCH=0
for r in $routers; do
    [ "${SMAC[$r]}" != "?" ] && [ "${SMAC[$r]}" != "${DMAC[$r]}" ] && MISMATCH=$((MISMATCH + 1))
done
echo "routers whose mgmt0 MAC differs from Docker's: $MISMATCH"

echo
echo "===== FIX: flush ARP on $BRIDGE and re-resolve ====="
ip neigh flush dev "$BRIDGE" nud all
for r in $routers; do ping -c1 -W1 "${IP[$r]}" >/dev/null 2>&1; done
sleep 2

echo
echo "===== ROUTERS AFTER ====="
probe
table

echo
echo "===== VERDICT ====="
FAILED=$(for r in $routers; do [ "${OK[$r]}" = NO ] && echo "${r#$LAB}"; done)
if [ -z "$FAILED" ]; then
    echo "All routers answer now. Run: ./5_run_config_mgmt.sh"
else
    echo "Still not answering: $(echo $FAILED)"
    if [ -n "$DUPS" ]; then
        echo "CAUSE: routers share a mgmt0 MAC ($DUPS), so the bridge sends their traffic"
        echo "to only one of them."
    else
        for r in $routers; do
            [ "${OK[$r]}" = NO ] || continue
            case "${NEIGH[$r]}" in
                *FAILED*|*INCOMPLETE*|"- "*) echo "  ${r#$LAB}: ser10 gets no ARP reply from ${IP[$r]} (mgmt0 ${SMAC[$r]}, Docker ${DMAC[$r]})";;
                *) echo "  ${r#$LAB}: ARP resolves (${NEIGH[$r]}) but JSON-RPC does not answer";;
            esac
        done
    fi
    echo "Paste mgmt_fix.txt back."
fi
echo
echo "Saved to $OUT"
