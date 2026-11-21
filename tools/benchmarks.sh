#!/bin/bash
# Build LeJIT for release and run the timing suites, printing the tables and
# keeping a copy in build/bench/<date>-<commit>.txt. Run from anywhere:
#
#   tools/benchmarks.sh                # bench and pyperf
#   tools/benchmarks.sh pyperf         # one suite: bench or pyperf
#   tools/benchmarks.sh -k richards    # only names containing 'richards'
#   tools/benchmarks.sh --python ~/.pyenv/versions/3.7.17/bin/python3.7
#   tools/benchmarks.sh --lejit ~/work/lejit-worktrees/x
#
# Suites: bench   tools/bench.py        small kernels, LeJIT against CPython
#         pyperf  tools/pyperf37.py     pyperformance, LeJIT against CPython
#
# The Python is --python, else $PY, else python3; the LeJIT checkout is
# --lejit, else $LEJIT, else ~/work/lejit. Leaves a release build there for
# that Python. Take a heavy-job slot first.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-python3}
LEJIT=${LEJIT:-$HOME/work/lejit}

suites=()
filter=()
while [ $# -gt 0 ]; do
    case "$1" in
        bench|pyperf) suites+=("$1") ;;
        -k) filter=(-k "$2"); shift ;;
        --python) PY=$2; shift ;;
        --lejit) LEJIT=$2; shift ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1 (try --help)"; exit 2 ;;
    esac
    shift
done
[ ${#suites[@]} -eq 0 ] && suites=(bench pyperf)
LEJIT=$(cd "$LEJIT" && pwd)
export LEJIT

# release build: the build rebuilds when the LE_ options change
mkdir -p "$here/build/bench"
if ! env $(env | grep -o '^LE_[A-Za-z0-9_]*' | sed 's/^/-u /') \
        make -C "$LEJIT" build PY="$PY" > "$here/build/bench/build.log" 2>&1; then
    grep -E ": error|Error" "$here/build/bench/build.log" | head -20
    echo "BUILD FAILED (full log: $here/build/bench/build.log)"
    exit 1
fi

commit=$(git -C "$LEJIT" rev-parse --short HEAD)
dirty=$(git -C "$LEJIT" status --porcelain --untracked-files=no | head -1)
out=$here/build/bench/$(date +%Y-%m-%d-%H%M)-$commit${dirty:+-dirty}.txt

{
    echo "LeJIT benchmarks: $(date '+%Y-%m-%d %H:%M'), commit $commit${dirty:+ (uncommitted changes)}"
    echo "release build, $("$PY" --version 2>&1), $(nproc) CPUs"
    for s in "${suites[@]}"; do
        echo
        case $s in
            bench)   echo "== small kernels (tools/bench.py)"
                     "$PY" "$here/tools/bench.py" "${filter[@]}" || echo "(failed)" ;;
            pyperf)  echo "== pyperformance (tools/pyperf37.py)"
                     "$PY" "$here/tools/pyperf37.py" "${filter[@]}" || echo "(failed)" ;;
        esac
    done
} 2>&1 | tee "$out"
echo
echo "saved: $out"
