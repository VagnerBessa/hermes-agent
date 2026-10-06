"""Drive a REAL Hermes AIAgent through N turns with the memory provider configured in HERMES_HOME
and time what the user waits for: the whole turn, and the part of it spent inside memory calls.

usage: agent_turn.py <out.json> <turns> <llm_base_url>     (HERMES_HOME must be set)
"""
import json
import os
import sys
import time

out_path, turns, base_url = sys.argv[1], int(sys.argv[2]), sys.argv[3]
t_import = time.perf_counter()
from run_agent import AIAgent  # noqa: E402

timings = {"prefetch": [], "sync": [], "queue": [], "turn": []}


def wrap(obj, name, bucket):
    fn = getattr(obj, name)

    def timed(*a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            timings[bucket].append(time.perf_counter() - t0)

    setattr(obj, name, timed)


t0 = time.perf_counter()
agent = AIAgent(model="fake-model", base_url=base_url, api_key="sk-fake", quiet_mode=True,
                enabled_toolsets=["memory"], max_iterations=3)
init_s = time.perf_counter() - t0
mm = agent._memory_manager
provider = mm.providers[0].name if mm and mm.providers else None
if mm:
    wrap(mm, "prefetch_all", "prefetch")
    wrap(mm, "sync_all", "sync")
    wrap(mm, "queue_prefetch_all", "queue")

history, errors = [], 0
topics = ["o boletim de conjuntura", "o ajuste sazonal X13", "o vault Tolaria", "o CAGED", "o PIB municipal"]
for i in range(turns):
    msg = f"Turno {i}: lembre que prefiro respostas curtas sobre {topics[i % len(topics)]}; prazo dia {i + 3}."
    t0 = time.perf_counter()
    try:
        r = agent.run_conversation(user_message=msg, conversation_history=history)
        history = r.get("messages", history)
    except Exception as e:  # noqa: BLE001
        errors += 1
        print("turn error", type(e).__name__, e, file=sys.stderr)
    timings["turn"].append(time.perf_counter() - t0)

t0 = time.perf_counter()
if mm:
    mm.shutdown_all()
shutdown_s = time.perf_counter() - t0


def stats(xs):
    xs = sorted(xs)
    if not xs:
        return None
    q = lambda p: round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] * 1000, 1)  # noqa: E731
    return {"n": len(xs), "p50": q(0.5), "p95": q(0.95), "max": q(1.0)}


import psutil  # noqa: E402

res = {"rss_mb": round(psutil.Process().memory_info().rss / 1e6), "provider": provider, "home": os.environ.get("HERMES_HOME"), "init_s": round(init_s, 2),
       "turn_ms": stats(timings["turn"]), "prefetch_ms": stats(timings["prefetch"]),
       "sync_ms": stats(timings["sync"]), "queue_ms": stats(timings["queue"]),
       "shutdown_s": round(shutdown_s, 2), "errors": errors}
json.dump(res, open(out_path, "w", encoding="utf-8"))
print(json.dumps(res))
os._exit(0)
