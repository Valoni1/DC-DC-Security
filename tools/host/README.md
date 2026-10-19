# Lab host repair scripts

One-off fixes for problems on the lab host (ser10) itself, found while bringing
up the DC1-DC2 lab in October 2026. They are not part of building or running the
lab. Run each from the repo folder with `sudo bash tools/host/<script>`; each
saves its output next to itself as a git-ignored .txt file.

| Script | Fixes |
|---|---|
| diagnose_srl_boot.sh | SR Linux exits at boot with "sudo: account validation failure" (host AppArmor unix-chkpwd profile; `--fix` sets it to complain mode) |
| fix_duplicate_docker.sh | Two Docker daemons (apt and snap) running at once; switches the apt one off for good |
| fix_mgmt_bridge.sh | Host routes the lab management subnet to a dead bridge ("No route to host" to every router) |
| clean_ghost_routers.sh | SR Linux processes left over from earlier deploys answering for lab IPs |
| fix_srl_mgmt.sh | Full check of why routers don't answer on mgmt0 (AppArmor, memory, bridge, router namespace, ARP capture) |
| fix_mgmt_reachability.sh | Per-router MAC / ARP / JSON-RPC comparison and ARP flush |

Root cause of the October problems: a snap Docker installed on 12 Oct took over
the Docker socket while the apt docker-ce kept running with the old lab.
