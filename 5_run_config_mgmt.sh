#!/bin/bash
# NetBox credentials come from the environment, never from this file.
#   export NETBOX_TOKEN='<your NetBox API token>'
#   export NETBOX_URL='http://localhost:8000'   # optional, this is the default
if [ -z "${NETBOX_TOKEN}" ]; then
    echo "NETBOX_TOKEN environment variable is not set." >&2
    exit 1
fi
export NB_HOST="${NETBOX_URL:-http://localhost:8000}"
export NB_TOKEN="${NETBOX_TOKEN}"
source ./venv/bin/activate

cd config_mgmt
python3 ./configure.py -p clab-DC1-DC2- -s DC1 -s DC2 --commit true
