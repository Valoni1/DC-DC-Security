#!/bin/bash
# The NetBox API token is read from NETBOX_TOKEN, never from nrx.conf.
if [ -z "${NETBOX_TOKEN}" ]; then
    echo "NETBOX_TOKEN environment variable is not set." >&2
    exit 1
fi
export NB_API_TOKEN="${NETBOX_TOKEN}"
source ./venv/bin/activate
python3 ./nrx/nrx -c ./nrx.conf -o clab -D ./
