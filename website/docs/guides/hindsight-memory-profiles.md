---
sidebar_position: 20
title: "Hindsight Memory Across Profiles"
description: "Use Hindsight as the memory of every Hermes profile without stalling the agent: one shared server, a bank per profile, and the load-test numbers behind that layout"
---

# Hindsight Memory Across Profiles

Hindsight can be the memory of the principal profile only, or of every profile. Both work. The
layout decides whether many profiles stall each other, so this guide gives the layout, the
script that applies it, and the measurements behind it.

## The layout

- **One Hindsight server for all profiles.** Run it yourself (`local_external`, the default of the
  script below) or let the plugin start it (`local_embedded` with the shared hindsight-embed
  profile `hermes`). Never one server per Hermes profile: each one is a separate API process plus
  an embedded PostgreSQL, and a memory process per profile is exactly what exhausts RAM when
  dozens of profiles start at once.
- **A bank per profile** (`bank_id_template: hermes-{profile}`), so each profile keeps its own
  memory. `--shared-bank` puts every profile in one bank instead.
- **In `local_embedded`, identical LLM settings in every profile.** The plugin rewrites the shared
  daemon's env file and restarts the daemon whenever a profile starts with LLM settings that
  differ from it (`_profile_env_drifted` in the plugin). Each restart took ~20 s in the tests
  below, and every call from every profile failed during it. `local_external` avoids this
  entirely because the server owns its LLM settings.
- **`recall_sync: true`.** By default the plugin injects only the recall it ran in the background
  after the previous turn, so the first message of every session gets no memory. Sync recall
  answers the current message instead; the cost is in the table below.
- **`prefetch_waits_for_retain: false`.** By default the background recall for the next turn
  first waits for the previous turn's retain to finish. The previous turn is already in the
  conversation, and the wait is what pushed recall into the plugin's 3 s per-turn cap.

## Run the server (Linux, systemd user service)

Install and flags follow the Hindsight docs (`hindsight-docs/docs/developer/installation.md` and
`configuration.mdx`). `hindsight-api` is the full package with local embedding models;
`hindsight-api-slim` leaves them out.

```bash
uv venv ~/hindsight/venv --python 3.12
~/hindsight/venv/bin/pip install hindsight-api

cat > ~/hindsight/hindsight.env <<'EOF'
HINDSIGHT_API_LLM_PROVIDER=none
HINDSIGHT_API_RETAIN_EXTRACTION_MODE=chunks
HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
HINDSIGHT_API_RERANKER_PROVIDER=local
# Default storage is the embedded pg0 in ~/.hindsight/data; for production point it at PostgreSQL:
# HINDSIGHT_API_DATABASE_URL=postgresql://user:pass@localhost:5432/hindsight
EOF

mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/hindsight.service <<'EOF'
[Unit]
Description=Hindsight memory server (shared by every Hermes profile)

[Service]
EnvironmentFile=%h/hindsight/hindsight.env
ExecStart=%h/hindsight/venv/bin/hindsight-api --host 127.0.0.1 --port 8890
Restart=on-failure

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now hindsight
curl -s http://127.0.0.1:8890/health
```

The first start downloads the embedding and reranker models. With `LLM_PROVIDER=none` the server
stores chunks as `world` facts and builds no observations, so the setup script below sets
`recall_types: observation,world,experience` (the plugin's default, observations only, would
recall nothing). Keep `HINDSIGHT_API_WORKERS` at 1 with pg0.

## Apply it

```bash
# Principal profile only, against a Hindsight server already running on :8890
python scripts/hindsight_memory_setup.py

# Several or all profiles
python scripts/hindsight_memory_setup.py --profiles default,bento
python scripts/hindsight_memory_setup.py --all-profiles --dry-run

# No server yet: one shared embedded server (DeepSeek by default, same key for all profiles)
python scripts/hindsight_memory_setup.py --embedded
```

The script installs the catalog plugin (`hermes -p <profile> plugins install hindsight --enable
--yes-deps`, plus `--force` when a copy already exists). Without `--yes-deps` a run with no
terminal skips the plugin's Python dependencies and leaves it disabled ("dependency install
skipped (non-interactive)"); `--force` repairs a copy such a run left behind. It then writes
`<profile home>/hindsight/config.json` and sets `memory.provider: hindsight`. Start a new session afterwards; `hermes -p <profile> memory status`
confirms. The built-in `MEMORY.md`/`USER.md` stay on unless you pass `--builtin-off`; while on,
their writes are mirrored into Hindsight.

## Does the agent freeze?

Measured on 2026-10-06 with real `AIAgent` turns (`scripts/hindsight_bench/agent_turn.py`), the
real Hindsight plugin (catalog pin, `local_external`), hindsight-api 0.10.2, a fake LLM that
answers in 2 s, 10 turns per profile, every profile started at the same moment. Linux, 4 cores,
16 GB.

