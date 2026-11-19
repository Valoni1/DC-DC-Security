# ScopeWeaver — external AI attacker

[ScopeWeaver](https://github.com/cskwork/scopeweaver) is an LLM multi-agent
penetration-testing tool (planner and worker agents that drive real recon and
exploitation tools). In this lab it plays the outside attacker: it reaches one
internet-facing host and pivots inward, for research candidates A (ransomware
across replication) and C (Zero Trust between DCs). Candidate D (induced false
failover) stays a separate on-path injector on the DCI and does not use this.

## Where it sits
It runs as its own docker compose stack (app + PostgreSQL) on the lab's
external segment, a Linux bridge `clab-ext` (`198.51.100.0/29`):

```
  scopeweaver .3 ─┐
  vantage .1    ──┤  clab-ext (untrusted)  ── dc1-h1 .2 (exposed) ─ eth1 ─ DC1 fabric ─ DCI ─ DC2
```

The ScopeWeaver container reaches only the exposed host `dc1-h1` (`198.51.100.2`)
on this segment; it has no route to the lab's management network. The SR Linux
routers are untouched.

## Model
A hosted frontier model is needed — this agent plans and drives tools over many
steps, and a local model good enough would need a GPU ser10 does not have. This
lab uses **DeepSeek** via its OpenAI-compatible API: ScopeWeaver speaks to any
"OpenAI Chat Completions" endpoint, so the provider is set to `openai`, the base
URL points at `https://api.deepseek.com`, and the DeepSeek key goes in
`OPENAI_API_KEY`. Default `ARTEX_LLM_MODEL` is `deepseek-chat`; use
`deepseek-reasoner` for the stronger (slower) reasoning model. Set
`OPENAI_API_KEY` in `src/.env`. (To switch to another provider later, set
`ARTEX_LLM_PROVIDER`/`ARTEX_LLM_BASE_URL`/`ARTEX_LLM_MODEL` and the matching key,
or configure it in the UI under LLM → New.)

## Run (on ser10, after `./deploy_lab.sh`)
```
./scopeweaver/setup_scopeweaver.sh      # creates clab-ext, clones, builds, starts
# then paste your key into scopeweaver/src/.env if setup warned it was empty, and:
#   (cd scopeweaver/src && docker compose up -d)
```
Reach the UI by SSH tunnel (do not publish the port to the internet):
```
ssh -L 8787:localhost:8787 <you>@ser10     # then http://localhost:8787
```

## Files
- `docker-compose.override.yml` — pins the LLM to hosted Claude and attaches the
  app to `clab-ext` at `.3`. Merged on top of upstream's compose.
- `env.lab.example` — the lab's env; copied to `src/.env` on first run.
- `setup_scopeweaver.sh` — the whole setup in one run.
- `src/` — the upstream ScopeWeaver checkout (AGPL-3.0; git-ignored, not vendored here).

## Safety and licence
ScopeWeaver is a real offensive tool and is AGPL-3.0; its own terms limit it to
local, authorised testing. Keep it on the isolated lab only — never point it at
any online system. The `src/` checkout is git-ignored so its AGPL code is not
committed into this dissertation repo.
