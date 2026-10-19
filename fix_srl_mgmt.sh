#!/usr/bin/env bash
# fix_srl_mgmt.sh
#
# The SR Linux routers of the DC1-DC2 lab boot and show mgmt0 up with an
# address, but ser10 gets "No route to host" (no ARP reply) from every one of
# them, so configure.py cannot reach any router.
#
# This checks every remaining cause in one run, fixes what it can, and retests:
#   1. AppArmor: denials logged since the lab started, from any host profile
#      other than docker-default. Host profiles attach by path to binaries
#      inside privileged containers (this already broke sudo through the
#      unix-chkpwd profile). Each profile named is put in complain mode.
#   2. ser10 side: the router's veth is on the management bridge and up,
#      bridge netfilter, nftables/ebtables rules that drop ARP or the subnet.
#   3. Router side: interfaces and addresses in the container and in its
#      srbase-mgmt namespace, the XDP program that hands eth0 traffic to
#      mgmt0, ARP sysctls, ping from mgmt0 to the bridge gateway, and the
#      sr_xdp_lc / mgmt logs.
#   4. A packet capture on both ends of the link while ser10 ARPs for the
#      router, which shows exactly where the ARP reply is lost.
# If step 1 changed a profile, the routers booted under the old rules, so the
# lab is redeployed (without the Windows hosts) and every router is retested.
#
# USAGE (on the lab host, from the repo folder, with the lab deployed):
#   sudo bash fix_srl_mgmt.sh            # diagnose, fix, redeploy if needed, retest
#   sudo bash fix_srl_mgmt.sh --no-redeploy
# Output is also saved to srl_mgmt_fix.txt.

LAB="clab-DC1-DC2-"
TOPO="DC1-DC2.clab.yaml"
HERE="$(dirname "$(readlink -f "$0")")"
OUT="$HERE/srl_mgmt_fix.txt"
REDEPLOY=1
[ "$1" = "--no-redeploy" ] && REDEPLOY=0

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash $0 $*"
    exit 1
fi
cd "$HERE" || exit 1
exec > >(tee "$OUT") 2>&1

section() { printf '\n===== %s =====\n' "$1"; }
routers() { docker ps --filter "name=^${LAB}" --format '{{.Names}} {{.Image}}' | awk '/srlinux/ {print $1}' | sort; }
ip_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$1" | awk '{print $1}'; }
answers() { curl -s -o /dev/null -m 3 -w '%{http_code}' "http://$1/jsonrpc" | grep -qv '^000$'; }
in_r() { docker exec "$R" bash -c "$1" 2>&1; }

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
STARTED=$(docker inspect -f '{{.State.StartedAt}}' "$R" | cut -c1-19 | tr T ' ')

section "BEFORE"
date
uname -r
echo "test router: ${R#$LAB} $RIP (started $STARTED UTC)"
retest

section "1. APPARMOR"
aa-status 2>/dev/null | sed -n '1,3p;/complain/,$p' | head -30
DENIALS=$(journalctl -k --utc --since "$STARTED" 2>/dev/null | grep -i 'apparmor="DENIED"')
[ -z "$DENIALS" ] && DENIALS=$(dmesg | grep -i 'apparmor="DENIED"' | tail -500)
echo "denials since the lab started: $(printf '%s' "$DENIALS" | grep -c .)"
echo "count  profile  process:"
printf '%s\n' "$DENIALS" | grep . | awk '{p=c="?"; for(i=1;i<=NF;i++){if($i~/^profile=/)p=$i; if($i~/^comm=/)c=$i} print p, c}' \
    | sort | uniq -c | sort -rn | head -20
printf '%s\n' "$DENIALS" | tail -5
PROFILES=$(printf '%s\n' "$DENIALS" | grep -oE 'profile="[^"]+"' | cut -d'"' -f2 \
    | sort -u | grep -vE '^(docker-default|unconfined)$')
