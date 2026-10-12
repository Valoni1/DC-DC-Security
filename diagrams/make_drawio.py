"""Draw the DC1/DC2 lab as a draw.io diagram, using only what the repo defines.

Sources:
  DC1-DC2.clab.yaml        nodes, roles, memory, cabling (containerlab names e1-N)
  ip_plan_dc1_dc2.yaml     loopbacks and link /31s (NetBox names ethernet-1/N)
  config_mgmt/configure.py ASN_BASE (AS per role and DC)

The script checks that the containerlab cabling and the IP plan describe the
same links before drawing anything, so the picture cannot drift from the lab.

    python3 diagrams/make_drawio.py   # writes diagrams/DC1-DC2-topology.drawio
"""
import ast
import html
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "diagrams", "DC1-DC2-topology.drawio")

ROLE_SLUG = {
    "Leaf": "leaf",
    "Spine": "spine",
    "Superspine": "superspine",
    "DC Gateway": "dc-gateway",
}
STYLE = {
    "dc-gateway": "fillColor=#e1d5e7;strokeColor=#9673a6;",
    "superspine": "fillColor=#f8cecc;strokeColor=#b85450;",
    "spine": "fillColor=#ffe6cc;strokeColor=#d79b00;",
    "leaf": "fillColor=#d5e8d4;strokeColor=#82b366;",
}


def load():
    with open(os.path.join(ROOT, "DC1-DC2.clab.yaml")) as f:
        clab = yaml.safe_load(f)
    with open(os.path.join(ROOT, "ip_plan_dc1_dc2.yaml")) as f:
        plan = yaml.safe_load(f)
    # Read ASN_BASE straight from configure.py rather than copying it.
    with open(os.path.join(ROOT, "config_mgmt", "configure.py")) as f:
        src = f.read()
    asn_base = ast.literal_eval(re.search(r"ASN_BASE = (\{.*?\})", src, re.S).group(1))
    return clab, plan, asn_base


def nb_if(clab_if):
    """e1-27 -> ethernet-1/27"""
    m = re.fullmatch(r"e(\d+)-(\d+)", clab_if)
    return f"ethernet-{m.group(1)}/{m.group(2)}"


def check(clab, plan):
    nodes = {n: v for n, v in clab["topology"]["nodes"].items() if v["kind"] == "srl"}
    clab_links = set()
    for link in clab["topology"]["links"]:
        (a, ai), (b, bi) = (e.split(":") for e in link["endpoints"])
        clab_links.add(frozenset([(a, nb_if(ai)), (b, nb_if(bi))]))
    plan_links = {frozenset([(l["a"], l["a_if"]), (l["b"], l["b_if"])]) for l in plan["links"]}
    assert clab_links == plan_links, clab_links ^ plan_links
    assert set(nodes) == set(plan["loopbacks"]), set(nodes) ^ set(plan["loopbacks"])
    return nodes


class Drawing:
    def __init__(self):
        self.cells = []
        self.n = 0

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}{self.n}"

    def box(self, value, x, y, w, h, style, cid=None, parent="1"):
        cid = cid or self._id("v")
        self.cells.append(
            f'<mxCell id="{cid}" value="{html.escape(value)}" style="{style}" '
            f'vertex="1" parent="{parent}"><mxGeometry x="{x}" y="{y}" '
            f'width="{w}" height="{h}" as="geometry"/></mxCell>'
        )
        return cid

    def edge(self, src, dst, style, value="", labels=()):
        cid = self._id("e")
        self.cells.append(
            f'<mxCell id="{cid}" value="{html.escape(value)}" style="{style}" edge="1" '
            f'parent="1" source="{src}" target="{dst}"><mxGeometry relative="1" as="geometry"/></mxCell>'
        )
        # labels: (text, position along edge -1..1, offset y)
        for text, pos, dy in labels:
            self.cells.append(
                f'<mxCell id="{self._id("l")}" value="{html.escape(text)}" '
                f'style="edgeLabel;html=1;align=center;verticalAlign=middle;fontSize=10;" '
                f'vertex="1" connectable="0" parent="{cid}"><mxGeometry x="{pos}" '
                f'relative="1" as="geometry"><mxPoint y="{dy}" as="offset"/></mxGeometry></mxCell>'
            )
        return cid

    def xml(self):
        body = "".join(self.cells)
        return (
            '<mxfile host="app.diagrams.net"><diagram id="dc1dc2" name="DC1-DC2 network">'
            '<mxGraphModel dx="2900" dy="1600" grid="1" gridSize="10" guides="1" page="0" '
            'math="0" shadow="0"><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            f"{body}</root></mxGraphModel></diagram></mxfile>\n"
        )


