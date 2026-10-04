#!/usr/bin/env python3
import os
import sys

import pynetbox
from pprint import pprint as pp

if not os.environ.get('NETBOX_TOKEN'):
    sys.exit('NETBOX_TOKEN environment variable is not set.')

nb = pynetbox.api(
    os.environ.get('NETBOX_URL', 'http://localhost:8000'),
    token=os.environ['NETBOX_TOKEN']
)


devices = list(nb.dcim.devices.all())

for d in devices:
    for tag in d.tags:
        if 'demo' in tag['name']:
            for t in d.tags:
                if 'final' in t['name']:
                    d.tags.remove(t)
            d.tags.append({"name": "isis=initial"})
            d.save()