CHANGED=""
for p in $PROFILES; do
    # profile "name" ... or a profile named by its path: /usr/sbin/x { ...
    f=$(grep -lE "^[[:space:]]*profile[[:space:]]+\"?${p//\//\\/}\"?([[:space:]]|\{|$)" /etc/apparmor.d/* 2>/dev/null | head -1)
    [ -z "$f" ] && f=$(grep -lE "^[[:space:]]*\"?${p//\//\\/}\"?[[:space:]]" /etc/apparmor.d/* 2>/dev/null | head -1)
    if [ -z "$f" ]; then
        echo "profile $p: no file found in /etc/apparmor.d, cannot change it"
        continue
    fi
    echo "FIX: profile $p ($f) -> complain mode"
    aa-complain "$f" && CHANGED="$CHANGED $f"
done
[ -z "$PROFILES" ] && echo "no host profile denied anything since the lab started"

section "1b. MEMORY (sr_xdp_lc OOM-killed = mgmt0 dead while the container stays Up)"
free -h | head -2
OOM=$(journalctl -k --utc --since "$STARTED" 2>/dev/null | grep -iE 'oom-kill|Killed process' | grep -ciE 'sr_|srlinux')
echo "SR Linux processes OOM-killed since the lab started: $OOM"
journalctl -k --utc --since "$STARTED" 2>/dev/null | grep -iE 'Killed process' | tail -5
echo "sr_xdp_lc running: $(pgrep -c sr_xdp_lc) (one per router: $(routers | wc -l))"

section "2. SER10 SIDE"
IFL=$(in_r 'cat /sys/class/net/eth0/iflink')
VETH=$(ip -o link | awk -F': ' -v i="$IFL" '$1 == i {print $2}' | cut -d@ -f1)
BR=$(ip -o route get "$RIP" | sed -n 's/.* dev \([^ ]*\).*/\1/p')
echo "route to $RIP: $(ip -o route get "$RIP" | head -1)"
echo "router eth0 peer on ser10: ${VETH:-not found}"
ip -d link show "$VETH" 2>&1 | head -3
ip -br link show "$BR" 2>&1
if [ -n "$VETH" ]; then
    master=$(ip -o link show "$VETH" | grep -o 'master [^ ]*' | cut -d' ' -f2)
    if [ "$master" != "$BR" ]; then
        echo "FIX: $VETH is on '${master:-nothing}', not $BR; attaching it"
        ip link set "$VETH" master "$BR"
    fi
    ip -o link show "$VETH" | grep -q 'state DOWN' && { echo "FIX: $VETH is down; bringing it up"; ip link set "$VETH" up; }
fi
echo "bridge-nf-call-iptables=$(cat /proc/sys/net/bridge/bridge-nf-call-iptables 2>/dev/null || echo n/a) arptables=$(cat /proc/sys/net/bridge/bridge-nf-call-arptables 2>/dev/null || echo n/a)"
echo "-- nftables rules that drop on the bridge or ARP or 172.20.20:"
nft list ruleset 2>/dev/null | grep -nE 'table|drop|reject' | grep -iE 'table|arp|bridge|172\.20\.20|'"$BR" | head -30
echo "-- ebtables:"
ebtables -L 2>/dev/null | grep -vE '^$|^Bridge chain.*entries: 0' | head -15
echo "-- iptables raw PREROUTING drops (Docker 28+ direct-access filter):"
iptables -t raw -S PREROUTING 2>/dev/null | grep -E 'DROP' | grep -E '172\.20\.20|'"$BR" | head -5

section "3. ROUTER SIDE (${R#$LAB})"
in_r 'echo "-- container netns:"; ip -br link; ip -br addr
      echo "-- XDP on eth0:"; ip -d link show eth0 | grep -iE "xdp|prog" || echo "no XDP program on eth0"
      echo "-- namespaces:"; ip netns list
      echo "-- srbase-mgmt:"; ip netns exec srbase-mgmt ip -br link; ip netns exec srbase-mgmt ip -br addr; ip netns exec srbase-mgmt ip route
      for i in $(ip netns exec srbase-mgmt ls /sys/class/net); do
          printf "%s arp_ignore=%s arp_filter=%s\n" $i \
            $(ip netns exec srbase-mgmt cat /proc/sys/net/ipv4/conf/$i/arp_ignore /proc/sys/net/ipv4/conf/$i/arp_filter 2>/dev/null)
      done
      echo "-- ping bridge gateway from srbase-mgmt:"
      ip netns exec srbase-mgmt ping -c2 -W1 '"${RIP%.*}.1"' | tail -2
      echo "-- sr_xdp_lc / mgmt errors in logs:"
      grep -hiE "error|fail|denied|xdp" /var/log/srlinux/debug/sr_xdp_lc* /var/log/srlinux/stdout/sr_xdp_lc* /var/log/srlinux/debug/sr_mgmt* 2>/dev/null | tail -15'
echo "-- SR Linux apps not running:"
docker exec "$R" sr_cli "show system application" 2>/dev/null | grep -iE 'error|fail|starting|waiting|stopped' | head -15

section "4. WHERE THE ARP REPLY IS LOST"
ip neigh flush dev "$BR" to "$RIP" 2>/dev/null
CAP=$(mktemp -d)
# wait on these PIDs only: a bare "wait" also waits for the tee behind the
# script's own output, which never exits
timeout 8 tcpdump -lnei "$VETH" arp > "$CAP/veth" 2>/dev/null & P1=$!
timeout 8 docker exec "$R" timeout 6 tcpdump -lnei eth0 arp > "$CAP/eth0" 2>/dev/null & P2=$!
timeout 8 docker exec "$R" timeout 6 ip netns exec srbase-mgmt tcpdump -lnei any arp > "$CAP/mgmt" 2>/dev/null & P3=$!
sleep 1
ping -c3 -W1 "$RIP" >/dev/null 2>&1
wait $P1 $P2 $P3
for w in veth eth0 mgmt; do
    req=$(grep -c "Request who-has $RIP" "$CAP/$w"); rep=$(grep -c "Reply $RIP is-at" "$CAP/$w")
    printf '%-38s requests %s, replies %s\n' "$(case $w in veth) echo "ser10 side ($VETH)";; eth0) echo "router eth0";; mgmt) echo "router srbase-mgmt";; esac)" "$req" "$rep"
done
LOST=""
if ! docker exec "$R" which tcpdump >/dev/null 2>&1; then
    [ "$(grep -c "is-at" "$CAP/veth")" = 0 ] && LOST="ser10 sends the ARP but gets no reply (tcpdump missing in the router, so no capture inside)"
elif [ "$(grep -c "who-has $RIP" "$CAP/veth")" = 0 ]; then LOST="ser10 never sends the ARP onto the router's veth (bridge/host side)"
elif [ "$(grep -c "who-has $RIP" "$CAP/eth0")" = 0 ]; then LOST="the ARP leaves ser10 but never arrives on the router's eth0 (veth link)"
elif [ "$(grep -c "who-has $RIP" "$CAP/mgmt")" = 0 ]; then LOST="the ARP reaches eth0 but is not handed to mgmt0: SR Linux's XDP forwarder (sr_xdp_lc) is not passing management traffic"
elif [ "$(grep -c "is-at" "$CAP/mgmt")" = 0 ]; then LOST="mgmt0 receives the ARP but does not answer (address/ARP settings in srbase-mgmt)"
elif [ "$(grep -c "is-at" "$CAP/veth")" = 0 ]; then LOST="mgmt0 answers but the reply does not get back out to ser10 (XDP return path)"
fi
echo "${LOST:-the ARP reply makes it back to ser10}"
rm -rf "$CAP"

if [ -n "$CHANGED" ] && [ "$REDEPLOY" = 1 ]; then
    section "REDEPLOY (routers booted under the old AppArmor rules)"
    NODES=$(grep -oE '^        [a-z0-9-]+:$' "$TOPO" | tr -d ' :' | grep -v -- '-w1$' | paste -sd, -)
    clab des -t "$TOPO" --cleanup
    rm -rf "clab-DC1-DC2"
    clab dep -t "$TOPO" --node-filter "$NODES" 2>&1 | grep -viE '^\s*$' | tail -40
    echo "waiting up to 3 minutes for the routers' JSON-RPC..."
    for i in $(seq 18); do retest >/dev/null && break; sleep 10; done
fi

section "AFTER"
if retest; then
    section "VERDICT"
    echo "FIXED: every router answers on its management address."
    [ -n "$CHANGED" ] && echo "Changed to complain mode:$CHANGED (undo later with: sudo aa-enforce <file>)"
    echo "Next: ./5_run_config_mgmt.sh 2>&1 | grep -iE 'deployed|skipping|error|failed'"
    echo "      python3 configure_hosts_dc1_dc2.py && python3 check_hosts_dc1_dc2.py"
else
    section "VERDICT"
    [ -n "$CHANGED" ] && echo "AppArmor profiles changed:$CHANGED, routers still do not answer."
    echo "WHERE IT BREAKS: ${LOST:-unknown}"
    [ "${OOM:-0}" -gt 0 ] && echo "ALSO: $OOM SR Linux processes were OOM-killed; ser10 is out of memory for this lab."
    echo "Paste srl_mgmt_fix.txt back."
fi
echo
echo "Saved to $OUT"
