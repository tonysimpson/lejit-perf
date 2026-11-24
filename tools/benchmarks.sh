#!/bin/bash
# Install LeJIT into a venv and run the timing suites, printing the tables
# and keeping a copy in build/bench/<date>-<python>.txt. Run from anywhere:
#
#   tools/benchmarks.sh                # bench and pyperf
#   tools/benchmarks.sh pyperf         # one suite: bench or pyperf
#   tools/benchmarks.sh -k richards    # only names containing 'richards'
#   tools/benchmarks.sh --python 3.7   # pyenv's 3.7, or a path
#   tools/benchmarks.sh --lejit my-branch          # a GitHub ref
#   tools/benchmarks.sh --lejit ~/work/lejit-worktrees/x   # a checkout
#
# Suites: bench   tools/bench.py        small kernels, LeJIT against CPython
#                                       and CPython with a pass-through hook
#         pyperf  tools/pyperf37.py     pyperformance, the same
#
# --python defaults to 3.10 and --lejit to $LEJIT, else main
# (tools/lejitenv.py). Take a heavy-job slot first.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)

suites=()
filter=()
python=3.10
lejit=${LEJIT:-main}
while [ $# -gt 0 ]; do
    case "$1" in
        bench|pyperf) suites+=("$1") ;;
        -k) filter=(-k "$2"); shift ;;
        --python) python=$2; shift ;;
        --lejit) lejit=$2; shift ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1 (try --help)"; exit 2 ;;
    esac
    shift
done
[ ${#suites[@]} -eq 0 ] && suites=(bench pyperf)

py=$(python3 "$here/tools/lejitenv.py" install --python "$python" --lejit "$lejit")
mkdir -p "$here/build/bench"
out=$here/build/bench/$(date +%Y-%m-%d-%H%M)-$("$py" -c 'import platform; print(platform.python_version())').txt

{
    echo "LeJIT benchmarks: $(date '+%Y-%m-%d %H:%M'), LeJIT $lejit"
    echo "release build, $("$py" --version 2>&1), $(nproc) CPUs"
    for s in "${suites[@]}"; do
        echo
        case $s in
            bench)   echo "== small kernels (tools/bench.py)"
                     "$py" "$here/tools/bench.py" "${filter[@]}" || echo "(failed)" ;;
            pyperf)  echo "== pyperformance (tools/pyperf37.py)"
                     "$py" "$here/tools/pyperf37.py" "${filter[@]}" || echo "(failed)" ;;
        esac
    done
} 2>&1 | tee "$out"
echo
echo "saved: $out"
