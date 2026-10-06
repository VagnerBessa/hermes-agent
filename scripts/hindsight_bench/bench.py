"""Load test: Hindsight memory for the principal Hermes profile only vs. every profile.

Daemons are started through hindsight_embed's manager, exactly as the Hermes plugin's
local_embedded mode does, and driven with the same client calls the plugin makes per turn:
arecall (auto-recall before a turn) and aretain_batch(retain_async=True) (auto-retain after it).

usage: bench.py <scenario> <n_profiles> <turns> <think_s> <out.json>
scenario: principal | shared_daemon | shared_bank | daemon_per_profile

Env: DRIFT_AT="20,50" restarts the shared daemon with different LLM settings at those seconds
(what the plugin does when a profile with other LLM settings starts); EXTRA_CFG='{"K": "V"}' adds
daemon settings. Run it as a non-root user (embedded PostgreSQL refuses root), with
``python tei_fake.py 18080`` running and hindsight-api-slim, hindsight-client, hindsight-embed,
pg0-embedded and psutil installed. See website/docs/guides/hindsight-memory-profiles.md.
"""
import asyncio
import json
import os
import random
import statistics
import sys
import threading
import time

import psutil
from hindsight_client import Hindsight
from hindsight_embed import get_embed_manager

CFG = {
    "HINDSIGHT_API_LLM_PROVIDER": "mock",
    "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "tei",
    "HINDSIGHT_API_EMBEDDINGS_TEI_URL": os.environ.get("TEI_URL", "http://127.0.0.1:18080"),
    "HINDSIGHT_API_RERANKER_PROVIDER": "rrf",
    "HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT": "0",
    "HINDSIGHT_API_LOG_LEVEL": "warning",
    **json.loads(os.environ.get("EXTRA_CFG", "{}")),
}

BENCH_USER = os.environ.get("BENCH_USER") or psutil.Process().username()

TOPICS = [
    "o boletim de conjuntura paulista de setembro e a taxa de desemprego da RMSP",
    "o ajuste sazonal X13 da série de produção industrial do IBGE",
    "o vault Tolaria e as notas sobre filosofia da ciência",
    "a revisão do relatório técnico sobre mercado de trabalho formal no CAGED",
    "a rotina Para você ver às 8h e 14h no balão do mascote",
    "o painel de PIB municipal e a nova base de 2021",
    "a skill de escrita do Seade e o guia de estilo das notas",
    "a migração do harness do Hermes para a memória do Hindsight",
]


def turn_text(profile: str, i: int) -> tuple[str, str]:
    t = random.choice(TOPICS)
    user = (
        f"User: No perfil {profile}, turno {i}, quero retomar {t}. "
        f"Lembre que eu prefiro respostas curtas com evidência e que o prazo é dia {random.randint(1, 28)}. "
        f"Vagner trabalha no Seade e usa DeepSeek no Bento."
    )
    asst = (
        f"Assistant: Certo. Sobre {t}, o ponto principal é organizar as fontes, conferir a série "
        f"e registrar a decisão. Vou anotar o prazo e a preferência por respostas curtas. " * 3
    )
    return f"{user}\n{asst}", f"o que eu disse sobre {random.choice(TOPICS)}?"


def proc_rss_mb() -> tuple[float, int]:
    total, n = 0.0, 0
    for p in psutil.process_iter(["username", "cmdline"]):
        try:
            if p.info["username"] != BENCH_USER:
                continue
            cmd = " ".join(p.info["cmdline"] or [])
            if "hindsight" in cmd and "bench.py" not in cmd or "postgres" in cmd:
                total += p.memory_full_info().pss / 1e6
                n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            pass
    return total, n


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.peak, self.samples, self.stop_ev = 0.0, [], threading.Event()
        self.cpu = []

    def run(self):
        psutil.cpu_percent(None)
        while not self.stop_ev.wait(0.5):
            rss, n = proc_rss_mb()
            self.peak = max(self.peak, rss)
            self.samples.append((rss, n))
            self.cpu.append(psutil.cpu_percent(None))


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(q / 100 * (len(xs) - 1))))] * 1000, 1)


async def agent(client: Hindsight, bank: str, profile: str, turns: int, think: float, out: dict):
    rec, ret, errs, ops, hits = [], [], 0, [], []
    for i in range(turns):
        content, query = turn_text(profile, i)
        t0 = time.perf_counter()
        try:
            r = await asyncio.wait_for(
                client.arecall(bank_id=bank, query=query, budget="mid", max_tokens=4096, types=["observation"]),
                30,
            )
            rec.append(time.perf_counter() - t0)
            hits.append(len(getattr(r, "results", None) or []))
        except Exception as e:  # noqa: BLE001
            if "NotFound" in type(e).__name__:  # bank not created yet: the first turn, empty recall
                rec.append(time.perf_counter() - t0)
                hits.append(0)
            else:
                errs += 1
                out.setdefault("err_samples", []).append(f"recall {type(e).__name__}: {e}"[:200])
        t0 = time.perf_counter()
        try:
            r = await asyncio.wait_for(
                client.aretain_batch(
                    bank_id=bank,
                    items=[{"content": content, "context": f"conversation between Hermes Agent ({profile}) and the User"}],
                    document_id=f"{profile}-session",
                    retain_async=True,
                ),
                30,
            )
            ret.append(time.perf_counter() - t0)
            op = getattr(r, "operation_id", None)
            if op:
                ops.append(op)
        except Exception as e:  # noqa: BLE001
            errs += 1
            out.setdefault("err_samples", []).append(f"retain {type(e).__name__}: {e}"[:200])
        await asyncio.sleep(think * random.uniform(0.5, 1.5))
    out.update(recall=rec, retain=ret, errors=errs, ops=ops, bank=bank, hits=hits)


