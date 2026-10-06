#!/usr/bin/env bash
# make_home.sh <dir> <provider: none|hindsight> <bank> <api_url>
# HINDSIGHT_PLUGIN_SRC: a checkout of vectorize-io/hindsight/hindsight-integrations/hermes.
# EXTRA_JSON: extra plugin settings, e.g. ',"prefetch_waits_for_retain":false'.
set -euo pipefail
d=$1; mkdir -p "$d/plugins"
if [ "$2" = hindsight ]; then
  cp -r "$HINDSIGHT_PLUGIN_SRC" "$d/plugins/hindsight"
  mkdir -p "$d/hindsight"
  printf '{"mode":"local_external","api_url":"%s","bank_id":"%s","memory_mode":"hybrid","auto_recall":true,"auto_retain":true,"retain_async":true%s}\n' "$4" "$3" "${EXTRA_JSON:-}" > "$d/hindsight/config.json"
  printf 'memory:\n  provider: hindsight\nplugins:\n  enabled: [hindsight]\n' > "$d/config.yaml"
else
  printf 'memory:\n  provider: ""\n' > "$d/config.yaml"
fi
