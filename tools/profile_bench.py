"""Run one pyperformance benchmark warm under LeJIT for a while, for a
profiler:

    perf record -F 4999 -g -o richards.data \
        build/venv/3.10.16/bin/python tools/profile_bench.py richards 4
    perf report -i richards.data --no-children --sort sym -g none

Needs kernel.perf_event_paranoid <= 2 (Debian's default is 3:
sudo sysctl kernel.perf_event_paranoid=1, until reboot). Emitted code shows
as [JIT]. Run it in a venv here with LeJIT installed (tools/lejitenv.py
install), so perf records the benchmark, not pip.
"""
import os
import sys
import time
TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import pyperf37
pyperf37.start_lejit()
name, secs = sys.argv[1], float(sys.argv[2])
benches = pyperf37.load(os.path.join(TOOLS, 'pyperformance', 'bm_%s.py' % name))
_, timed = benches[0]
timed(3)                    # compile
t = time.perf_counter()
n = 0
while time.perf_counter() - t < secs:
    timed(1); n += 1
print(name, n, 'iterations')
