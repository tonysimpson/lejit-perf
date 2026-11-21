"""Run one mite benchmark in this process and print one JSON line.

    python -I mitebench.py BENCH --mode plain|lejit|late [--duration S] [--late-at S]
    python -I mitebench.py BENCH --mode ... --check     fixed workload, prints a digest
    python -I mitebench.py gen                          (re)make data/pool.msgpack etc.
    python -I mitebench.py list

plain: plain CPython.  lejit: lejit.start() before mite is imported.
late: plain until --late-at seconds (or half the work, with --check), then
lejit.start().
"""

import argparse
import json
import logging
import os
import resource
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "lejit"), os.path.join(HERE, "site"),
          os.path.join(ROOT, "src"), HERE):
    if p not in sys.path:
        sys.path.insert(1, p)

ap = argparse.ArgumentParser()
ap.add_argument("bench")
ap.add_argument("--mode", default="plain", choices=("plain", "lejit", "late"))
ap.add_argument("--duration", type=float, default=10.0)
ap.add_argument("--late-at", type=float, default=3.0)
ap.add_argument("--check", action="store_true")
ap.add_argument("--ops", type=int, help="stop after this many ops (no digest work), for perf")
ap.add_argument("--profile", help="write cProfile stats here (plain mode only)")
ap.add_argument("--profile-after", type=float, default=0.0,
                help="start profiling only after this many seconds")
args = ap.parse_args()

if args.mode == "lejit":
    import lejit
    lejit.start()

# as mite's setup_logging does, with mite's default --log-level=INFO
logging.basicConfig(level="INFO",
                    format="[%(asctime)s] <%(levelname)s> [%(name)s] %(message)s")


class Clock:
    """The benchmarks call tick(n) after each batch of n operations."""

    def __init__(self, mode, duration, late_at, limit=None):
        self.mode = mode
        self.duration = duration
        self.late_at = late_at
        self.limit = limit          # fixed op count, for --check
        self.ops = 0
        self.timeline = []          # (t_end, ops, dt)
        self.lejit_at = 0.0 if mode == "lejit" else None
        self.done = False
        self.prof = self.prof_at = None
        self.t0 = self.last = time.perf_counter()

    def tick(self, n):
        now = time.perf_counter()
        self.ops += n
        self.timeline.append((now - self.t0, n, now - self.last))
        if self.mode == "late" and self.lejit_at is None:
            due = (self.ops >= self.limit // 2) if self.limit else \
                  (now - self.t0 >= self.late_at)
            if due:
                import lejit
                lejit.start()
                self.lejit_at = time.perf_counter() - self.t0
                now = time.perf_counter()
        if self.prof_at is not None and now - self.t0 >= self.prof_at:
            self.prof.enable()
            self.prof_at = None
        self.last = now
        if self.limit is not None:
            self.done = self.ops >= self.limit
        else:
            self.done = now - self.t0 >= self.duration
        return self.done


def _rate(tl, lo, hi):
    ops = sum(n for t, n, dt in tl if lo < t <= hi)
    dt = sum(dt for t, n, dt in tl if lo < t <= hi)
    return ops / dt if dt > 0 else 0.0


def analyse(clock):
    tl = clock.timeline
    end = tl[-1][0]
    warm_lo = end * 0.6
    warm = _rate(tl, warm_lo, end)
    # warm-up: the earliest t (from when LeJIT started, or from 0) after which
    # every 1 s window runs at >= 90% of the warm rate
    base = clock.lejit_at or 0.0
    step, win = 0.25, 1.0
    starts = [base + k * step for k in range(int((end - base - win) / step) + 1)]
    ok = [_rate(tl, s, s + win) >= 0.9 * warm for s in starts]
    warmup = None
    for i in range(len(ok)):
        if all(ok[i:]):
            warmup = starts[i] - base
            break
    return dict(
        warm=warm,
        first_s=_rate(tl, base, base + 1.0),
        warmup_s=warmup,
        total_ops=clock.ops,
        elapsed=end,
        lejit_at=clock.lejit_at,
        per_s=[round(_rate(tl, k, k + 1.0)) for k in range(int(end))],
    )


def main():
    import benches
    if args.bench == "list":
        print(" ".join(benches.BENCHES))
        return
    if args.bench == "gen":
        benches.generate()
        return
    factory = benches.BENCHES[args.bench]
    b = factory(check=args.check)
    clock = Clock(args.mode, args.duration, args.late_at,
                  b.check_ops if args.check else args.ops)
    prof = None
    if args.profile:
        import cProfile
        prof = cProfile.Profile()
        if args.profile_after:
            clock.prof, clock.prof_at = prof, args.profile_after
        else:
            prof.enable()
    err = None
    try:
        b.run(clock)
    except BaseException as e:      # report, then fail
        import traceback
        err = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    if prof:
        prof.disable()
        prof.dump_stats(args.profile)
    out = dict(bench=args.bench, mode=args.mode, check=args.check,
               maxrss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    if err:
        out["error"] = err
    else:
        out.update(analyse(clock))
        if args.check:
            out["digest"] = b.digest()
    print(json.dumps(out), flush=True)
    if err:
        sys.exit(1)


main()