| Scenario | Turn p50 | Turn p95 (worst profile) | Longest wait for memory in a turn | RSS per agent |
|---|---|---|---|---|
| No external memory, 1 profile | 2.02 s | 3.3 s | — | 164 MB |
| Hindsight, principal only | 2.12 s | 3.3 s | 0.22 s | 198 MB |
| Hindsight, 10 profiles at once | 5.02 s | 6.2 s | 3.0 s (cap) | 198 MB |
| Hindsight, 20 profiles at once | 5.02 s | 11.8 s | 3.1 s (cap) | 198 MB |
| Hindsight server down, 10 profiles | 2.07 s | 8.7 s | 2.8 s | 199 MB |
| Hindsight server wedged (accepts, never answers), 10 profiles | 5.02 s | 6.1 s | 3.0 s (cap) | 199 MB |

The rows above use the background recall (`recall_sync: false`). The setup script turns
`recall_sync` on, measured the same way against an LLM-less server
(`HINDSIGHT_API_LLM_PROVIDER=none`, `recall_types: observation,world,experience`):

| Scenario | Turn p50 | Turn p95 (worst profile) | Memory wait in a turn: p50 / longest | Close |
|---|---|---|---|---|
| Background recall, principal only | 2.09 s | 5.0 s | — / 0.17 s | 0.07 s |
| Background recall, 10 profiles at once | 2.11 s | 6.5 s | — / 1.3 s | 0.10 s |
| `recall_sync`, principal only | 2.10 s | 4.4 s | — / 1.1 s | 0.02 s |
| `recall_sync`, 10 profiles at once | 2.10 s | 10.8 s | 75 ms / 3.9 s | 0.01 s |
| `recall_sync`, server wedged, 10 profiles | 2.02 s | 14.1 s | — / 8.0 s (cap) | 10.0 s |

With `recall_sync` the wait is the recall itself, no longer capped at 3 s by the plugin: usually
under 0.1 s, a few seconds when many profiles recall at the same instant, and at most 8 s
(core's cap on any external prefetch) when the server hangs. Still no turn hung or failed.

No turn hung and no turn failed. The plugin waits at most 3 s per turn for recall
(`prefetch()` joins its background recall with a 3 s cap) and core bounds every external
prefetch at 8 s (`agent/memory_manager.py`). The worst case is a slower turn, never a stuck
one. Closing a session against a wedged server takes up to 15 s (10 s retain-writer join plus
5 s prefetch join), measured at 15.0 s.

Each Hermes process with the plugin weighs ~34 MB more than without it: the plugin loads only
the HTTP client, never embedding models. The models live once, in the shared server.

## Server load: principal only vs every profile

Measured with `scripts/hindsight_bench/bench.py`: daemons started through hindsight-embed exactly
as the plugin does, driven with the plugin's per-turn calls (recall, then async retain). LLM and
embeddings were stand-ins (Hindsight's mock LLM and hashed vectors), so these numbers exclude
model compute.

Realistic pace, one turn every ~10 s per profile:

| Layout | Profiles | Recall p50 / p95 | Retain ack p95 | Server memory (PSS, idle → peak) | Errors |
|---|---|---|---|---|---|
| One server | 1 (principal) | 26 / 58 ms | 10 ms | 410 → 484 MB | 0 |
| One server | 10 | 62 / 396 ms | 58 ms | 411 → 640 MB | 0 |
| One server per profile | 6 | 30 / 72 ms | 20 ms | 2,228 → 2,636 MB (96 processes) | 0 |

Stress pace, one turn every ~1 s per profile:

| Layout | Profiles | Recall p50 / p95 | Server memory peak | CPU mean |
|---|---|---|---|---|
| One server | 1 | 31 / 201 ms | 500 MB | 17% |
| One server | 3 | 278 / 737 ms | 546 MB | 36% |
| One server | 6 | 1,047 / 2,028 ms | 645 MB | 42% |
| One server | 10 | 1,126 / 2,202 ms | 682 MB | 40% |
| One server, one shared bank | 6 | 207 / 458 ms | 582 MB | 39% |
| One server per profile | 3 | 51 / 308 ms | 1,370 MB | 47% |
| One server per profile | 6 | 81 / 404 ms | 2,715 MB | 82% |

Retain acknowledgements stayed under 60 ms at p95 in every run; every async retain became
recall-visible within 1.1 s, except a burst with no pause at all (27 s backlog on the principal).

Reading the numbers:

- A server per profile buys lower recall latency with ~370 MB and a ~15 s cold start per
  profile. On a laptop with dozens of profiles that is the RAM exhaustion pattern; avoid it.
- One server slows recall when many profiles are busy at the same instant, because the API is a
  single process. `HINDSIGHT_API_WORKERS` above 1 does not start with the embedded database
  ("Database URL is required for migrations"). The agent does not wait for that slowness beyond
  the 3 s cap above.
- Real embedding and reranker models add their RAM once to the shared server, and per profile in
  the server-per-profile layout.

## Reproduce

```bash
# Server bench (run as a non-root user; embedded PostgreSQL refuses root)
python scripts/hindsight_bench/tei_fake.py 18080 &
python scripts/hindsight_bench/bench.py shared_daemon 10 12 10 out.json

# Agent bench (fake LLM, then N real agents against one Hindsight server)
LLM_DELAY_S=2 python scripts/hindsight_bench/fake_llm.py 18090 &
HERMES_PYTHON=$(command -v python) HINDSIGHT_PLUGIN_SRC=path/to/hindsight-integrations/hermes \
  scripts/hindsight_bench/run_e2e.sh ./e2e hs10 10 hindsight http://127.0.0.1:8890 10
```
