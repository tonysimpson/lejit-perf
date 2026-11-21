#!/bin/bash
# Run every benchmark RUNS times in each mode, two at a time, each pinned to
# its own physical core; results go to ../results/runs-<lane>.jsonl, then
# summarise.py prints the table.
#   ./runall.sh [RUNS] [DURATION] [bench ...]
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
PY="$ROOT/venv/bin/python"
RUNS=${1:-5}; DUR=${2:-10}; shift 2 2>/dev/null || shift $#
BENCHES=${*:-$(cd "$HERE" && "$PY" -I mitebench.py list 2>/dev/null)}
OUT="$ROOT/results"; mkdir -p "$OUT"
jobs=()
for r in $(seq "$RUNS"); do for b in $BENCHES; do for m in plain lejit late; do
  jobs+=("$b $m"); done; done; done
lane() {   # lane CORE INDEX
  local core=$1 idx=$2 i
  for ((i = idx; i < ${#jobs[@]}; i += 2)); do
    set -- ${jobs[$i]}
    (cd "$HERE" && prlimit --as=4000000000 taskset -c "$core" timeout 300 \
      "$PY" -I mitebench.py "$1" --mode "$2" --duration "$DUR" 2>>"$OUT/stderr-$core.log" \
      || echo "{\"bench\": \"$1\", \"mode\": \"$2\", \"error\": \"exit $?\"}") >>"$OUT/runs-$core.jsonl"
  done
}
lane 3 0 & p1=$!
lane 5 1 & p2=$!
wait $p1 $p2
"$PY" -I "$HERE/summarise.py" "$OUT"