def table(rows, header):
    th = "".join(f"<th>{h}</th>" for h in header)
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (
        '<table border="1" cellpadding="3" style="border-collapse:collapse;font-size:11px;'
        f'font-family:monospace;width:100%"><tr style="background:#eeeeee">{th}</tr>{trs}</table>'
    )


def main():
    clab, plan, asn_base = load()
    nodes = check(clab, plan)
    d = Drawing()

    def dc_of(name):
        return int(re.match(r"dc(\d+)-", name).group(1))

    def asn(name):
        return asn_base[ROLE_SLUG[nodes[name]["labels"]["role"]]] + dc_of(name)

    W, NW, NH = 1400, 160, 66
    dc_x = {1: 0, 2: W + 100}
    tiers = {  # y and horizontal centres (relative to the DC box) per tier
        "dc-gateway": (110, [W / 2]),
        "superspine": (270, [W / 2 - 220, W / 2 + 220]),
        "spine": (440, [W / 2 - 330, W / 2 + 330]),
        "leaf": (640, [W / 2 + (i - 2.5) * 225 for i in range(6)]),
    }
    TABLE_Y, TABLE_H = 760, 440
    BOX_H = TABLE_Y + TABLE_H + 20

    for dc in (1, 2):  # both site boxes first so they sit behind everything else
        d.box(
            f"<b>DC{dc}</b> &nbsp;(NetBox site DC{dc}, containerlab group DC{dc})<br>"
            f"loopbacks 172.20.{dc}.0/24 &nbsp;·&nbsp; fabric links /31s from 10.1.{dc}.0/24 "
            f"&nbsp;·&nbsp; OSPFv2 area 0.0.0.0 inside the DC",
            dc_x[dc], 0, W, BOX_H,
            "rounded=1;arcSize=2;whiteSpace=wrap;html=1;fillColor=#f9f9f9;strokeColor=#666666;"
            "dashed=1;verticalAlign=top;align=left;spacingLeft=10;spacingTop=6;fontSize=14;",
        )
    for dc in (1, 2):
        plan_links = [l for l in plan["links"] if dc_of(l["a"]) == dc and dc_of(l["b"]) == dc]
        for role, (y, xs) in tiers.items():
            members = sorted(
                n for n, v in nodes.items()
                if dc_of(n) == dc and ROLE_SLUG[v["labels"]["role"]] == role
            )
            assert len(members) == len(xs), (dc, role, members)
            for name, cx in zip(members, xs):
                v = nodes[name]
                d.box(
                    f"<b>{name}</b><br>{v['labels']['role']} · AS {asn(name)}<br>"
                    f"lo0 {plan['loopbacks'][name]}",
                    dc_x[dc] + cx - NW / 2, y, NW, NH,
                    "rounded=1;whiteSpace=wrap;html=1;fontSize=11;" + STYLE[role],
                    cid=name,
                )
        for l in plan_links:
            d.edge(l["a"], l["b"], "endArrow=none;html=1;strokeColor=#555555;strokeWidth=1.5;")
        rows = [
            [i + 1, l["a"], l["a_if"], l["a_ip"], l["b"], l["b_if"], l["b_ip"]]
            for i, l in enumerate(plan_links)
        ]
        d.box(
            f"<b>DC{dc} fabric links ({len(rows)})</b> &nbsp;— from ip_plan_dc1_dc2.yaml; "
            "containerlab name e1-N = ethernet-1/N<br>"
            + table(rows, ["#", "A end", "interface", "IPv4", "B end", "interface", "IPv4"]),
            dc_x[dc] + 20, TABLE_Y, W - 40, TABLE_H,
            "text;html=1;whiteSpace=wrap;align=left;verticalAlign=top;overflow=fill;fontSize=12;",
        )

    # DCI: the only link between the two sites
    dci = [l for l in plan["links"] if dc_of(l["a"]) != dc_of(l["b"])]
    assert len(dci) == 1, dci
    l = dci[0]
    d.edge(
        l["a"], l["b"],
        "endArrow=none;html=1;strokeColor=#1f5fbf;strokeWidth=4;fontSize=12;fontStyle=1;",
        value=f"DCI link · eBGP AS {asn(l['a'])} ↔ AS {asn(l['b'])}",
        labels=[
            (f"{l['a']} {l['a_if']}<br>{l['a_ip']}", -0.75, 22),
            (f"{l['b']} {l['b_if']}<br>{l['b_ip']}", 0.75, 22),
            ("no OSPF on this link", 0, 22),
        ],
    )

    # Routing, management and legend panels under the two DCs
    py = BOX_H + 40
    mem = {}
    for v in nodes.values():
        mem.setdefault(v["labels"]["role"], set()).add(v["memory"])
    mem_txt = ", ".join(f"{r} {'/'.join(sorted(m))}" for r, m in mem.items())
    ex = next(iter(nodes.values()))
    panel = ("rounded=1;arcSize=3;whiteSpace=wrap;html=1;align=left;verticalAlign=top;"
             "spacingLeft=10;spacingTop=6;spacingRight=10;fontSize=12;")

    d.box(
        "<b>Routing (as rendered by config_mgmt/configure.py templates)</b><ul>"
        "<li><b>OSPFv2</b>, area 0.0.0.0, point-to-point on every fabric link, lo0 passive. "
        "Separate per DC: the DCI interface is left out.</li>"
        "<li><b>iBGP</b> leaf ↔ spine inside AS 6500N, peering between lo0 addresses; "
        "spines are route reflectors (cluster-id 1). AFI/SAFI: evpn, ipv4-unicast.</li>"
        "<li><b>eBGP</b> spine (6500N) ↔ superspine (6510N) and superspine (6510N) ↔ "
        "DC gateway (6520N), on the link /31s.</li>"
        "<li><b>eBGP over DCI</b> dc1-dci-r1 (65201) ↔ dc2-dci-r1 (65202). "
        "Export policy dci-export sends only the local 172.20.N.0/24 loopbacks (mask 24..32); "
        "dci-import accepts all; bgp-to-ospf redistributes them into the DC's OSPF (gateway is ASBR).</li>"
        "</ul>Note: config_mgmt/configs/ holds no rendered DC1/DC2 configs yet, "
        "so this is the intended configuration, not one captured from a running lab.",
        0, py, 1400, 190, panel + "fillColor=#ffffff;strokeColor=#666666;",
    )
    d.box(
        "<b>Out-of-band management and tooling</b><ul>"
        "<li>No mgmt block in DC1-DC2.clab.yaml, so containerlab's default management "
        "network applies (bridge <i>clab</i>, 172.20.20.0/24). Every node's mgmt0 sits in "
        "SR Linux network-instance <i>mgmt</i>; container names are clab-DC1-DC2-&lt;node&gt;.</li>"
        "<li><b>graphite</b> (linux, netreplica/graphite) on the same network, host port 8080 → 80, "
        "topology viewer.</li>"
        "<li><b>NetBox</b> (docker compose, host port 8000 → 8080) is the source of truth. "
        "nrx exports sites DC1, DC2 to containerlab; configure.py pushes configs over "
        "JSON-RPC to http://clab-DC1-DC2-&lt;node&gt;/jsonrpc.</li></ul>",
        1500, py, 1400, 190, panel + "fillColor=#f5f5f5;strokeColor=#999999;dashed=1;",
    )
    legend_y = py + 220
    d.box(
        "<b>Legend</b>", 0, legend_y, 2900, 110,
        panel + "fillColor=#ffffff;strokeColor=#666666;",
    )
    lx = 20
    for role, label in [("dc-gateway", "DC Gateway (AS 6520N)"),
                        ("superspine", "Superspine (AS 6510N)"),
                        ("spine", "Spine (AS 6500N)"),
                        ("leaf", "Leaf (AS 6500N)")]:
        d.box(label, lx, legend_y + 40, 190, 40,
              "rounded=1;whiteSpace=wrap;html=1;fontSize=11;" + STYLE[role])
        lx += 210
    d.box(
        f"All {len(nodes)} routers: Nokia SR Linux (kind {ex['kind']}, type {ex['type'].strip()}, "
        f"image {ex['image']}), 7220 IXR-D3L. Memory: {mem_txt}.<br>"
        f"Grey line = intra-DC fabric link ({len(plan['links']) - 1}); "
        f"thick blue = DCI link (1). Total {len(plan['links'])} links. N = DC number.",
        lx + 20, legend_y + 30, 2900 - lx - 40, 60,
        "text;html=1;whiteSpace=wrap;align=left;verticalAlign=middle;fontSize=12;",
    )

    with open(OUT, "w") as f:
        f.write(d.xml())
    print(f"wrote {OUT}: {len(nodes)} nodes, {len(plan['links'])} links")


if __name__ == "__main__":
    main()
