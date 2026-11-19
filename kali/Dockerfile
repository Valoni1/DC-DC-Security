# lab-kali: the manual-attacker image for the external/untrusted segment.
#
# The attacker node in DC1-DC2.clab.yaml is the plain, non-AI vantage box used
# to simulate manual attacks (as opposed to the ScopeWeaver AI stack on the same
# segment). It is built on Kali rolling with a small, curated toolset baked in,
# so redeploys are fast and need no network on the node itself.
#
# deploy_lab.sh builds this as lab-kali:latest before deploying if it is missing.
# To rebuild by hand: docker build -t lab-kali:latest kali/
FROM kalilinux/kali-rolling:latest

# A modest toolset for recon and pivoting from the exposed host inward. The full
# Kali metapackage (kali-linux-headless) is deliberately avoided to keep the
# image small on the lab host; add more with `apt-get install` inside the node,
# or extend this list and rebuild.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        iproute2 iputils-ping net-tools \
        nmap netcat-traditional curl wget dnsutils tcpdump \
        hydra sshpass openssh-client \
        python3 python3-pip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Keep the container alive so containerlab can exec into it; the lab side
# (eth1 on the clab-ext segment) is addressed by the node's exec in the topology.
CMD ["sleep", "infinity"]
