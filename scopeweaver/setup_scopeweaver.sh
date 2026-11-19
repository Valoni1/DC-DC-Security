#!/usr/bin/env bash
# setup_scopeweaver.sh
#
# Stands up ScopeWeaver as its own docker compose stack (app + PostgreSQL) on
# the lab's external segment, so it sits outside the two DCs at 198.51.100.3
# and reaches the exposed host dc1-h1 (198.51.100.2) over the untrusted bridge,
# then pivots inward from there.
#
# It:
#   1. makes sure the external bridge clab-ext exists (shared with the lab)
#   2. clones ScopeWeaver (AGPL; kept out of this repo via .gitignore)
#   3. drops our .env (first run) and compose override on top of upstream
#   4. builds and starts the stack, waits for the UI
#   5. checks it can reach the exposed host
#
# Before the agent can run, put your DeepSeek key (as OPENAI_API_KEY) in
# scopeweaver/src/.env.
# ScopeWeaver is a deliberately offensive tool: run it only against this
# isolated lab, never an online system.
#
# USAGE (on ser10, after ./deploy_lab.sh so dc1-h1 is on the segment):
#   ./scopeweaver/setup_scopeweaver.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"   # the scopeweaver/ directory
REPO="https://github.com/cskwork/scopeweaver"
SRC="src"
NET="clab-ext"
SUBNET="198.51.100.0/29"
GW="198.51.100.6"

echo "===== 1. external bridge ====="
if docker network inspect "$NET" >/dev/null 2>&1; then
    echo "docker network $NET already exists"
else
    docker network create --subnet "$SUBNET" --gateway "$GW" \
        -o com.docker.network.bridge.name="$NET" "$NET"
    echo "created docker network/bridge $NET ($SUBNET)"
fi

echo "===== 2. ScopeWeaver source ====="
if [ -d "$SRC/.git" ]; then
    echo "already cloned in $SRC (run 'git -C $SRC pull' to update)"
else
    git clone "$REPO" "$SRC"
fi

echo "===== 3. env and override ====="
if [ -f "$SRC/.env" ]; then
    echo "$SRC/.env exists, left as is"
else
    cp env.lab.example "$SRC/.env"
    echo "copied env.lab.example -> $SRC/.env"
fi
cp docker-compose.override.yml "$SRC/docker-compose.override.yml"
if ! grep -qE '^OPENAI_API_KEY=.+' "$SRC/.env"; then
    echo "WARNING: OPENAI_API_KEY (the DeepSeek key) is empty in $SRC/.env."
    echo "         Paste your key there, then re-run this script (or: cd $SRC && docker compose up -d)."
fi

echo "===== 4. build and start ====="
( cd "$SRC" && docker compose up -d --build )

echo "===== 5. wait for the UI ====="
up=no
for i in $(seq 1 60); do
    if curl -sf -o /dev/null http://localhost:8787/ 2>/dev/null; then
        echo "ScopeWeaver UI is up after $((i*5))s"; up=yes; break
    fi
    sleep 5
done
[ "$up" = yes ] || echo "UI did not answer on :8787 within 5 min; check 'cd $SRC && docker compose logs'."

echo "===== 6. reach the exposed host ====="
# The ScopeWeaver image may not ship ping; treat a failure here as "check later".
if ( cd "$SRC" && docker compose exec -T scopeweaver sh -c \
       'command -v ping >/dev/null && ping -c3 -W1 198.51.100.2' ) 2>/dev/null | grep -q "0% packet loss"; then
    echo "OK: ScopeWeaver reaches the exposed host dc1-h1 (198.51.100.2)"
else
    echo "NOTE: could not confirm the path to 198.51.100.2 from here."
    echo "      Make sure the lab is deployed (./deploy_lab.sh) so dc1-h1 is on clab-ext,"
    echo "      then test from the UI or: docker compose -f $SRC/docker-compose.yml -f $SRC/docker-compose.override.yml exec scopeweaver sh"
fi

echo
echo "Done. UI: http://localhost:8787"
echo "Reach it with an SSH tunnel from your machine (do not expose ser10 to the internet):"
echo "  ssh -L 8787:localhost:8787 <you>@ser10   then open http://localhost:8787"
