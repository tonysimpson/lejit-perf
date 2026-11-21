"""Split the stats component's time into its parts, with or without LeJIT
(perf is not allowed here, and cProfile would stop LeJIT compiling):

    python -I attrib.py plain|lejit

  unpack       mite.utils.unpack_msg per message (msgpack, C)
  repr         the f"message to match by type: {msg}" that every
               matcher_by_type matcher builds, once per processor (C repr)
  stats        Receiver._run + Stats.process, all processors
  stats-repr   stats minus repr: the Python left over
"""

import itertools
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "lejit"), os.path.join(HERE, "site"),
          os.path.join(ROOT, "src"), HERE):
    sys.path.insert(1, p)

mode = sys.argv[1]
if mode == "lejit":
    import lejit
    lejit.start()

import logging
logging.basicConfig(level="INFO")

import benches
from mite.utils import unpack_msg

pool = benches._load(benches.POOL)
msgs = [unpack_msg(r) for r in pool]
N = len(pool)


def best(fn, reps=5):
    fn()
    ts = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t)
    return min(ts) / N * 1e6      # microseconds per message


def unpack():
    for r in pool:
        unpack_msg(r)


s = benches.StatsBench(False)
nproc = len(s.stats._all_stats)


def reprs():
    for m in msgs:
        for _ in range(nproc):
            f"message to match by type: {m}"


def stats():
    s.recv._socket = benches._fake_socket(recv=iter(pool).__next__)
    s.recv._run(benches._stop_after(N))
    s.out.clear()


r = dict(mode=mode, processors=nproc, unpack=best(unpack), repr=best(reprs),
         stats=best(stats))
r["stats-repr"] = r["stats"] - r["repr"]
print(json.dumps({k: round(v, 2) if isinstance(v, float) else v for k, v in r.items()}))

# What-if (NOT mite as shipped): the same processors with matchers that skip
# the logger.debug f-string, to show what LeJIT does with the Python left.
def _quiet(m):
    targets = m.__closure__[0].cell_contents

    def type_matcher(msg):
        return msg.get("type", None) in targets
    return type_matcher


for p in s.stats._all_stats:
    p.matcher = _quiet(p.matcher)
r2 = best(stats)
print(json.dumps({"mode": mode, "stats_without_debug_fstring_us": round(r2, 2),
                  "msg_per_s": round(1e6 / r2)}))
