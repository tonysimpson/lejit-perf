#!/bin/bash
# perf record each benchmark at a fixed workload, plain and LeJIT side by side
# (cores 3 and 5), into ../perf/<bench>-<mode>.data; then perfsplit.py.
#   ./perf.sh [CALLGRAPH]     CALLGRAPH: fp (default) or dwarf
set -u
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(dirname "$HERE"); PY="$ROOT/venv/bin/python"
CG=${1:-fp}; OUT="$ROOT/perf"; mkdir -p "$OUT"
for job in "stats 60000" "stats_quiet 1500000" "controller 100000" "runner 2000000"; do
  set -- $job
  for m in plain lejit; do
    core=$([ $m = plain ] && echo 3 || echo 5)
    (cd "$HERE" && prlimit --as=4000000000 taskset -c $core perf record -F 2000 \
       --call-graph "$CG" -o "$OUT/$1-$m.data" -q -- \
       "$PY" -I mitebench.py "$1" --mode $m --ops "$2" >"$OUT/$1-$m.json" 2>/dev/null) &
  done; wait
done
for f in "$OUT"/*.data; do "$PY" -I "$HERE/perfsplit.py" "$f"; done
