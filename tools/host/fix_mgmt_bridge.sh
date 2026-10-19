#!/usr/bin/env bash
# fix_mgmt_bridge.sh
#
# Fixes "No route to host" from ser10 to every SR Linux router of the DC1-DC2
# lab. fix_srl_mgmt.sh showed that the routers are fine (mgmt0.0 has its
# address, no AppArmor or memory problem) but ser10 sends traffic for
# 172.20.20.0/24 to a bridge that is DOWN with NO-CARRIER, i.e. no router is
# plugged into it. The routers' management cables are on another bridge, or
# not on any. The AppArmor log also shows Docker running from the snap
# (snap.docker.dockerd), so ser10 may have two Docker daemons, each with its
# own "clab" network on 172.20.20.0/24. That would also explain the "ghost"
# routers that answered ARP earlier while Docker did not list them.
#
# The script:
#   1. finds the bridge Docker created for the lab's management network
#   2. finds each router's management cable on ser10 and plugs it into that
#      bridge if it is elsewhere, and brings it up
#   3. takes down every other bridge that also holds 172.20.20.1, so the route
#      goes to the bridge the routers are on
#   4. lists the Docker daemons on ser10, for the permanent fix
#   5. retests every router's JSON-RPC
#
# USAGE (on the lab host, from the repo folder, with the lab deployed):
#   sudo bash tools/host/fix_mgmt_bridge.sh
# Output is also saved to mgmt_bridge_fix.txt.

LAB="clab-DC1-DC2-"
OUT="$(dirname "$(readlink -f "$0")")/mgmt_bridge_fix.txt"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash $0"
    exit 1
fi
exec > >(tee "$OUT") 2>&1

section() { printf '\n===== %s =====\n' "$1"; }
routers() { docker ps --filter "name=^${LAB}" --format '{{.Names}} {{.Image}}' | awk '/srlinux/ {print $1}' | sort; }
ip_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$1" | awk '{print $1}'; }
answers() { curl -s -o /dev/null -m 3 -w '%{http_code}' "http://$1/jsonrpc" | grep -qv '^000$'; }
retest() {
    local ok=0 bad=""
    for r in $(routers); do
        if answers "$(ip_of "$r")"; then ok=$((ok + 1)); else bad="$bad ${r#$LAB}"; fi
    done
    echo "routers answering JSON-RPC: $ok of $(routers | wc -l)"
    [ -n "$bad" ] && echo "not answering:$bad"
    [ -z "$bad" ]
}

R=$(routers | grep -- '-dc1-l1$' || routers | head -1)
[ -z "$R" ] && { echo "No SR Linux routers running. Deploy the lab first."; exit 1; }
RIP=$(ip_of "$R")
SUBNET="${RIP%.*}"

section "1. THE LAB'S MANAGEMENT NETWORK"
NET=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$R" | awk '{print $1}')
NETID=$(docker network inspect -f '{{.Id}}' "$NET")
LIVE_BR=$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' "$NET")
[ -z "$LIVE_BR" ] || [ "$LIVE_BR" = "<no value>" ] && LIVE_BR="br-${NETID:0:12}"
echo "docker network: $NET ($NETID)"
echo "its bridge: $LIVE_BR"
ip -br link show "$LIVE_BR" 2>&1
ip -br addr show "$LIVE_BR" 2>&1
echo "bridges holding ${SUBNET}.1 on ser10:"
ip -o -4 addr show | awk -v a="${SUBNET}.1/" 'index($4, a) == 1 {print "  " $2, $4}'
echo "route to $RIP: $(ip -o route get "$RIP" | head -1)"

