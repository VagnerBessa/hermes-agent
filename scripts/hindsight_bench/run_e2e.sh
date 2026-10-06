#!/usr/bin/env bash
# run_e2e.sh <out_root> <tag> <n_agents> <provider: none|hindsight> <api_url> <turns>
# Starts n real Hermes AIAgents at once, one profile home each, against fake_llm.py on :18090.
# HERMES_PYTHON: interpreter with Hermes + hindsight-client installed (run from the checkout root).
set -euo pipefail
root=$1; tag=$2; n=$3; prov=$4; url=$5; turns=$6
here=$(cd "$(dirname "$0")" && pwd)
rm -rf "${root:?}/$tag"; mkdir -p "$root/$tag"
for i in $(seq 0 $((n-1))); do "$here/make_home.sh" "$root/$tag/h$i" "$prov" "hermes-$tag-p$i" "$url"; done
for i in $(seq 0 $((n-1))); do
  HERMES_HOME="$root/$tag/h$i" "$HERMES_PYTHON" "$here/agent_turn.py" "$root/$tag/r$i.json" "$turns" http://127.0.0.1:18090/v1 > "$root/$tag/log$i.txt" 2>&1 &
done
wait
"$HERMES_PYTHON" - "$root/$tag" <<'P'
import glob, json, statistics, sys
rs = [json.load(open(f)) for f in sorted(glob.glob(f"{sys.argv[1]}/r*.json"))]
worst = lambda k, f: max((r[k][f] for r in rs if r.get(k)), default=None)  # noqa: E731
print(json.dumps({"agents": len(rs), "turn_p50_median": round(statistics.median(r["turn_ms"]["p50"] for r in rs)),
                  "turn_p95_worst": worst("turn_ms", "p95"), "prefetch_worst": worst("prefetch_ms", "max"),
                  "shutdown_s_worst": max(r["shutdown_s"] for r in rs),
                  "rss_mb_per_agent": round(statistics.mean(r["rss_mb"] for r in rs)), "errors": sum(r["errors"] for r in rs)}))
P
