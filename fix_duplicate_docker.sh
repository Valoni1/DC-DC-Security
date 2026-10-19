#!/usr/bin/env bash
# fix_duplicate_docker.sh
#
# ser10 runs two Docker daemons at once:
#   - docker-ce 27.5.1 from apt   (/usr/bin/dockerd, systemd docker.service, since 4 Oct)
#   - docker 29.8.0 from the snap  (started 12 Oct)
# The snap daemon took over /var/run/docker.sock on 12 Oct, so every docker and
# clab command since then talks to it, and the current lab and NetBox live in it.
# The apt daemon kept running in the background with the 6-12 Oct lab and its
# own "clab" bridge on 172.20.20.0/24. That bridge stole the route to the
# management subnet (No route to host), and its old routers were the "ghosts".
#
# This keeps the snap daemon, which holds the working lab and NetBox, and
# switches the apt one off for good:
#   1. stops docker.service (apt) and disables and masks it, so it stays off
#      after reboots and apt upgrades. docker.socket is disabled and masked but
#      not stopped now, because stopping it would delete /var/run/docker.sock,
#      which the snap daemon is serving.
#   2. deletes every leftover bridge that holds the lab's gateway address but
#      is not the snap daemon's clab bridge
#   3. checks that docker still answers from the snap daemon and every router
#      still answers on its management address
#
# Undo: sudo systemctl unmask docker.service docker.socket
#       sudo systemctl enable --now docker.service
#
# USAGE (on the lab host, from the repo folder; the lab can stay up):
#   sudo bash fix_duplicate_docker.sh
# Output is also saved to duplicate_docker_fix.txt.

LAB="clab-DC1-DC2-"
OUT="$(dirname "$(readlink -f "$0")")/duplicate_docker_fix.txt"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash $0"
    exit 1
fi
exec > >(tee "$OUT") 2>&1

section() { printf '\n===== %s =====\n' "$1"; }
routers() { docker ps --filter "name=^${LAB}" --format '{{.Names}} {{.Image}}' | awk '/srlinux/ {print $1}' | sort; }
ip_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$1" | awk '{print $1}'; }
answers() { curl -s -o /dev/null -m 3 -w '%{http_code}' "http://$1/jsonrpc" | grep -qv '^000$'; }
daemons() { ps -eo pid,lstart,args | grep -E '[d]ockerd' | cut -c1-120; }

section "BEFORE"
daemons
echo "docker.sock served by: $(docker version --format '{{.Server.Version}}' 2>&1)"
SERVER=$(docker version --format '{{.Server.Version}}' 2>/dev/null)
if ! snap list docker >/dev/null 2>&1 || [ "$SERVER" != "$(snap list docker | awk 'NR==2 {print $2}')" ]; then
    echo "The socket is not served by the snap Docker, so this script's assumption is wrong."
    echo "Nothing changed. Paste this output back."
    exit 1
fi

section "1. SWITCH OFF THE APT DOCKER"
systemctl disable docker.service docker.socket 2>&1
systemctl mask docker.service docker.socket 2>&1
systemctl stop docker.service 2>&1
sleep 2
APT_PID=$(pgrep -f '^/usr/bin/dockerd' || true)
if [ -n "$APT_PID" ]; then
    echo "apt dockerd still running (pid $APT_PID); stopping it"
    kill "$APT_PID"; sleep 3
    pgrep -f '^/usr/bin/dockerd' >/dev/null && kill -9 $(pgrep -f '^/usr/bin/dockerd')
fi
echo "apt docker.service: $(systemctl is-enabled docker.service 2>&1) / $(systemctl is-active docker.service 2>&1)"

section "2. LEFTOVER BRIDGES"
R=$(routers | head -1)
NET=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$R" | awk '{print $1}')
NETID=$(docker network inspect -f '{{.Id}}' "$NET")
LIVE_BR="br-${NETID:0:12}"
GW="$(ip_of "$R" | cut -d. -f1-3).1/"
echo "lab bridge (snap daemon): $LIVE_BR"
for b in $(ip -o -4 addr show | awk -v a="$GW" 'index($4, a) == 1 {print $2}' | sort -u); do
    [ "$b" = "$LIVE_BR" ] && continue
    if [ -n "$(ip link show master "$b" 2>/dev/null)" ]; then
        echo "$b still has ports attached; leaving it (paste this back)"
        continue
    fi
    echo "deleting leftover bridge $b"
    ip link delete "$b"
done
ip -br link show type bridge

section "3. CHECK"
daemons
echo "docker.sock served by: $(docker version --format '{{.Server.Version}}' 2>&1)"
echo "containers: $(docker ps -q | wc -l)"
ok=0; bad=""
for r in $(routers); do
    if answers "$(ip_of "$r")"; then ok=$((ok + 1)); else bad="$bad ${r#$LAB}"; fi
done
echo "routers answering JSON-RPC: $ok of $(routers | wc -l)"

section "VERDICT"
if [ -z "$bad" ] && [ "$(docker version --format '{{.Server.Version}}' 2>/dev/null)" = "$SERVER" ] \
        && ! pgrep -f '^/usr/bin/dockerd' >/dev/null; then
    echo "FIXED: only the snap Docker runs now, and it stays that way after a reboot."
    echo "Every router still answers."
else
    [ -n "$bad" ] && echo "not answering:$bad"
    echo "Something is off; paste this output back."
fi
echo
echo "Saved to $OUT"