section "2. ROUTER MANAGEMENT CABLES"
MISSING=0
for r in $(routers); do
    idx=$(docker exec "$r" cat /sys/class/net/mgmt0/iflink 2>/dev/null)
    peer=$(ip -o link | awk -F': ' -v i="$idx" '$1 == i {print $2}' | cut -d@ -f1)
    if [ -z "$peer" ]; then
        printf '%-12s mgmt0 peer ifindex %s: NOT in ser10'"'"'s network namespace\n' "${r#$LAB}" "${idx:-?}"
        MISSING=$((MISSING + 1))
        continue
    fi
    master=$(ip -o link show "$peer" | grep -o 'master [^ ]*' | cut -d' ' -f2)
    state=$(ip -br link show "$peer" | awk '{print $2}')
    printf '%-12s %-16s on %-18s %s' "${r#$LAB}" "$peer" "${master:-nothing}" "$state"
    if [ "$master" != "$LIVE_BR" ] && ip link show "$LIVE_BR" >/dev/null 2>&1; then
        ip link set "$peer" master "$LIVE_BR" && printf '  FIXED: moved to %s' "$LIVE_BR"
    fi
    [ "$state" != "UP" ] && ip link set "$peer" up && printf '  FIXED: brought up'
    echo
done

section "3. DUPLICATE BRIDGES ON ${SUBNET}.0/24"
if ip link show "$LIVE_BR" >/dev/null 2>&1; then
    ip link set "$LIVE_BR" up
    for b in $(ip -o -4 addr show | awk -v a="${SUBNET}.1/" 'index($4, a) == 1 {print $2}' | sort -u); do
        [ "$b" = "$LIVE_BR" ] && continue
        echo "FIXED: $b also holds ${SUBNET}.1 and has no lab routers; taking it down"
        ip link show master "$b" | awk -F': ' '/^[0-9]/ {print "  was attached: " $2}'
        ip link set "$b" down
    done
else
    echo "Docker's bridge $LIVE_BR does not exist in ser10's network namespace."
fi
ip neigh flush dev "$LIVE_BR" 2>/dev/null
echo "route to $RIP now: $(ip -o route get "$RIP" | head -1)"
ip -br link show "$LIVE_BR" 2>&1

section "4. DOCKER DAEMONS ON SER10"
ps -eo pid,lstart,args | grep -E '[d]ockerd' | cut -c1-200
snap list docker 2>/dev/null | tail -n +2 | sed 's/^/snap: /'
dpkg -l 2>/dev/null | awk '/^ii/ && $2 ~ /^(docker-ce|docker.io|containerd.io)$/ {print "apt: " $2, $3}'
docker context ls 2>/dev/null
echo "DOCKER_HOST=${DOCKER_HOST:-unset}"
for s in /var/run/docker.sock /run/docker.sock /var/snap/docker/current/run/docker.sock /run/snap.docker/docker.sock; do
    [ -S "$s" ] || continue
    echo "-- $s: $(docker -H "unix://$s" version --format 'server {{.Server.Version}}' 2>&1 | head -1)"
    docker -H "unix://$s" network ls --no-trunc --format '   network {{.Name}} {{.ID}}' 2>/dev/null | grep -E 'clab|bridge'
    echo "   containers: $(docker -H "unix://$s" ps -q 2>/dev/null | wc -l)"
done

section "5. RETEST"
sleep 2
for r in $(routers); do ping -c1 -W1 "$(ip_of "$r")" >/dev/null 2>&1; done
if retest; then
    section "VERDICT"
    echo "FIXED: every router answers. ser10 was sending management traffic to a bridge"
    echo "with no routers on it."
    echo "Next (as your normal user, NETBOX_TOKEN exported):"
    echo "  ./5_run_config_mgmt.sh 2>&1 | grep -iE 'deployed|skipping|error|failed'"
    echo "  python3 configure_hosts_dc1_dc2.py && python3 check_hosts_dc1_dc2.py"
    echo "If section 4 lists two Docker daemons, the duplicate bridge comes back when the"
    echo "other one restarts; paste section 4 and I'll give you the permanent fix."
else
    section "VERDICT"
    if [ "$MISSING" -gt 0 ]; then
        echo "The routers' management cables are not in ser10's network namespace: the lab"
        echo "runs under a Docker daemon that is isolated from the host (section 4 shows which)."
    else
        echo "Bridge and cables are now correct but routers still do not answer."
    fi
    echo "Paste mgmt_bridge_fix.txt back."
fi
echo
echo "Saved to $OUT"
