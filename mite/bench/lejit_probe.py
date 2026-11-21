"""With the checked LeJIT build (../lejit-checked, built with LE_ALL_CHECKS=1
LE_TRACE=1 LE_CODESTATS=1): what LeJIT does with the stats hot path.

    python -I lejit_probe.py

Runs the stats benchmark's work (Receiver._run -> Stats.process) in rounds
and prints, per round, LeJIT's compile counters (to see whether it keeps
recompiling), then _code_info() for each hot function and the promotion
sites. Timings from this build are not meaningful (checks are on)."""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "lejit-checked"), os.path.join(HERE, "site"),
          os.path.join(ROOT, "src"), HERE):
    sys.path.insert(1, p)

import lejit
lejit.start()
core = lejit._core

import logging
logging.basicConfig(level="INFO")

import benches
import mite.stats as ms
import mite.utils as mu
import mite.zmq as mz

b = benches.StatsBench(False)
b.recv._socket = benches._fake_socket(recv=__import__("itertools").cycle(b.pool).__next__)

KEYS = ("analyse", "call", "pause", "respawn", "promotion", "code_buffers",
        "code_bytes", "store_type_changes", "load_type_changes", "global_fails")


def snap():
    s = core._stats()
    return {k: s[k] for k in KEYS}


prev = snap()
for rnd in range(6):
    t = time.perf_counter()
    b.recv._run(benches._stop_after(2500))
    b.out.clear()
    now = snap()
    delta = {}
    for k in KEYS:
        if isinstance(now[k], tuple):
            delta[k] = (now[k][0] - prev[k][0], round(now[k][1] - prev[k][1], 4))
        else:
            delta[k] = now[k] - prev[k]
    print(f"round {rnd}: {time.perf_counter() - t:.3f}s  {json.dumps(delta)}")
    prev = now

st = b.stats._all_stats
fns = {
    "zmq.Receiver._run": mz.Receiver._run,
    "utils.unpack_msg": mu.unpack_msg,
    "stats.Stats.process": ms.Stats.process,
    "stats.Counter.process": ms.Counter.process,
    "stats.Accumulator.process": ms.Accumulator.process,
    "stats.Gauge.process": ms.Gauge.process,
    "stats.Histogram.process": ms.Histogram.process,
    "stats.matcher_by_type.<type_matcher>": st[0].matcher,
    "stats.extractor.<extract_items> (generator)": st[0].extractor.extract,
    "logging.Logger.debug": logging.Logger.debug,
    "logging.Logger.isEnabledFor": logging.Logger.isEnabledFor,
}
print()
for name, f in fns.items():
    print(f"{name:48s} {core._code_info(f.__code__)}")
if hasattr(core, "_promostats"):
    ps = core._promostats()
    print("\npromotion sites:", ps if not isinstance(ps, (list, dict)) or len(ps) < 40
          else list(ps.items())[:40] if isinstance(ps, dict) else ps[:40])

# promotion sites in mite's and the logging module's code: values seen per site
print("\npromotion sites in the stats path (code, values promoted on):")
for rec in core._promostats():
    co = rec[0]
    if not any(s in co.co_filename for s in ("mite/", "logging", "zmq")):
        continue
    vals = [v for v in rec if isinstance(v, list)]
    vals = vals[0] if vals else []
    print(f"  {co.co_filename.split('/')[-1]}:{co.co_firstlineno}:{co.co_name}"
          f"  {len(vals)} value(s): {[v[0] for v in vals][:6]}")