async def drain(client: Hindsight, bank: str, ops: list[str], deadline: float) -> float | None:
    """Seconds until every async retain op of *bank* completed (recall-visible)."""
    t0 = time.perf_counter()
    pending = set(ops)
    while pending and time.perf_counter() - t0 < deadline:
        for op in list(pending):
            try:
                st = await client.operations.get_operation_status(bank_id=bank, operation_id=op)
                if str(getattr(st, "status", "")).lower() in {"completed", "failed"}:
                    pending.discard(op)
            except Exception:  # noqa: BLE001
                pending.discard(op)
        if pending:
            await asyncio.sleep(0.5)
    return None if pending else time.perf_counter() - t0


def main():
    scenario, n, turns, think, out_path = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
    tag = f"{scenario}-{n}-{int(time.time()) % 100000}"
    mgr = get_embed_manager()
    profiles = [f"p{i}" for i in range(n)]
    if scenario == "principal":
        profiles = ["principal"]
    daemon_profiles = [f"{tag}-{p}" for p in profiles] if scenario == "daemon_per_profile" else [f"{tag}-shared"]

    idle_before, _ = proc_rss_mb()
    starts = []
    for dp in daemon_profiles:
        t0 = time.perf_counter()
        ok = mgr.ensure_running(dict(CFG), dp)
        starts.append(round(time.perf_counter() - t0, 2))
        if not ok:
            raise SystemExit(f"daemon {dp} failed to start")
    time.sleep(2)
    idle_rss, idle_n = proc_rss_mb()

    def url_for(i):
        return mgr.get_url(daemon_profiles[i] if scenario == "daemon_per_profile" else daemon_profiles[0])

    def bank_for(i, p):
        return "hermes" if scenario == "shared_bank" else f"hermes-{p}"

    sampler = Sampler()
    sampler.start()

    # Simulate another Hermes profile starting with DIFFERENT LLM settings on the shared daemon:
    # the plugin rewrites the daemon env and stops it (_profile_env_drifted), then restarts it.
    restarts = []

    def drifter():
        for at in [float(x) for x in os.environ.get("DRIFT_AT", "").split(",") if x]:
            time.sleep(max(0.0, at - (time.perf_counter() - t_start)))
            t0 = time.perf_counter()
            mgr.stop(daemon_profiles[0])
            cfg = dict(CFG, HINDSIGHT_API_LLM_MODEL=f"other-{at}")
            mgr.ensure_running(cfg, daemon_profiles[0])
            restarts.append(round(time.perf_counter() - t0, 1))

    t_start = time.perf_counter()
    drift_thread = threading.Thread(target=drifter, daemon=True)
    drift_thread.start()

    async def run_all():
        clients = [Hindsight(base_url=url_for(i), timeout=30) for i in range(len(profiles))]
        outs = [dict(profile=p) for p in profiles]
        t0 = time.perf_counter()
        await asyncio.gather(*[agent(clients[i], bank_for(i, p), p, turns, think, outs[i]) for i, p in enumerate(profiles)])
        wall = time.perf_counter() - t0
        drains = await asyncio.gather(*[drain(clients[i], outs[i]["bank"], outs[i]["ops"], 300) for i in range(len(profiles))])
        return outs, wall, drains

    outs, wall, drains = asyncio.run(run_all())
    drift_thread.join(timeout=120)
    sampler.stop_ev.set()
    sampler.join()

    rec = [x for o in outs for x in o["recall"]]
    ret = [x for o in outs for x in o["retain"]]
    res = {
        "scenario": scenario,
        "profiles": len(profiles),
        "daemons": len(daemon_profiles),
        "turns_per_profile": turns,
        "think_s": think,
        "daemon_start_s": starts,
        "pss_mb_before": round(idle_before),
        "pss_mb_idle_after_start": round(idle_rss),
        "procs_idle": idle_n,
        "pss_mb_peak": round(sampler.peak),
        "cpu_pct_mean": round(statistics.mean(sampler.cpu), 1) if sampler.cpu else None,
        "cpu_pct_max": max(sampler.cpu) if sampler.cpu else None,
        "wall_s": round(wall, 1),
        "recall_ms": {"n": len(rec), "p50": pct(rec, 50), "p95": pct(rec, 95), "max": pct(rec, 100)},
        "retain_ack_ms": {"n": len(ret), "p50": pct(ret, 50), "p95": pct(ret, 95), "max": pct(ret, 100)},
        "recall_hits_mean_last_half": round(statistics.mean([h for o in outs for h in o["hits"][len(o["hits"]) // 2:]] or [0]), 1),
        "errors": sum(o["errors"] for o in outs),
        "err_samples": [e for o in outs for e in o.get("err_samples", [])][:5],
        "retain_drain_s_max": None if any(d is None for d in drains) else round(max(drains), 1),
        "retain_drain_timeouts": sum(1 for d in drains if d is None),
        "daemon_restarts_s": restarts,
    }
    for dp in daemon_profiles:
        mgr.stop(dp)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res))


if __name__ == "__main__":
    main()
