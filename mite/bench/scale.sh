#!/bin/bash
# Controller steady-state throughput against the number of runners.
#   ./scale.sh [RUNS]   -> ../results/scale.jsonl
set -u
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(dirname "$HERE"); PY="$ROOT/venv/bin/python"
RUNS=${1:-3}
for r in $(seq "$RUNS"); do for n in 100 500 2000; do
  for m in plain lejit; do
    core=$([ $m = plain ] && echo 3 || echo 5)
    (cd "$HERE" && MITE_BENCH_RUNNERS=$n prlimit --as=4000000000 taskset -c $core \
      "$PY" -I mitebench.py controller --mode $m --duration 20 2>/dev/null \
      | "$PY" -c "import json,sys; d=json.load(sys.stdin); d['runners']=$n; print(json.dumps(d))") \
      >>"$ROOT/results/scale.jsonl" &
  done; wait
done; done
