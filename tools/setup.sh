#!/bin/bash
# One-time setup, safe to run again: pyenv's CPython 3.7 to 3.10 (the
# versions CI and the history use), a venv for each under build/venv/, and
# a check for the programs the instruction counts need.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)

if ! command -v pyenv > /dev/null; then
    echo "pyenv is needed: https://github.com/pyenv/pyenv#installation"
    exit 1
fi
versions=$(python3 -c "import sys; sys.path.insert(0, '$here/tools'); import lejitenv; print(*lejitenv.DEFAULT_VERSIONS)")
for v in $versions; do
    pyenv install --skip-existing "$v"
    python3 "$here/tools/lejitenv.py" venv --python "$v"
done

missing=0
if ! command -v perf > /dev/null; then
    echo "perf is missing (instruction counts): on Debian or Ubuntu, sudo apt install linux-perf (Ubuntu: linux-tools-generic)"
    missing=1
fi
if ! command -v setarch > /dev/null; then
    echo "setarch is missing (instruction counts): it is in util-linux"
    missing=1
fi
if [ "$(cat /proc/sys/kernel/perf_event_paranoid)" -gt 2 ]; then
    echo "perf can't count until: sudo sysctl kernel.perf_event_paranoid=1 (until reboot)"
    missing=1
fi
[ $missing = 0 ] && echo "ready"
exit 0
