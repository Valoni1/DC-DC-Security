#!/bin/bash
# Brings NetBox up with Docker Compose and waits until its UI answers.
cd "$(dirname "$0")/netbox" || exit 1

if ! docker info >/dev/null 2>&1; then
    echo "Can't talk to Docker. Is it running, and is your user in the docker group?" >&2
    echo "Try: sudo systemctl start docker   or run this script with sudo." >&2
    exit 1
fi

echo "Deploying NetBox, the first run pulls images and loads the seed data..."
if ! docker compose up -d; then
    echo "docker compose up failed, see the errors above." >&2
    exit 1
fi

echo "Waiting for NetBox to answer on http://localhost:8000 (can take a few minutes)..."
for _ in $(seq 1 60); do
    if curl -sf -o /dev/null http://localhost:8000/login/; then
        echo "NetBox is up: http://localhost:8000 (admin / admin)"
        docker compose ps
        exit 0
    fi
    sleep 10
done

echo "NetBox didn't come up in 10 minutes. Check: cd netbox && docker compose ps && docker compose logs netbox" >&2
exit 1
